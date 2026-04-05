import csv
import io
import json
import logging
import asyncio
import threading
from datetime import datetime
from asgiref.sync import sync_to_async

from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count

from telegram import Bot, Update
from telegram.constants import ParseMode

from bot.bot_app import get_ptb_application
from bot.models import OTEvent, OTSignup, Agent
from bot.utils import format_announcement, format_signup_list, announcement_keyboard, _esc

logger = logging.getLogger(__name__)


async def telegram_webhook(request):
    """
    Async Django view — processes Telegram webhook updates.
    Requires running under an ASGI server (uvicorn) so the
    event loop is persistent and PTB's HTTP client stays alive.
    """
    if request.method != "POST":
        return JsonResponse({"status": "only POST allowed"}, status=405)

    try:
        app = get_ptb_application()
        if not app:
            return JsonResponse({"status": "error", "message": "Bot not configured"}, status=500)

        # Initialize once — safe to call repeatedly, PTB is idempotent here.
        if not app._initialized:
            await app.initialize()

        payload = json.loads(request.body.decode("utf-8"))
        update = Update.de_json(payload, app.bot)
        await app.process_update(update)

        return JsonResponse({"status": "ok"})
    except Exception as exc:
        import traceback
        err_str = traceback.format_exc()
        logger.error(f"Error handling webhook update:\n{err_str}")
        return JsonResponse({"status": "error", "error": str(exc), "traceback": err_str}, status=500)

telegram_webhook.csrf_exempt = True


# --- Web Dashboard Views ---

@login_required
def dashboard_view(request):
    events = OTEvent.objects.filter(is_open=True).order_by('-created_at')
    past_events = OTEvent.objects.filter(is_open=False).order_by('-created_at')[:10]
    active_signups = OTSignup.objects.filter(ot_event__is_open=True).count()
    
    return render(request, "bot/dashboard.html", {
        "events": events,
        "past_events": past_events,
        "active_signups": active_signups,
    })


