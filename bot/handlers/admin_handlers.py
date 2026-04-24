"""
Admin conversation handlers for OT Signup Bot.

Flow:
  /newot
    → ASK_TITLE       : Admin enters OT event title
    → ASK_DAYS        : Multi-select day keyboard
    → ASK_SLOTS       : Per-day slot selection (iterates through selected days)
    → ASK_MAX         : Max agents or no limit
    → (publish announcement to group)

  /closesignup
    → (sends compiled list + approval keyboard to admin in DM)
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta
from io import BytesIO

logger = logging.getLogger(__name__)

from asgiref.sync import sync_to_async

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TimedOut, NetworkError

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from bot.models import OTEvent, OTSignup
from bot.utils import (
    ALL_DAYS,
    days_keyboard,
    slot_keyboard_weekday,
    slot_keyboard_weekend,
    WEEKEND_DAYS,
    CLASS_TYPES,
    _hours_label,
    format_announcement,
    format_signup_list,
    select_event_keyboard,
    announcement_keyboard,
    split_text_for_telegram_messages,
    _esc,
)

# ── Conversation states ──────────────────────────────────────────────────────
ASK_TITLE = 0
ASK_DAYS = 1
ASK_SLOTS = 2
ASK_SLOT_CUSTOM = 3
ASK_MAX = 4
ASK_DEADLINE = 5

_DAY_INDEX = {d: i for i, d in enumerate(ALL_DAYS)}


def _sorted_days(days):
    return sorted(days, key=lambda d: _DAY_INDEX.get(d, 99))


async def _tg_call(coro_factory, retries: int = 3, delay_seconds: float = 0.8):
    """Retry Telegram API calls for transient network timeouts/errors."""
    import asyncio
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                await asyncio.sleep(delay_seconds * (attempt + 1))
                continue
            raise
    if last_exc:
        raise last_exc


# ── Dynamic admin management ─────────────────────────────────────────────────

_dynamic_admins: set[int] = set()
_admins_loaded: bool = False


async def _ensure_admins_loaded():
    global _dynamic_admins, _admins_loaded
    if not _admins_loaded:
        from bot.models import AdminUser
        ids = await sync_to_async(list)(
            AdminUser.objects.values_list('telegram_id', flat=True)
        )
        _dynamic_admins = set(ids)
        _admins_loaded = True


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS or user_id in _dynamic_admins


def admin_only(func):
    """Decorator that silently ignores non-admin users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _ensure_admins_loaded()
        uid = update.effective_user.id
        if not is_admin(uid):
            await update.message.reply_text("You are not authorised to use this command.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


@sync_to_async
def _create_event(title, telegram_id, days, time_slots, max_agents, deadline=None):
    return OTEvent.objects.create(
        title=title,
        created_by_telegram_id=telegram_id,
        days=days,
        time_slots=time_slots,
        max_agents=max_agents,
        deadline=deadline,
        group_chat_id=settings.GROUP_CHAT_ID,
    )


@sync_to_async
def _update_event(event_id, title, days, time_slots, max_agents, deadline):
    """Updates an existing OTEvent and cascade deletes any signups spanning removed days."""
    event = OTEvent.objects.get(pk=event_id)
    event.title = title
    event.days = days
    event.time_slots = time_slots
    event.max_agents = max_agents
    event.deadline = deadline
    event.save()
    
    # Remove any signups for days that were just removed
    OTSignup.objects.filter(ot_event=event).exclude(day__in=days).delete()
    return event


@sync_to_async
def _get_open_events():
    """Open OT events only. Do not silently flip is_open here — deadline/capacity flows notify admins first."""
    return list(OTEvent.objects.filter(is_open=True).order_by("-created_at"))


@sync_to_async
def _get_signup_count(event_id: int) -> int:
    return OTSignup.objects.filter(ot_event_id=event_id).count()


@sync_to_async
def _get_event(event_id):
    try:
        return OTEvent.objects.get(pk=event_id)
    except OTEvent.DoesNotExist:
        return None


@sync_to_async
def _get_signups(event):
    return list(
        OTSignup.objects.filter(ot_event=event)
        .select_related("agent")
        .order_by("day", "confirmed_at")
    )


@sync_to_async
def _close_event(event):
    event.is_open = False
    event.save()


@sync_to_async
def _save_message_id(event, msg_id):
    event.announcement_message_id = msg_id
    event.save()


@sync_to_async
def _db_add_admin(telegram_id, username, name, added_by):
    from bot.models import AdminUser
    AdminUser.objects.get_or_create(
        telegram_id=telegram_id,
        defaults={'telegram_username': username or '', 'telegram_name': name or '', 'added_by': added_by}
    )


@sync_to_async
def _db_remove_admin(telegram_id):
    from bot.models import AdminUser
    AdminUser.objects.filter(telegram_id=telegram_id).delete()


@sync_to_async
def _get_all_dynamic_admins():
    from bot.models import AdminUser
    return list(AdminUser.objects.all())


@sync_to_async
def _delete_event(event_id: int):
    """Delete an OT event and all its signups by ID."""
    OTEvent.objects.filter(pk=event_id).delete()


@sync_to_async
def _get_booked_days() -> set:
    """Return set of day names already in any open OT event."""
    days: set = set()
    for event in OTEvent.objects.filter(is_open=True):
        days.update(event.days)
    return days


# ── Admin approval before posting final list (deadline / capacity) ───────────

def _closure_prompt_cache_key(reason: str, event_id: int) -> str:
    return f"ot_admin_closure_prompt:{reason}:{event_id}"


def _group_posted_cache_key(event_id: int) -> str:
    return f"ot_group_closure_posted:{event_id}"


@sync_to_async
def _close_event_if_still_open(event_id: int) -> bool:
    """Return True if we closed an open event, False if already closed/missing."""
    updated = OTEvent.objects.filter(pk=event_id, is_open=True).update(is_open=False)
    return updated > 0


async def _post_closure_announcements_to_group(bot, event) -> None:
    """Edit original announcement (if possible) and send separate OT CLOSED notice."""
    from bot.utils import format_signup_list

    signups = await _get_signups(event)
    signup_text = format_signup_list(event, signups)
    closed_text = signup_text + "\n\n_Signups are now CLOSED._"
    final_notice = "📢 *OT CLOSED*\n\n" + closed_text
    try:
        if event.announcement_message_id and event.group_chat_id:
            try:
                await _tg_call(
                    lambda: bot.edit_message_text(
                        chat_id=event.group_chat_id,
                        message_id=event.announcement_message_id,
                        text=closed_text,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                )
            except Exception:
                pass
        await _tg_call(
            lambda: bot.send_message(
                chat_id=settings.GROUP_CHAT_ID,
                text=final_notice,
                parse_mode=ParseMode.MARKDOWN,
            )
        )
    except Exception as exc:
        logger.error(f"Failed to post closure to group for event {event.id}: {exc}")


async def begin_ot_closure_with_admin_approval(bot, event_id: int, reason: str) -> None:
    """
    Close signups for this OT, then DM all admins with list + CSV + approve/skip buttons.
    reason: 'deadline' | 'capacity'
    Idempotent per (reason, event_id) via cache.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.utils import format_signup_list, generate_csv

    ck = _closure_prompt_cache_key(reason, event_id)
    if not cache.add(ck, reason, timeout=86400 * 30):
        return

    closed = await _close_event_if_still_open(event_id)
    if not closed:
        cache.delete(ck)
        return

    event = await _get_event(event_id)
    if not event:
        cache.delete(ck)
        return

    signups = await _get_signups(event)
    signup_text = format_signup_list(event, signups)
    csv_bytes = generate_csv(event, signups)
    csv_filename = f"ot_{event.id}_{event.title.replace(' ', '_')[:30]}.csv"

    if reason == "deadline":
        title_line = "⏰ *Signup deadline reached*"
        detail = "Signups are now *closed* for this OT. Review the list and CSV, then post the final list to the group when ready."
    else:
        title_line = "🎯 *Signup limit reached*"
        detail = "This OT has reached its maximum number of signups. Review the list and CSV, then post the final list to the group when ready."

    confirm_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Post final list to group",
                    callback_data=f"approve_closure:{event_id}",
                ),
                InlineKeyboardButton(
                    "Skip (I'll handle it)",
                    callback_data=f"skip_closure:{event_id}",
                ),
            ]
        ]
    )

    await _ensure_admins_loaded()
    all_admin_ids = set(list(settings.ADMIN_IDS) + list(_dynamic_admins))

    for admin_id in all_admin_ids:
        try:

            async def _send_prompt(aid=admin_id):
                return await bot.send_message(
                    chat_id=aid,
                    text=(
                        f"{title_line}\n\n"
                        f"*{_esc(event.title)}*\n\n"
                        f"{detail}\n\n"
                        f"{signup_text}"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=confirm_kb,
                )

            await _tg_call(_send_prompt)

            async def _send_csv(aid=admin_id):
                return await bot.send_document(
                    chat_id=aid,
                    document=BytesIO(csv_bytes),
                    filename=csv_filename,
                    caption=f"📊 Export — *{_esc(event.title)}*",
                    parse_mode=ParseMode.MARKDOWN,
                )

            await _tg_call(_send_csv)
        except Exception as exc:
            logger.warning(f"Could not DM admin {admin_id} closure prompt: {exc}")

    logger.info(f"OT {event_id} closed ({reason}); admin approval prompt sent.")


async def approve_closure_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        event_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection.")
        return

    posted_key = _group_posted_cache_key(event_id)
    if not cache.add(posted_key, "1", timeout=86400 * 30):
        await query.edit_message_text("Final list was already posted to the group.")
        return

    event = await _get_event(event_id)
    if not event:
        cache.delete(posted_key)
        await query.edit_message_text("This OT event no longer exists.")
        return

    await _post_closure_announcements_to_group(context.bot, event)

    await query.edit_message_text(
        f"✅ Final list for *{_esc(event.title)}* has been posted to the group.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def skip_closure_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        event_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection.")
        return

    event = await _get_event(event_id)
    title = _esc(event.title) if event else "this OT"
    await query.edit_message_text(
        f"Skipped posting for *{title}*.\n\n"
        "Signups are already closed. You can share the CSV manually or ask another admin to post.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def scan_overdue_deadlines_on_startup(application) -> None:
    """If the bot was down when a deadline passed, notify admins once per event."""
    now = timezone.now()

    @sync_to_async
    def _overdue_ids():
        return list(
            OTEvent.objects.filter(
                is_open=True,
                deadline__isnull=False,
                deadline__lt=now,
            ).values_list("id", flat=True)
        )

    try:
        ids = await _overdue_ids()
        for eid in ids:
            await begin_ot_closure_with_admin_approval(application.bot, eid, "deadline")
    except Exception as e:
        logger.error(f"Overdue deadline scan failed: {e}")


@admin_only
async def editot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin triggers /editot to modify an existing open event."""
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events to edit.")
        return ConversationHandler.END

    if len(events) == 1:
        event = events[0]
        return await _init_edit_flow(update, context, event)

    keyboard = select_event_keyboard(events, "edit_event")
    await update.message.reply_text(
        "Select the OT event you want to edit:",
        reply_markup=keyboard,
    )
    return ASK_EDIT_SELECT


ASK_EDIT_SELECT = 20


async def edit_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        eid = int(query.data.split(":")[1])
        event = await _get_event(eid)
        if not event or not event.is_open:
            await query.edit_message_text("This event is no longer open for editing.")
            return ConversationHandler.END
        return await _init_edit_flow(update, context, event)
    except Exception:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END


async def _init_edit_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, event):
    context.user_data.clear()
    context.user_data["edit_event_id"] = event.id
    context.user_data["title"] = event.title
    context.user_data["selected_days"] = _sorted_days(list(event.days))
    context.user_data["time_slots"] = event.time_slots.copy()
    context.user_data["max_agents"] = event.max_agents
    context.user_data["is_editing"] = True

    msg = f"Editing OT: *{_esc(event.title)}*\n\nEnter the new title (or send /skip to keep current):"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return ASK_TITLE


@admin_only
async def newot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin triggers /newot.
    Multiple OT events are allowed; we just start a fresh definition flow.
    """
    context.user_data.clear()
    context.user_data["is_editing"] = False
    await update.message.reply_text(
        "Creating a new OT event.\n\nPlease enter the *title* for this OT (e.g. 'Monday Night Shift'):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_TITLE


# ── /newot flow ──────────────────────────────────────────────────────────────


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("Please enter a valid title.")
        return ASK_TITLE
    context.user_data["title"] = title

    # Allow overlapping OT schedules across different shifts/groups.
    from bot.utils import ALL_DAYS
    available = list(ALL_DAYS)
    context.user_data["available_days"] = available

    selected = context.user_data.get("selected_days")
    if selected is None:
        selected = []
        context.user_data["selected_days"] = selected
        context.user_data["time_slots"] = {}

    await update.message.reply_text(
        "Select the days for this OT event.\nTap a day to toggle it, then press Done.",
        reply_markup=days_keyboard(selected, available),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_DAYS


async def toggle_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = query.data.split(":", 1)[1]
    selected = context.user_data["selected_days"]
    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)
    available = context.user_data.get("available_days")
    await _tg_call(lambda: query.edit_message_reply_markup(
        reply_markup=days_keyboard(selected, available)
    ))
    return ASK_DAYS


async def days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected = context.user_data["selected_days"]
    if not selected:
        await query.answer("Please select at least one day!", show_alert=True)
        return ASK_DAYS

    # Normalize to calendar order regardless of click order.
    selected = _sorted_days(list(selected))
    context.user_data["selected_days"] = selected
    # Store which days still need slot configuration
    context.user_data["pending_days"] = list(selected)
    return await _ask_next_slot(query, context)


async def _ask_next_slot(query_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """Ask the admin to configure slots for the next pending day."""
    pending = context.user_data.get("pending_days", [])
    if not pending:
        # All days configured – move to max agents step
        await query_or_msg.edit_message_text(
            "Set the *maximum number of agents* who can sign up.\n"
            "Send a number, or type `0` for no limit.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_MAX

    day = pending[0]
    context.user_data["current_slot_day"] = day
    selected = context.user_data["time_slots"].setdefault(day, [])
    
    if day in WEEKEND_DAYS:
        kb = slot_keyboard_weekend(day, selected)
    else:
        kb = slot_keyboard_weekday(day, selected)

    await query_or_msg.edit_message_text(
        f"Configure time slots for *{day}*:\n(Select all options you want to offer to agents, then press Done)",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SLOTS


async def toggle_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, day, value = query.data.split(":", 2)
    hours = float(value)

    selected = context.user_data["time_slots"].setdefault(day, [])
    if hours in selected:
        selected.remove(hours)
    else:
        selected.append(hours)

    kb = slot_keyboard_weekend(day, selected) if day in WEEKEND_DAYS else slot_keyboard_weekday(day, selected)
    await _tg_call(lambda: query.edit_message_reply_markup(reply_markup=kb))
    return ASK_SLOTS


async def ask_custom_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, day = query.data.split(":", 1)
    context.user_data["current_slot_day"] = day
    await query.edit_message_text(
        f"Enter custom hours for *{day}* (e.g. `6` or `3.5`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SLOT_CUSTOM


async def slot_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin finishes slot config for a day."""
    query = update.callback_query
    _, day = query.data.split(":", 1)
    
    selected = context.user_data["time_slots"].get(day, [])
    if not selected:
        await query.answer("Please select at least one time slot!", show_alert=True)
        return ASK_SLOTS
        
    await query.answer()
    context.user_data["pending_days"].pop(0)
    return await _ask_next_slot(query, context)


async def receive_custom_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive number.")
        return ASK_SLOT_CUSTOM

    day = context.user_data["current_slot_day"]
    selected = context.user_data["time_slots"].setdefault(day, [])
    if hours not in selected:
        selected.append(hours)

    kb = slot_keyboard_weekend(day, selected) if day in WEEKEND_DAYS else slot_keyboard_weekday(day, selected)
    await update.message.reply_text(
        f"Configure time slots for *{day}*:\n(Added {hours}h custom slot)",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SLOTS


async def receive_max_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        max_agents = int(text)
        if max_agents < 0:
            raise ValueError
        max_agents = max_agents if max_agents > 0 else None
    except ValueError:
        await update.message.reply_text("Please enter a valid non-negative integer (0 = no limit).")
        return ASK_MAX

    context.user_data["max_agents"] = max_agents
    await update.message.reply_text(
        "Optional: How many *hours* until signup closes automatically?\n"
        "E.g. `24` for 1 day, `48` for 2 days.\n"
        "Type `0` or `skip` for no deadline.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_DEADLINE


async def receive_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parses deadline input, creates and publishes the OT event."""
    text = update.message.text.strip().lower()
    deadline = None

    if text not in ("0", "skip", "no"):
        try:
            hours = float(text)
            if hours <= 0:
                raise ValueError
            deadline = timezone.now() + timedelta(hours=hours)
        except ValueError:
            await update.message.reply_text(
                "Please enter a number of hours (e.g. `48`) or type `skip`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ASK_DEADLINE

    title = context.user_data["title"]
    days = _sorted_days(list(context.user_data["selected_days"]))
    time_slots = context.user_data["time_slots"]
    max_agents = context.user_data["max_agents"]
    uid = update.effective_user.id

    # Fill in any days that didn't get explicit slots
    for day in days:
        if day not in time_slots:
            time_slots[day] = [8.0] if day in WEEKEND_DAYS else [2.0, 4.0]

    # Keep slot mapping ordered by weekday for stable announcement rendering.
    ordered_time_slots = {day: time_slots[day] for day in days if day in time_slots}

    edit_event_id = context.user_data.get("edit_event_id")
    if edit_event_id:
        event = await _update_event(edit_event_id, title, days, ordered_time_slots, max_agents, deadline)
        announcement = format_announcement(event)
        keyboard = announcement_keyboard(settings.BOT_USERNAME, event.id)
        if getattr(event, 'announcement_message_id', None):
            try:
                await _tg_call(lambda: context.bot.edit_message_text(
                    chat_id=settings.GROUP_CHAT_ID,
                    message_id=event.announcement_message_id,
                    text=announcement,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                ))
            except Exception:
                pass  # Message might be completely identical or deleted by an admin manually
                
        max_str = str(max_agents) if max_agents else "Unlimited"
        deadline_str = f", closes in {text}h" if deadline else ""
        await update.message.reply_text(
            f"OT Event *{_esc(title)}* updated successfully!\n"
            f"Days: {', '.join(days)}\n"
            f"Max: {max_str}{deadline_str}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        event = await _create_event(title, uid, days, ordered_time_slots, max_agents, deadline)
        announcement = format_announcement(event)
        keyboard = announcement_keyboard(settings.BOT_USERNAME, event.id)

        msg = await _tg_call(lambda: context.bot.send_message(
            chat_id=settings.GROUP_CHAT_ID,
            text=announcement,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        ))
        await _save_message_id(event, msg.message_id)

        # Schedule a deadline alert job if a deadline was set
        if deadline and context.job_queue:
            context.job_queue.run_once(
                deadline_alert,
                when=deadline,
                data={"event_id": event.id},
                name=f"deadline_alert_{event.id}",
            )

        max_str = str(max_agents) if max_agents else "Unlimited"
        deadline_str = f", closes in {text}h" if deadline else ""
        await update.message.reply_text(
            f"OT Event *{_esc(title)}* published!\n"
            f"Days: {', '.join(days)}\n"
            f"Max: {max_str}{deadline_str}\n\n"
            f"Use /closesignup to close manually, or it will auto-close at the deadline.",
            parse_mode=ParseMode.MARKDOWN,
        )

    context.user_data.clear()
    return ConversationHandler.END


# ── Deadline alert job ────────────────────────────────────────────────────────


async def deadline_alert(context) -> None:
    """JobQueue: fires at deadline — close signups, DM admins list + CSV + post approval."""
    job_data = context.job.data
    event_id = job_data["event_id"]
    try:
        await begin_ot_closure_with_admin_approval(context.bot, event_id, "deadline")
    except Exception as e:
        logger.error(f"Failed in deadline_alert for event {event_id}: {e}")


async def cancel_newot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("OT creation cancelled.")
    return ConversationHandler.END


# ── /cancelot flow ────────────────────────────────────────────────────────────

@admin_only
async def cancel_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin runs /cancelot — shows active events with a confirm button."""
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events to cancel.")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    for event in events:
        signup_count = await sync_to_async(event.signups.count)()
        buttons = [
            [InlineKeyboardButton(
                f"Yes, delete '{event.title}' and all {signup_count} signup(s)",
                callback_data=f"cancelot_confirm:{event.id}"
            )],
            [InlineKeyboardButton("No, keep it", callback_data="cancelot_abort")],
        ]
        await update.message.reply_text(
            f"Are you sure you want to *cancel and delete* the OT event:\n\n"
            f"*{_esc(event.title)}*\n\n"
            f"This will delete all {signup_count} signup(s) and cannot be undone.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN,
        )


async def cancel_event_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation of OT event cancellation."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    if query.data == "cancelot_abort":
        await query.edit_message_text("Cancellation aborted. The OT event is still active.")
        return

    event_id = int(query.data.split(":", 1)[1])

    try:
        event = await _get_event(event_id)
        title = event.title
        ann_msg_id = event.announcement_message_id
        ann_chat_id = event.group_chat_id

        await _delete_event(event_id)

        # Try to edit the group announcement to mark as cancelled
        if ann_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=ann_chat_id,
                    message_id=ann_msg_id,
                    text="CANCELLED\n\n(This OT event has been cancelled by an admin.)",
                )
            except Exception:
                pass

        await query.edit_message_text(
            f"OT event *{_esc(title)}* has been deleted along with all its signups.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as e:
        await query.edit_message_text(f"Failed to delete event: {e}")


# ── /closesignup flow ────────────────────────────────────────────────────────

@admin_only
async def close_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin selects one open OT event to close."""
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events right now.")
        return

    if len(events) == 1:
        event = events[0]
        await _send_close_confirmation_prompt(update.message, event)
        return

    keyboard = select_event_keyboard(events, "close_event")
    await update.message.reply_text(
        "Select which OT event to close:",
        reply_markup=keyboard,
    )


async def close_signup_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler for selecting a specific event to close."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        event_id = int(query.data.split(":", 1)[1])
        event = await _get_event(event_id)
    except Exception:
        await query.edit_message_text("Invalid event selection.")
        return

    if not event or not event.is_open:
        await query.edit_message_text("This OT event is already closed.")
        return

    await _send_close_confirmation_prompt(query, event, use_edit=True)


async def _send_close_confirmation_prompt(target, event, use_edit: bool = False):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Yes, close now",
                    callback_data=f"close_confirm:{event.id}",
                ),
                InlineKeyboardButton("Cancel", callback_data="close_abort"),
            ]
        ]
    )
    text = (
        f"Are you sure you want to close *{_esc(event.title)}* now?\n\n"
        "This will post the closed list to group and send CSV to admins."
    )
    if use_edit:
        await target.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    else:
        await target.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )


async def close_signup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final confirmation for closing an OT event."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    if query.data == "close_abort":
        await query.edit_message_text("Close cancelled. OT remains open.")
        return

    try:
        event_id = int(query.data.split(":", 1)[1])
        event = await _get_event(event_id)
    except Exception:
        await query.edit_message_text("Invalid close request.")
        return

    if not event or not event.is_open:
        await query.edit_message_text("This OT event is already closed.")
        return

    await _close_signup_for_event(event, context)
    await query.edit_message_text(
        f"✅ *{_esc(event.title)}* has been closed. Results posted to group and CSV sent to admins.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _close_signup_for_event(event, context):
    """Close one OT event, post final list to group, DM CSV to admins."""
    signups = await _get_signups(event)
    await _close_event(event)

    from bot.utils import generate_csv, _esc
    csv_bytes = generate_csv(event, signups)
    csv_filename = f"ot_{event.id}_{event.title.replace(' ', '_')[:30]}.csv"

    await _post_closure_announcements_to_group(context.bot, event)

    await _ensure_admins_loaded()
    all_admin_ids = set(list(settings.ADMIN_IDS) + list(_dynamic_admins))
    for admin_id in all_admin_ids:
        try:

            async def _send_export(aid=admin_id):
                return await context.bot.send_document(
                    chat_id=aid,
                    document=BytesIO(csv_bytes),
                    filename=csv_filename,
                    caption=f"📊 Export for *{_esc(event.title)}*",
                    parse_mode=ParseMode.MARKDOWN,
                )

            await _tg_call(_send_export)
        except Exception as exc:
            logger.warning(f"Could not DM admin {admin_id}: {exc}")


# ── Extra Admin Commands ──────────────────────────────────────────────────────

@admin_only
async def status_ot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin checks the current signup list without closing the event."""
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events right now.")
        return

    for event in events:
        signups = await _get_signups(event)
        list_text = format_signup_list(event, signups)
        full = f"*CURRENT STATUS (Not Closed)*\n\n{list_text}"
        for part in split_text_for_telegram_messages(full):
            await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)


@admin_only
async def remove_ot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: If multiple OTs are open, let admin pick which one to remove from."""
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events right now.")
        return

    if len(events) == 1:
        # Only one OT — go straight to agent list
        await _show_remove_agent_list(update.message, events[0])
        return

    # Multiple OTs — show picker
    keyboard = select_event_keyboard(events, "rm_event")
    await update.message.reply_text(
        "Select the OT you want to remove an agent from:",
        reply_markup=keyboard,
    )


async def remove_select_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: admin picked which OT to remove from."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        event_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection.")
        return

    event = await _get_event(event_id)
    if not event or not event.is_open:
        await query.edit_message_text("That OT is no longer open.")
        return

    await _show_remove_agent_list(query, event)


async def _show_remove_agent_list(query_or_msg, event):
    """Render the inline agent-selection keyboard for a given OT event."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    signups = await _get_signups(event)
    if not signups:
        text = f"Nobody has signed up for *{_esc(event.title)}* yet."
        if hasattr(query_or_msg, "edit_message_text"):
            await query_or_msg.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await query_or_msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    seen_agents = {}
    for signup in signups:
        aid = signup.agent.id
        if aid not in seen_agents:
            seen_agents[aid] = signup.agent.agent_name

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"rm_agent:{aid}:{event.id}")]
        for aid, name in seen_agents.items()
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="rm_agent:cancel")])

    kb = InlineKeyboardMarkup(buttons)
    text = f"*Remove from: {_esc(event.title)}*\n\nSelect the agent to remove:"
    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await query_or_msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def remove_agent_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Agent was selected — show their individual day signups for removal."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    parts = query.data.split(":")
    if parts[1] == "cancel":
        await query.edit_message_text("Removal cancelled.")
        return

    agent_id = int(parts[1])
    event_id = int(parts[2])

    from bot.models import Agent, OTSignup
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.utils import _hours_label, CLASS_TYPES

    agent = await sync_to_async(Agent.objects.get)(pk=agent_id)
    event = await _get_event(event_id)
    signups = await sync_to_async(list)(
        OTSignup.objects.filter(agent=agent, ot_event=event).order_by("day")
    )

    if not signups:
        await query.edit_message_text(f"No signups found for {_esc(agent.agent_name)}.")
        return

    buttons = []
    for signup in signups:
        hrs = float(signup.hours)
        label = _hours_label(signup.day, hrs)
        class_label = dict(CLASS_TYPES).get(signup.class_type, signup.class_type)
        text = f"Remove: {signup.day} ({label}, {class_label})"
        buttons.append([InlineKeyboardButton(text, callback_data=f"rm_day:{signup.id}:{agent_id}:{event_id}")])

    buttons.append([InlineKeyboardButton("Back to agents", callback_data=f"rm_agent_back:{event_id}")])

    await query.edit_message_text(
        f"*{_esc(agent.agent_name)}* — select the day(s) to remove:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def remove_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: A specific day signup was tapped — delete it and refresh the day list."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    parts = query.data.split(":")
    signup_id = int(parts[1])
    agent_id = int(parts[2])
    event_id = int(parts[3])

    from bot.models import Agent, OTSignup
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.utils import _hours_label, CLASS_TYPES

    try:
        signup = await sync_to_async(OTSignup.objects.select_related("agent", "ot_event").get)(id=signup_id)
        day = signup.day
        agent_name = signup.agent.agent_name
        await sync_to_async(signup.delete)()
    except OTSignup.DoesNotExist:
        await query.answer("Already deleted.", show_alert=True)
        return

    # Refresh remaining signups for this agent
    agent = await sync_to_async(Agent.objects.get)(pk=agent_id)
    event = await _get_event(event_id)
    remaining = await sync_to_async(list)(
        OTSignup.objects.filter(agent=agent, ot_event=event).order_by("day")
    )

    if not remaining:
        await query.edit_message_text(
            f"Removed *{_esc(agent_name)}* ({day}).\n\nNo more signups for this agent.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    buttons = []
    for s in remaining:
        hrs = float(s.hours)
        label = _hours_label(s.day, hrs)
        class_label = dict(CLASS_TYPES).get(s.class_type, s.class_type)
        text = f"Remove: {s.day} ({label}, {class_label})"
        buttons.append([InlineKeyboardButton(text, callback_data=f"rm_day:{s.id}:{agent_id}:{event_id}")])

    buttons.append([InlineKeyboardButton("Back to agents", callback_data=f"rm_agent_back:{event_id}")])

    await query.edit_message_text(
        f"Removed *{_esc(agent_name)}* ({day}).\n\n*{_esc(agent.agent_name)}* — remaining signups:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def remove_back_to_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to the agent list from the day-selection screen."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    event_id = int(query.data.split(":")[1])
    event = await _get_event(event_id)
    await _show_remove_agent_list(query, event)



# ── ConversationHandler factory ───────────────────────────────────────────────

# ── /summary, /export ────────────────────────────────────────────────────────

@admin_only
async def summary_ot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin gets a quick breakdown of hours and slots per day and class."""
    await _ensure_admins_loaded()
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("No open OT events.")
        return

    from collections import defaultdict
    for event in events:
        signups = await _get_signups(event)
        if not signups:
            await update.message.reply_text(f"No signups for *{_esc(event.title)}* yet.", parse_mode=ParseMode.MARKDOWN)
            continue

        day_data = defaultdict(lambda: {'count': 0, 'hours': 0.0})
        class_hours = defaultdict(float)
        total_hours = 0.0

        for signup in signups:
            hrs = float(signup.hours)
            day_data[signup.day]['count'] += 1
            day_data[signup.day]['hours'] += hrs
            class_hours[signup.class_type] += hrs
            total_hours += hrs

        unique_agents = len({s.agent_id for s in signups})
        lines = [f"*OT SUMMARY - {_esc(event.title)}*\n"]
        lines.append(f"Agents: {unique_agents}  |  Slots: {len(signups)}  |  Total: {total_hours:.1f}h\n")
        lines.append("*By Day:*")
        for day in event.days:
            if day in day_data:
                d = day_data[day]
                lines.append(f"  {day}: {d['count']} agent(s), {d['hours']:.1f}h")
        lines.append("\n*By Class:*")
        for code, label in CLASS_TYPES:
            if code in class_hours:
                lines.append(f"  {label}: {class_hours[code]:.1f}h")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@admin_only
async def export_ot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin exports the signup list as a CSV file."""
    await _ensure_admins_loaded()
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("No open OT events to export.")
        return

    from bot.utils import generate_csv

    for event in events:
        signups = await _get_signups(event)
        csv_bytes = generate_csv(event, signups)
        filename = f"OT_{event.title.replace(' ', '_')}_{event.created_at.strftime('%Y%m%d')}.csv"
        bio = io.BytesIO(csv_bytes)
        bio.name = filename

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            filename=filename,
            caption=f"OT Export: {event.title} ({len(signups)} signup(s))",
        )


# ── Admin management ──────────────────────────────────────────────────────────

@admin_only
async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new admin by replying to their message or passing their ID."""
    await _ensure_admins_loaded()

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        target_id = target.id
        target_username = target.username or ""
        target_name = target.full_name or str(target_id)
    elif context.args:
        try:
            target_id = int(context.args[0])
            target_username = ""
            target_name = str(target_id)
        except ValueError:
            await update.message.reply_text("Usage: reply to someone's message, or /addadmin <telegram_id>")
            return
    else:
        await update.message.reply_text("Usage: reply to someone's message, or /addadmin <telegram_id>")
        return

    if target_id in settings.ADMIN_IDS:
        await update.message.reply_text("This user is already a super admin.")
        return
    if target_id in _dynamic_admins:
        await update.message.reply_text(f"User {target_id} is already an admin.")
        return

    await _db_add_admin(target_id, target_username, target_name, update.effective_user.id)
    _dynamic_admins.add(target_id)

    display = f"@{target_username}" if target_username else target_name
    await update.message.reply_text(
        f"Admin added: *{_esc(display)}* (ID: `{target_id}`)",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def remove_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of dynamically added admins to remove."""
    await _ensure_admins_loaded()
    dynamic = await _get_all_dynamic_admins()

    if not dynamic:
        await update.message.reply_text(
            "No bot-managed admins to remove.\n"
            "Super admins from the environment cannot be removed via bot."
        )
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = []
    for a in dynamic:
        label = f"@{a.telegram_username}" if a.telegram_username else (a.telegram_name or str(a.telegram_id))
        buttons.append([InlineKeyboardButton(f"Remove: {label}", callback_data=f"rmadmin:{a.telegram_id}")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="rmadmin:cancel")])

    await update.message.reply_text(
        "Select the admin to remove:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    action = query.data.split(":", 1)[1]
    if action == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    target_id = int(action)
    await _db_remove_admin(target_id)
    _dynamic_admins.discard(target_id)
    await query.edit_message_text(f"Admin {target_id} has been removed.")


@admin_only
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all current admins."""
    await _ensure_admins_loaded()
    lines = ["*Current Admins:*\n", "*Super Admins (env):*"]
    for aid in settings.ADMIN_IDS:
        lines.append(f"  - `{aid}`")

    dynamic = await _get_all_dynamic_admins()
    if dynamic:
        lines.append("\n*Bot-managed Admins:*")
        for a in dynamic:
            display = f"@{a.telegram_username}" if a.telegram_username else (a.telegram_name or str(a.telegram_id))
            lines.append(f"  - {_esc(display)} (`{a.telegram_id}`)")
    else:
        lines.append("\nNo bot-managed admins.")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cancel_newot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the /newot or /editot flow."""
    context.user_data.clear()
    msg = "OT creation/edit cancelled."
    if update.callback_query:
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)
    return ConversationHandler.END


# ── /disableday command ───────────────────────────────────────────────────────

@sync_to_async
def _set_disabled_days(event_id: int, disabled_days: list) -> None:
    """Persist the disabled_days list for a single OT event."""
    OTEvent.objects.filter(pk=event_id).update(disabled_days=disabled_days)


def _disableday_keyboard(event, pending_disabled: list) -> InlineKeyboardMarkup:
    """Build a toggle keyboard: ✅ = open, 🔒 = signups disabled."""
    disabled_set = set(pending_disabled)
    buttons = []
    for day in event.days:
        icon = "🔒" if day in disabled_set else "✅"
        buttons.append([
            InlineKeyboardButton(
                f"{icon} {day}",
                callback_data=f"disableday_toggle:{event.id}:{day}",
            )
        ])
    buttons.append([
        InlineKeyboardButton("✔️ Done", callback_data=f"disableday_done:{event.id}"),
        InlineKeyboardButton("Cancel", callback_data="disableday_cancel"),
    ])
    return InlineKeyboardMarkup(buttons)


@admin_only
async def disableday_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin runs /disableday — show event picker or go straight to day toggle."""
    await _ensure_admins_loaded()
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events right now.")
        return

    if len(events) == 1:
        event = events[0]
        context.user_data["disableday_pending"] = list(event.disabled_days or [])
        await update.message.reply_text(
            f"Toggle days for *{_esc(event.title)}*:\n"
            "✅ = open to signups  |  🔒 = signups disabled",
            reply_markup=_disableday_keyboard(event, context.user_data["disableday_pending"]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Multiple OT events — show picker first
    keyboard = select_event_keyboard(events, "disableday_event")
    await update.message.reply_text(
        "Select the OT event to manage days for:",
        reply_markup=keyboard,
    )


async def disableday_select_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin picked an event from the multi-OT picker for /disableday."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        event_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection.")
        return

    event = await _get_event(event_id)
    if not event or not event.is_open:
        await query.edit_message_text("That OT event is no longer open.")
        return

    context.user_data["disableday_pending"] = list(event.disabled_days or [])
    await query.edit_message_text(
        f"Toggle days for *{_esc(event.title)}*:\n"
        "✅ = open to signups  |  🔒 = signups disabled",
        reply_markup=_disableday_keyboard(event, context.user_data["disableday_pending"]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def disableday_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle a single day's disabled state (in-memory, not yet saved)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        _prefix, event_id_str, day = query.data.split(":", 2)
        event_id = int(event_id_str)
    except (ValueError, TypeError):
        await query.answer("Invalid data.", show_alert=True)
        return

    event = await _get_event(event_id)
    if not event or not event.is_open:
        await query.edit_message_text("This OT event is no longer open.")
        return

    pending = context.user_data.setdefault("disableday_pending", list(event.disabled_days or []))
    if day in pending:
        pending.remove(day)
    else:
        pending.append(day)

    try:
        await query.edit_message_reply_markup(
            reply_markup=_disableday_keyboard(event, pending)
        )
    except Exception:
        pass  # No-op if keyboard is identical


async def disableday_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the toggled disabled_days to the DB and confirm."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    try:
        event_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection.")
        return

    event = await _get_event(event_id)
    if not event or not event.is_open:
        await query.edit_message_text("This OT event is no longer open.")
        return

    pending = context.user_data.pop("disableday_pending", [])
    await _set_disabled_days(event_id, pending)

    open_days = [d for d in event.days if d not in pending]
    locked_days = [d for d in event.days if d in pending]

    parts = []
    if open_days:
        parts.append("✅ *Open:* " + ", ".join(open_days))
    if locked_days:
        parts.append("🔒 *Disabled:* " + ", ".join(locked_days))

    await query.edit_message_text(
        f"*{_esc(event.title)}* — day status updated:\n\n" + "\n".join(parts),
        parse_mode=ParseMode.MARKDOWN,
    )


async def disableday_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel without saving."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("disableday_pending", None)
    await query.edit_message_text("Cancelled. No changes were saved.")


# ── ConversationHandler factory ───────────────────────────────────────────────

def build_admin_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("newot", newot_start, filters=filters.ChatType.PRIVATE),
            CommandHandler("editot", editot_start, filters=filters.ChatType.PRIVATE),
        ],
        states={
            ASK_EDIT_SELECT: [CallbackQueryHandler(edit_select_callback, pattern=r"^edit_event:")],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            ASK_DAYS: [
                CallbackQueryHandler(toggle_day, pattern=r"^day_toggle:"),
                CallbackQueryHandler(days_done, pattern=r"^days_done$"),
            ],
            ASK_SLOTS: [
                CallbackQueryHandler(toggle_slot, pattern=r"^slot_toggle:"),
                CallbackQueryHandler(ask_custom_slot, pattern=r"^slot_custom:"),
                CallbackQueryHandler(slot_done, pattern=r"^slot_done:"),
            ],
            ASK_SLOT_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_slot)
            ],
            ASK_MAX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_max_agents)
            ],
            ASK_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_deadline)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_newot)],
        # Without this, /newot and /editot are ignored mid-flow (entry points only run when state is None).
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )
