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
from datetime import timedelta

from asgiref.sync import sync_to_async

from telegram import Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

from django.conf import settings
from django.utils import timezone
from bot.models import OTEvent, OTSignup
from bot.utils import (
    days_keyboard,
    slot_keyboard_weekday,
    slot_keyboard_weekend,
    WEEKEND_DAYS,
    CLASS_TYPES,
    _hours_label,
    format_announcement,
    format_signup_list,
    approve_list_keyboard,
    _esc,
)

# ── Conversation states ──────────────────────────────────────────────────────
ASK_TITLE = 0
ASK_DAYS = 1
ASK_SLOTS = 2
ASK_SLOT_CUSTOM = 3
ASK_MAX = 4
ASK_DEADLINE = 5


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
    now = timezone.now()
    # Auto-close any events whose deadline has passed
    OTEvent.objects.filter(is_open=True, deadline__lt=now).update(is_open=False)
    return list(OTEvent.objects.filter(is_open=True).order_by("-created_at"))


@sync_to_async
def _get_signup_count(event_id: int) -> int:
    return OTSignup.objects.filter(ot_event_id=event_id).count()


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
def _get_event(event_id):
    return OTEvent.objects.get(pk=event_id)


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
def _delete_event(event_id: int):
    """Delete an OT event and all its signups by ID."""
    OTEvent.objects.filter(pk=event_id).prefetch_related("signups")  # eager
    # Cascade delete handles signups automatically (on_delete=CASCADE)
    OTEvent.objects.filter(pk=event_id).delete()


@sync_to_async
def _get_signup_count(event_id: int) -> int:
    return OTSignup.objects.filter(ot_event_id=event_id).count()


@sync_to_async
def _get_booked_days() -> set:
    """Return set of day names already in any open OT event."""
    days: set = set()
    for event in OTEvent.objects.filter(is_open=True):
        days.update(event.days)
    return days


# ── /newot flow ──────────────────────────────────────────────────────────────