@login_required
def ot_create_view(request):
    if request.method == "POST":
        # Server-side debounce/idempotency guard for accidental double-clicks.
        lock_key = f"ot_create_lock:{request.user.id}"
        if not cache.add(lock_key, True, timeout=8):
            messages.warning(request, "Publish already in progress. Duplicate submit ignored.")
            return redirect("dashboard")

        title = request.POST.get("title", "").strip()
        days = request.POST.getlist("days")
        max_agents_str = request.POST.get("max_agents", "").strip()
        deadline_str = request.POST.get("deadline", "")
        
        if not title or not days:
            messages.error(request, "Title and at least one day are required.")
            return redirect("ot-create")
            
        max_agents = int(max_agents_str) if max_agents_str.isdigit() else None
        
        deadline = None
        if deadline_str:
            try:
                # Format from HTML datetime-local: YYYY-MM-DDTHH:MM
                dt_naive = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
                deadline = timezone.make_aware(dt_naive, timezone.get_current_timezone())
            except ValueError:
                messages.error(request, "Invalid deadline format.")
                return redirect("ot-create")
                
        # Time slots logic: User selection via form checkboxes + optional custom hours
        weekday_slots_str = request.POST.getlist("weekday_slots")
        weekend_slots_str = request.POST.getlist("weekend_slots")
        weekday_custom_raw = request.POST.get("weekday_custom_hours", "").strip()
        weekend_custom_raw = request.POST.get("weekend_custom_hours", "").strip()
        
        def _parse_custom(raw):
            result = []
            for part in raw.split(","):
                part = part.strip()
                try:
                    val = float(part)
                    if val > 0:
                        result.append(val)
                except ValueError:
                    pass
            return result

        # Fallback to defaults if they unchecked everything
        weekday_slots = [float(s) for s in weekday_slots_str] if weekday_slots_str else [2.0, 4.0]
        weekday_slots += [h for h in _parse_custom(weekday_custom_raw) if h not in weekday_slots]
        weekend_slots = [float(s) for s in weekend_slots_str] if weekend_slots_str else [8.0, 10.0, 12.0]
        weekend_slots += [h for h in _parse_custom(weekend_custom_raw) if h not in weekend_slots]

        from bot.utils import WEEKEND_DAYS
        time_slots = {}
        for day in days:
            if day in WEEKEND_DAYS:
                time_slots[day] = weekend_slots.copy()
            else:
                time_slots[day] = weekday_slots.copy()
            
        event = OTEvent.objects.create(
            title=title,
            created_by_telegram_id=request.user.id, # We store user ID roughly
            days=days,
            time_slots=time_slots,
            max_agents=max_agents,
            deadline=deadline,
            group_chat_id=settings.GROUP_CHAT_ID,
        )
        
        # Fire Telegram broadcast synchronously via raw Bot in a separate thread
        from telegram import Bot
        announcement = format_announcement(event)
        
        def _run_async(coro):
            """Run an async coroutine safely in a fresh event loop."""
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(coro)
            finally:
                loop.close()
        
        async def _send():
            bot_obj = Bot(token=settings.TELEGRAM_BOT_TOKEN)
            keyboard = announcement_keyboard(settings.BOT_USERNAME, event.id)
            msg = await bot_obj.send_message(
                chat_id=settings.GROUP_CHAT_ID,
                text=announcement,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            event.announcement_message_id = msg.message_id
            await sync_to_async(event.save)()
            
        try:
            thread = threading.Thread(target=_run_async, args=(_send(),))
            thread.start()
            thread.join(timeout=10)
            messages.success(request, f"OT '{title}' published successfully to Telegram!")
        except Exception as e:
            logger.error(f"Telegram broadcast failed: {e}")
            messages.error(request, f"Saved OT but failed to broadcast to Telegram: {e}")
        
        return redirect("dashboard")
        
    # GET: allow reusing the same day across multiple OT events.
    from bot.utils import ALL_DAYS
    
    return render(request, "bot/ot_form.html", {
        "is_edit": False,
        "available_days": ALL_DAYS,
    })


@login_required
def ot_edit_view(request, pk):
    event = get_object_or_404(OTEvent, pk=pk, is_open=True)
    # Very similar logic to create but we update the event and calling edit_message_text...
    # For brevity if the user needs full edit form we can build it, but redirect for now
    messages.info(request, "Editing from Web is coming soon! Use /editot in Telegram to edit active slots safely.")
    return redirect("dashboard")


@login_required
def ot_close_view(request, pk):
    if request.method != "POST":
        return redirect("dashboard")

    # Server-side debounce/idempotency guard for accidental double-clicks.
    lock_key = f"ot_close_lock:{request.user.id}:{pk}"
    if not cache.add(lock_key, True, timeout=8):
        messages.warning(request, "Close already in progress. Duplicate submit ignored.")
        return redirect("dashboard")

    event = get_object_or_404(OTEvent, pk=pk)
    if not event.is_open:
        messages.info(request, f"OT '{event.title}' is already closed.")
        return redirect("dashboard")

    event.is_open = False
    event.save()

    # Compile signup list
    signups = list(
        OTSignup.objects.filter(ot_event=event)
        .select_related("agent")
        .order_by("day", "confirmed_at")
    )
    signup_text = format_signup_list(event, signups)

    # Build CSV bytes using the centralized utility
    from bot.utils import generate_csv
    csv_bytes = generate_csv(event, signups)
    csv_filename = f"ot_{event.id}_{event.title.replace(' ', '_')[:30]}.csv"

    def _run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()

    async def _broadcast_close():
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

        # 1. Update original announcement (if possible) + always send separate close notice
        try:
            closed_text = signup_text + "\n\n_Signups are now CLOSED._"
            final_notice = "📢 *OT CLOSED*\n\n" + closed_text
            # Edit the original announcement to mark as closed
            if event.announcement_message_id and event.group_chat_id:
                try:
                    await bot.edit_message_text(
                        chat_id=event.group_chat_id,
                        message_id=event.announcement_message_id,
                        text=closed_text,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
            await bot.send_message(
                chat_id=settings.GROUP_CHAT_ID,
                text=final_notice,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.error(f"Failed to post closure to group: {exc}")

        # 2. DM every admin with the signup list + CSV
        from io import BytesIO
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"*OT Closed from Dashboard*\n\n{signup_text}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await bot.send_document(
                    chat_id=admin_id,
                    document=BytesIO(csv_bytes),
                    filename=csv_filename,
                    caption=f"Full signup export for *{_esc(event.title)}*",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as exc:
                logger.warning(f"Could not DM admin {admin_id}: {exc}")

    try:
        thread = threading.Thread(target=_run_async, args=(_broadcast_close(),))
        thread.start()
        thread.join(timeout=15)
        messages.success(request, f"OT '{event.title}' closed. Signup list posted to group & sent to admins.")
    except Exception as e:
        logger.error(f"Close broadcast failed: {e}")
        messages.error(request, f"OT closed but broadcast failed: {e}")

    return redirect("dashboard")


@login_required
def ot_detail_view(request, pk):
    event = get_object_or_404(OTEvent, pk=pk)
    return render(request, "bot/ot_detail.html", {
        "event": event,
    })


@login_required
def user_management_view(request):
    """List signups and agents; delete actions use separate POST endpoints."""
    events = OTEvent.objects.order_by("-created_at")[:300]

    signup_qs = (
        OTSignup.objects.select_related("agent", "ot_event")
        .order_by("-confirmed_at")
    )
    event_filter = request.GET.get("event", "").strip()
    selected_event = ""
    if event_filter.isdigit():
        eid = int(event_filter)
        signup_qs = signup_qs.filter(ot_event_id=eid)
        selected_event = str(eid)

    signup_page_no = request.GET.get("signups_page", 1)
    signup_paginator = Paginator(signup_qs, 35)
    signups_page = signup_paginator.get_page(signup_page_no)

    agent_qs = Agent.objects.annotate(signup_count=Count("signups")).order_by("agent_name")
    agent_page_no = request.GET.get("agents_page", 1)
    agent_paginator = Paginator(agent_qs, 40)
    agents_page = agent_paginator.get_page(agent_page_no)

    return render(
        request,
        "bot/user_management.html",
        {
            "events": events,
            "selected_event": selected_event,
            "signups_page": signups_page,
            "agents_page": agents_page,
        },
    )


@login_required
@require_POST
def delete_signup_view(request, signup_id):
    signup = get_object_or_404(
        OTSignup.objects.select_related("agent", "ot_event"), pk=signup_id
    )
    label = (
        f"{signup.agent.agent_name} — {signup.day} "
        f"({signup.ot_event.title})"
    )
    signup.delete()
    messages.success(request, f"Removed signup: {label}")
    return redirect(_user_management_redirect_from_request(request))


@login_required
@require_POST
def delete_agent_view(request, agent_id):
    agent = get_object_or_404(Agent, pk=agent_id)
    name = agent.agent_name
    tid = agent.telegram_id
    n = agent.signups.count()
    agent.delete()
    messages.success(
        request,
        f"Deleted agent “{name}” (Telegram {tid}) and {n} signup record(s).",
    )
    return redirect(_user_management_redirect_from_request(request))


def _user_management_redirect_from_request(request):
    """Preserve filters/pagination after POST (GET is empty on form POST)."""
    posted = (request.POST.get("next_query") or "").strip()
    if posted and "://" not in posted and not posted.startswith("//"):
        return f"{reverse('user-management')}?{posted}"
    q = request.GET.urlencode()
    base = reverse("user-management")
    return f"{base}?{q}" if q else base
