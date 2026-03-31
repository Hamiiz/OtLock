import json
import logging
from datetime import datetime
from asgiref.sync import async_to_sync

from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

from telegram import Update
from telegram.constants import ParseMode

from bot.bot_app import get_ptb_application
from bot.models import OTEvent
from bot.utils import format_announcement, _esc

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
    now = timezone.now()
    # Auto-close expired events safely before querying
    OTEvent.objects.filter(is_open=True, deadline__lt=now).update(is_open=False)
    
    events = OTEvent.objects.filter(is_open=True).order_by('-created_at')
    past_events = OTEvent.objects.filter(is_open=False).order_by('-created_at')[:10]
    
    return render(request, "bot/dashboard.html", {
        "events": events,
        "past_events": past_events,
    })


@login_required
def ot_create_view(request):
    if request.method == "POST":
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
                
        # Time slots logic: User selection via form checkboxes
        weekday_slots_str = request.POST.getlist("weekday_slots")
        weekend_slots_str = request.POST.getlist("weekend_slots")
        
        # Fallback to defaults if they unchecked everything
        weekday_slots = [float(s) for s in weekday_slots_str] if weekday_slots_str else [2.0, 4.0]
        weekend_slots = [float(s) for s in weekend_slots_str] if weekend_slots_str else [8.0, 10.0, 12.0]

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
        
        # Fire Telegram broadcast synchronously via raw Bot to bypass App loop initialization 
        from telegram import Bot
        announcement = format_announcement(event)
        
        async def _send():
            bot_obj = Bot(token=settings.TELEGRAM_BOT_TOKEN)
            msg = await bot_obj.send_message(
                chat_id=settings.GROUP_CHAT_ID,
                text=announcement,
                parse_mode=ParseMode.MARKDOWN,
            )
            event.announcement_message_id = msg.message_id
            event.save()
            
        try:
            async_to_sync(_send)()
            messages.success(request, f"OT '{title}' published successfully to Telegram!")
        except Exception as e:
            logger.error(f"Telegram broadcast failed: {e}")
            messages.error(request, f"Saved OT but failed to broadcast to Telegram: {e}")
        
        return redirect("dashboard")
        
    # GET
    from bot.utils import ALL_DAYS
    # Check currently booked days
    booked = set()
    for e in OTEvent.objects.filter(is_open=True):
        booked.update(e.days)
        
    available_days = [d for d in ALL_DAYS if d not in booked]
    
    return render(request, "bot/ot_form.html", {
        "is_edit": False,
        "available_days": available_days,
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
    if request.method == "POST":
        event = get_object_or_404(OTEvent, pk=pk)
        event.is_open = False
        event.save()
        messages.success(request, f"OT Event '{event.title}' is now closed.")
    return redirect("dashboard")


@login_required
def ot_detail_view(request, pk):
    event = get_object_or_404(OTEvent, pk=pk)
    return render(request, "bot/ot_detail.html", {
        "event": event,
    })