@admin_only
async def newot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin triggers /newot. Block if an event is already open."""
    open_events = await _get_open_events()
    if open_events:
        event = open_events[0]
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = [
            [InlineKeyboardButton(
                f"Cancel & delete '{event.title}' first",
                callback_data=f"cancelot_confirm:{event.id}"
            )],
            [InlineKeyboardButton("Never mind", callback_data="cancelot_abort")],
        ]
        await update.message.reply_text(
            f"There is already an active OT event: *{_esc(event.title)}*\n\n"
            "You must cancel or close it before creating a new one.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["selected_days"] = []
    context.user_data["time_slots"] = {}
    await update.message.reply_text(
        "*New OT Event*\n\nEnter a title / description for this OT event:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_TITLE


@admin_only
async def editot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin triggers /editot to modify an active event's days and slots without cancelling it."""
    open_events = await _get_open_events()
    if not open_events:
        await update.message.reply_text("There are no active OT events to edit right now.")
        return ConversationHandler.END

    event = open_events[0]
    
    context.user_data.clear()
    context.user_data["edit_event_id"] = event.id
    context.user_data["title"] = event.title
    context.user_data["selected_days"] = list(event.days)
    context.user_data["time_slots"] = event.time_slots if isinstance(event.time_slots, dict) else dict(event.time_slots)
    context.user_data["max_agents"] = event.max_agents
    
    booked = await _get_booked_days()
    for day in event.days:
        booked.discard(day)
    
    from bot.utils import ALL_DAYS
    available = [d for d in ALL_DAYS if d not in booked]
    context.user_data["available_days"] = available
    
    await update.message.reply_text(
        f"Editing OT Event: *{_esc(event.title)}*\n"
        "Select the days for this OT event.\nTap a day to toggle it, then press Done.",
        reply_markup=days_keyboard(context.user_data["selected_days"], available),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_DAYS


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["title"] = update.message.text.strip()
    # Filter out days already booked in another open event
    booked = await _get_booked_days()
    from bot.utils import ALL_DAYS
    available = [d for d in ALL_DAYS if d not in booked]
    context.user_data["available_days"] = available
    if not available:
        await update.message.reply_text(
            "All days are already scheduled in an active OT event. "
            "Use /cancelot or /closesignup first."
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "Select the days for this OT event.\nTap a day to toggle it, then press Done.",
        reply_markup=days_keyboard(context.user_data["selected_days"], available),
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
    await query.edit_message_reply_markup(
        reply_markup=days_keyboard(selected, available)
    )
    return ASK_DAYS


async def days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected = context.user_data["selected_days"]
    if not selected:
        await query.answer("Please select at least one day!", show_alert=True)
        return ASK_DAYS

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
    await query.edit_message_reply_markup(reply_markup=kb)
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
    days = context.user_data["selected_days"]
    time_slots = context.user_data["time_slots"]
    max_agents = context.user_data["max_agents"]
    uid = update.effective_user.id

    # Fill in any days that didn't get explicit slots
    for day in days:
        if day not in time_slots:
            time_slots[day] = [8.0] if day in WEEKEND_DAYS else [2.0, 4.0]

    edit_event_id = context.user_data.get("edit_event_id")
    if edit_event_id:
        event = await _update_event(edit_event_id, title, days, time_slots, max_agents, deadline)
        announcement = format_announcement(event)
        if getattr(event, 'announcement_message_id', None):
            try:
                await context.bot.edit_message_text(
                    chat_id=settings.GROUP_CHAT_ID,
                    message_id=event.announcement_message_id,
                    text=announcement,
                    parse_mode=ParseMode.MARKDOWN,
                )
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
        event = await _create_event(title, uid, days, time_slots, max_agents, deadline)
        announcement = format_announcement(event)

        msg = await context.bot.send_message(
            chat_id=settings.GROUP_CHAT_ID,
            text=announcement,
            parse_mode=ParseMode.MARKDOWN,
        )
        await _save_message_id(event, msg.message_id)

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

async def close_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin closes the active OT event and gets the signup list for approval."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return

    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events right now.")
        return

    # Close all open events (or adjust to handle multiple events if needed)
    for event in events:
        signups = await _get_signups(event)
        await _close_event(event)
        list_text = format_signup_list(event, signups)
        approval_keyboard = approve_list_keyboard(event.id)
        await update.message.reply_text(
            f"Signup closed for *{_esc(event.title)}*.\n\n"
            f"Review the list below and tap Approve to send it to the group:\n\n"
            f"{list_text}",
            reply_markup=approval_keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )


async def approve_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin taps 'Approve & Send' – forwards signup list to group."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Not authorised.", show_alert=True)
        return

    event_id = int(query.data.split(":", 1)[1])
    event = await _get_event(event_id)
    signups = await _get_signups(event)
    list_text = format_signup_list(event, signups)

    await context.bot.send_message(
        chat_id=settings.GROUP_CHAT_ID,
        text=list_text,
        parse_mode=ParseMode.MARKDOWN,
    )

    await query.edit_message_text(
        f"Signup list for *{_esc(event.title)}* has been sent to the group!",
        parse_mode=ParseMode.MARKDOWN,
    )


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
        await update.message.reply_text(
            f"*CURRENT STATUS (Not Closed)*\n\n{list_text}",
            parse_mode=ParseMode.MARKDOWN,
        )


@admin_only
async def remove_ot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Show a list of unique agents who have signups in the active event."""
    events = await _get_open_events()
    if not events:
        await update.message.reply_text("There are no open OT events right now.")
        return

    event = events[0]
    signups = await _get_signups(event)

    if not signups:
        await update.message.reply_text(
            f"Nobody has signed up for *{_esc(event.title)}* yet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Deduplicate: one button per unique agent
    seen_agents = {}
    for signup in signups:
        agent_id = signup.agent.id
        if agent_id not in seen_agents:
            seen_agents[agent_id] = signup.agent.agent_name

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"rm_agent:{agent_id}:{event.id}")]
        for agent_id, name in seen_agents.items()
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="rm_agent:cancel")])

    await update.message.reply_text(
        f"*Remove from: {_esc(event.title)}*\n\nSelect the agent to remove:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


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
    signups = await _get_signups(event)

    if not signups:
        await query.edit_message_text("No signups remaining.")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    seen_agents = {}
    for signup in signups:
        aid = signup.agent.id
        if aid not in seen_agents:
            seen_agents[aid] = signup.agent.agent_name

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"rm_agent:{aid}:{event_id}")]
        for aid, name in seen_agents.items()
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="rm_agent:cancel")])

    await query.edit_message_text(
        f"*Remove from: {_esc(event.title)}*\n\nSelect the agent to remove:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )



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

    for event in events:
        signups = await _get_signups(event)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Agent Name", "Day", "Hours", "Class", "Signed Up At (UTC)"])
        for signup in signups:
            writer.writerow([
                signup.agent.agent_name,
                signup.day,
                float(signup.hours),
                dict(CLASS_TYPES).get(signup.class_type, signup.class_type),
                signup.confirmed_at.strftime("%Y-%m-%d %H:%M"),
            ])

        csv_bytes = output.getvalue().encode('utf-8')
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


# ── ConversationHandler factory ───────────────────────────────────────────────

def build_admin_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("newot", newot_start),
            CommandHandler("editot", editot_start),
        ],
        states={
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
        per_user=True,
        per_chat=True,
        per_message=False,
    )
