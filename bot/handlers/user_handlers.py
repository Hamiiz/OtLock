"""
User conversation handlers for OT Signup Bot.

Flow:
  /start (in private chat)
    → PICK_DAYS  : Multi-select toggle of available days
    → PICK_HOURS : Per-day hours selection (loops through selected days)
    → PICK_CLASS : User selects class type (once, applied to all signups)
    → CONFIRM    : Summary of all selections – cannot undo after 
"""
from __future__ import annotations

import re
import secrets

from asgiref.sync import sync_to_async

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

from bot.models import Agent, OTEvent, OTSignup
from bot.utils import (
    user_event_reply_keyboard,
    user_days_reply_keyboard,
    user_hours_reply_keyboard,
    user_class_reply_keyboard,
    user_confirm_reply_keyboard,
    user_day_multi_keyboard,
    user_hour_keyboard,
    class_keyboard,
    confirm_keyboard,
    CLASS_TYPES,
    ALL_DAYS,
    _hours_label,
    _esc,
)

# ── Conversation states ──────────────────────────────────────────────────────
PICK_EVENT = 4
PICK_DAYS = 0
PICK_HOURS = 1
PICK_CLASS = 2
CONFIRM = 3

_DAY_INDEX = {d: i for i, d in enumerate(ALL_DAYS)}


# ── DB helpers ───────────────────────────────────────────────────────────────

@sync_to_async
def _get_or_create_agent(telegram_id, telegram_username, agent_name):
    agent, _ = Agent.objects.update_or_create(
        telegram_id=telegram_id,
        defaults={"telegram_username": telegram_username, "agent_name": agent_name},
    )
    return agent


@sync_to_async
def _get_agent(telegram_id):
    try:
        return Agent.objects.get(telegram_id=telegram_id)
    except Agent.DoesNotExist:
        return None


@sync_to_async
def _agent_by_pk(pk):
    if not pk:
        return None
    return Agent.objects.filter(pk=pk).first()


@sync_to_async
def _get_open_events():
    """Return all currently open OT events."""
    return list(OTEvent.objects.filter(is_open=True).order_by("-created_at"))


@sync_to_async
def _get_event(event_id):
    try:
        return OTEvent.objects.get(pk=event_id, is_open=True)
    except OTEvent.DoesNotExist:
        return None


@sync_to_async
def _get_event_row_by_pk(event_id):
    """Return OT row if it exists (open or closed). None if deleted."""
    if not event_id:
        return None
    return OTEvent.objects.filter(pk=event_id).first()


@sync_to_async
def _already_signed_up_day(agent, event, day):
    """True if agent already has a signup for this event+day combination."""
    return OTSignup.objects.filter(agent=agent, ot_event=event, day=day).exists()


@sync_to_async
def _any_signup_for_event(agent, event):
    """True if agent has any signup at all for this event."""
    return OTSignup.objects.filter(agent=agent, ot_event=event).exists()


@sync_to_async
def _get_days_taken_in_other_open_events(agent, event):
    """Return a set of day names already booked in open OT events OTHER than *event*."""
    return set(
        OTSignup.objects.filter(agent=agent, ot_event__is_open=True)
        .exclude(ot_event=event)
        .values_list("day", flat=True)
    )


@sync_to_async
def _get_signed_days_for_event(agent, event):
    """Return a set of day names already booked for this specific event."""
    return set(
        OTSignup.objects.filter(agent=agent, ot_event=event)
        .values_list("day", flat=True)
    )


@sync_to_async
def _create_signup(agent, event, day, hours, class_type):
    from django.db import transaction
    with transaction.atomic():
        # Re-fetch the event with a row-level lock to prevent race conditions
        # on max_agents when two users confirm at the same moment.
        try:
            ev = OTEvent.objects.select_for_update().get(pk=event.pk)
        except OTEvent.DoesNotExist:
            return None, "gone"
        if not ev.is_open:
            return None, "closed"
        # Enforce one-open-OT-per-user rule at commit time as well.
        # This closes race windows between /start and final confirmation.
        duplicate_day = OTSignup.objects.filter(
            agent=agent,
            ot_event__is_open=True
        ).exclude(ot_event=ev).filter(day=day).exists()
        if duplicate_day:
            return None, "duplicate_day"
        if ev.is_full():
            return None, "full"   # caller handles the "event is full" case
        signup, created = OTSignup.objects.get_or_create(
            agent=agent,
            ot_event=ev,
            day=day,
            defaults=dict(hours=hours, class_type=class_type),
        )
        return signup, "created" if created else "exists"


@sync_to_async
def _event_is_full(event):
    return event.is_full()


async def _end_signup_session(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    use_edit: bool = True,
) -> int:
    """Clear signup state and end conversation without crashing other handlers."""
    context.user_data.clear()
    try:
        if query and use_edit:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        elif query and query.message:
            await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        try:
            if query and query.message:
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
    return ConversationHandler.END


def _signup_state_ok(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("event_id"))


def _new_wizard_session_id() -> str:
    # 8 hex chars, fits well into callback_data (and regex checks in handlers).
    return secrets.token_hex(4)


async def _require_wizard_session(query, context: ContextTypes.DEFAULT_TYPE, callback_session_id: str):
    expected = context.user_data.get("wizard_session_id")
    if not expected or callback_session_id != expected:
        try:
            await query.answer("This selection is outdated.", show_alert=True)
        except Exception:
            pass
        return await _end_signup_session(
            query,
            context,
            "This signup session is no longer valid. Please send /start again.",
        )
    return True


def _sorted_days(days):
    return sorted(days, key=lambda d: _DAY_INDEX.get(d, 99))


def _sorted_day_hours(day_hours):
    return sorted(day_hours.items(), key=lambda kv: _DAY_INDEX.get(kv[0], 99))


# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point – /start or first message in private chat."""
    context.user_data.clear()
    context.user_data["wizard_session_id"] = _new_wizard_session_id()

    # 1. Check for deep-link argument
    event = None
    if context.args:
        arg = context.args[0]
        # Accept both current and older deep-link payloads.
        try:
            if arg.startswith("signup_"):
                eid = int(arg.split("_", 1)[1])
                event = await _get_event(eid)
            elif arg.startswith("ot_"):
                eid = int(arg.split("_", 1)[1])
                event = await _get_event(eid)
        except (IndexError, ValueError):
            event = None

    # 2. Pre-load the agent (used below and passed into the signup flow)
    existing_agent = await _get_agent(update.effective_user.id)

    # 3. Handle event selection
    if not event:
        events = await _get_open_events()
        if not events:
            await update.message.reply_text(
                "There is no active OT signup at the moment.\n"
                "Watch the group for announcements!"
            )
            return ConversationHandler.END
        
        if len(events) == 1:
            event = events[0]
        else:
            # Multi-OT: show picker
            context.user_data["open_events"] = events
            keyboard = user_event_reply_keyboard(events)
            await update.message.reply_text(
                "Multiple OT shifts are active.\n"
                "Pick the exact OT for your shift from the list below:",
                reply_markup=keyboard
            )
            return PICK_EVENT

    # 4. Proceed with selected event
    if await _event_is_full(event):
        await update.message.reply_text(
            f"The OT signup for *{_esc(event.title)}* is currently full.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    context.user_data["event_id"] = event.id
    return await _start_signup_flow(update, context, event)


async def select_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for when a user picks an event from the multi-OT picker."""
    query = update.callback_query
    await query.answer()
    
    data = query.data  # "user_signup:<session>:<event_id>"
    try:
        _prefix, callback_session_id, event_id_str = data.split(":", 2)
        event_id = int(event_id_str)
        session_ok = await _require_wizard_session(
            query,
            context,
            callback_session_id,
        )
        if session_ok is not True:
            return session_ok

        event = await _get_event(event_id)
        if not event:
            context.user_data.clear()
            try:
                await query.edit_message_text("This OT event is no longer active.")
            except Exception:
                pass
            return ConversationHandler.END

        if await _event_is_full(event):
            context.user_data.clear()
            try:
                await query.edit_message_text(
                    f"The OT signup for *{_esc(event.title)}* is currently full.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            return ConversationHandler.END

        context.user_data["event_id"] = event.id
        # We need to manually call the next step because start() logic didn't finish
        return await _start_signup_flow(update, context, event)
    except ValueError:
        context.user_data.clear()
        try:
            await query.edit_message_text("Invalid selection.")
        except Exception:
            pass
        return ConversationHandler.END


async def _start_signup_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, event):
    """Internal helper for shared logic when starting the actual signup steps.

    Agents are now allowed to sign up for multiple open OTs as long as they
    don't pick the same *day* in more than one OT.  Days already booked in
    another open OT are shown as 🚫 in the keyboard and cannot be selected.

    Agents may also re-enter this flow for an OT they have already partially
    signed up for in order to add more days.
    """
    context.user_data.setdefault("wizard_session_id", _new_wizard_session_id())

    existing_agent = await _get_agent(update.effective_user.id)

    async def _send_text(text, **kwargs):
        if update.callback_query:
            await update.callback_query.message.reply_text(text, **kwargs)
        else:
            await update.message.reply_text(text, **kwargs)

    if existing_agent:
        context.user_data["agent_name"] = existing_agent.agent_name
        context.user_data["agent_id"] = existing_agent.pk
        context.user_data["agent_known"] = True

        # Days already booked in OTHER open OTs — these are off-limits for this OT.
        disabled_days = await _get_days_taken_in_other_open_events(existing_agent, event)
        # Days already confirmed for THIS event (so we don't re-show them as selectable).
        already_signed_days = await _get_signed_days_for_event(existing_agent, event)

        # Available = event days not yet signed for this OT; selectable = available minus blocked.
        available_for_selection = [d for d in event.days if d not in already_signed_days]
        selectable_days = [d for d in available_for_selection if d not in disabled_days]

        if not selectable_days and not already_signed_days:
            # Nothing usable left at all
            msg = (
                f"All available days in *{_esc(event.title)}* are already booked "
                "in your other OT signups. You cannot sign up for this OT."
            )
            await _send_text(msg, parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

        context.user_data["selected_days"] = []
        context.user_data["day_hours"] = {}
        context.user_data["event_days"] = selectable_days
        context.user_data["disabled_days"] = list(disabled_days)
        context.user_data["signup_time_slots"] = dict(event.time_slots or {})

        already_note = ""
        if already_signed_days:
            already_note = (
                f"\n_(Already signed: {', '.join(_sorted_days(list(already_signed_days)))})_"
            )
        blocked_note = ""
        if disabled_days:
            blocked_note = (
                f"\n_Blocked (booked in another OT): {', '.join(_sorted_days(list(disabled_days)))}_"
            )

        await _send_text(
            f"Welcome back, *{_esc(existing_agent.agent_name)}*!\n\n"
            f"Signing up for: *{_esc(event.title)}*{already_note}{blocked_note}\n\n"
            "Select the days you want to work OT.\n"
            "Tap a day to toggle it, then press Done.",
            reply_markup=user_days_reply_keyboard(selectable_days),
            parse_mode=ParseMode.MARKDOWN,
        )
        return PICK_DAYS

    # ── New agent: ask for name first ──────────────────────────────────────
    context.user_data["agent_known"] = False
    context.user_data["selected_days"] = []
    context.user_data["day_hours"] = {}
    context.user_data["event_days"] = event.days
    context.user_data["disabled_days"] = []
    context.user_data["signup_time_slots"] = dict(event.time_slots or {})
    await _send_text(
        f"Welcome! You're signing up for *{_esc(event.title)}*.\n\n"
        "Please enter your *agent name* exactly as it appears in your roster:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return _ASK_NAME

_ASK_NAME = 10  # Extra state only used for new agents


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agent_name = update.message.text.strip()[:50]  # silently truncate at 50 chars
    if not agent_name:
        await update.message.reply_text("Please enter a valid name.")
        return _ASK_NAME

    # Reject stale OT-picker button presses (e.g. "OT 5 | Night OT shift").
    # This happens when Telegram's persistent reply keyboard still shows the
    # event-selection buttons from a previous step and the user taps one while
    # the bot is waiting for their agent name.
    if re.match(r"^OT\s+\d+", agent_name, re.IGNORECASE):
        await update.message.reply_text(
            "That looks like an OT selection, not a name. "
            "Please type your *agent name* exactly as it appears in the roster:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return _ASK_NAME

    event_days = context.user_data.get("event_days")
    if not event_days:
        await update.message.reply_text(
            "Your session expired or this chat was reset. Please tap *Sign Up* on the latest OT announcement or send /start.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["agent_name"] = agent_name
    user = update.effective_user
    agent = await _get_or_create_agent(user.id, user.username or "", agent_name)
    context.user_data["agent_id"] = agent.pk
    disabled_days = context.user_data.get("disabled_days") or []
    event_days = context.user_data.get("event_days") or []
    blocked_note = ""
    if disabled_days:
        blocked_note = (
            f"\n_(Blocked in another OT: {', '.join(disabled_days)})_"
        )
    await update.message.reply_text(
        f"Name saved: *{_esc(agent_name)}*{blocked_note}\n\n"
        "Select the days you want to work OT.\n"
        "Tap a day to toggle it, then press Done.",
        reply_markup=user_days_reply_keyboard(event_days),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_DAYS


def _parse_day_from_text(text: str, allowed_days: list[str]) -> str | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    # Normalize by exact match (case-insensitive) against allowed day names.
    for d in allowed_days:
        if d.lower() == t:
            return d
    return None


def _parse_hours_from_text(text: str) -> float | None:
    if not text:
        return None
    t = text.strip().lower()
    # Expected formats: "2", "2.0", "2 hrs", "2.0hrs"
    t = t.replace("hrs", "").replace("hr", "").strip()
    # Keep only digits and dot.
    cleaned = "".join(ch for ch in t if ch.isdigit() or ch == ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_class_from_text(text: str) -> str | None:
    if not text:
        return None
    label_to_code = {label.lower(): code for code, label in CLASS_TYPES}
    t = text.strip().lower()
    return label_to_code.get(t)


async def pick_event_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Message-based OT selection (no inline callbacks)."""
    text = update.message.text or ""
    # Expected: "OT <id> | <title>" or just "OT <id>"
    import re
    match = re.search(r"^OT\s+(\d+)", text.strip(), re.IGNORECASE)
    if not match:
        await update.message.reply_text("Select an OT using the buttons above.")
        return PICK_EVENT
    try:
        eid = int(match.group(1))
    except ValueError:
        await update.message.reply_text("Invalid OT selection. Try again.")
        return PICK_EVENT

    event = await _get_event(eid)
    if not event:
        context.user_data.clear()
        await update.message.reply_text(
            "This OT event is no longer active. Send /start to see current OTs."
        )
        return ConversationHandler.END

    context.user_data["event_id"] = event.id
    # Proceed with signup wizard.
    return await _start_signup_flow(update, context, event)


async def pick_days_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Day toggle + completion ('Done') in message-based flow."""
    if not _signup_state_ok(context) or "event_days" not in context.user_data:
        await update.message.reply_text(
            "This signup session expired. Send /start again."
        )
        context.user_data.clear()
        return ConversationHandler.END

    allowed_days: list[str] = context.user_data.get("event_days") or []
    selected = context.user_data.setdefault("selected_days", [])
    text = update.message.text.strip()

    if text.lower() == "done":
        if not selected:
            await update.message.reply_text("Select at least one day before pressing Done.")
            return PICK_DAYS
        context.user_data["pending_days"] = _sorted_days(list(selected))
        # Ask for hours for the first pending day.
        return await _ask_next_hours_message(update, context)

    day = _parse_day_from_text(text, allowed_days)
    if not day:
        await update.message.reply_text(
            "Invalid day. Tap one of the day buttons, or press Done when finished."
        )
        return PICK_DAYS

    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)

    ordered = _sorted_days(list(selected))
    pretty = ", ".join(ordered) if ordered else "(none yet)"
    await update.message.reply_text(
        f"Selected days: {pretty}\n\nTap more days or press Done.",
        reply_markup=user_days_reply_keyboard(allowed_days),
    )
    return PICK_DAYS


async def _ask_next_hours_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_days") or []
    if not pending:
        return await _ask_class_message(update, context)

    day = pending[0]
    context.user_data["current_hours_day"] = day

    ts = context.user_data.get("signup_time_slots") or {}
    slots = ts.get(day, []) or []
    # If admin left day with no slots, skip it.
    if not slots:
        pending.pop(0)
        context.user_data["pending_days"] = pending
        return await _ask_next_hours_message(update, context)

    await update.message.reply_text(
        f"For *{day}*, select the hours you can work:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=user_hours_reply_keyboard(slots),
    )
    return PICK_HOURS


async def pick_hours_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hour selection in message-based flow."""
    if not _signup_state_ok(context):
        await update.message.reply_text("Session expired. Send /start again.")
        context.user_data.clear()
        return ConversationHandler.END

    pending = context.user_data.get("pending_days") or []
    day = context.user_data.get("current_hours_day")
    if not pending or not day:
        await update.message.reply_text("This step is out of date. Send /start again.")
        context.user_data.clear()
        return ConversationHandler.END

    text = update.message.text.strip()
    hours = _parse_hours_from_text(text)
    if hours is None:
        await update.message.reply_text("Please choose one of the hour buttons.")
        return PICK_HOURS

    ts = context.user_data.get("signup_time_slots") or {}
    allowed = ts.get(day, []) or []
    if hours not in allowed:
        await update.message.reply_text("That hour isn't available for this day. Try again.")
        return PICK_HOURS

    context.user_data.setdefault("day_hours", {})[day] = hours
    pending.pop(0)
    context.user_data["pending_days"] = pending
    return await _ask_next_hours_message(update, context)


async def _ask_class_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Select your class type (applies to all your selected days):",
        reply_markup=user_class_reply_keyboard(),
    )
    return PICK_CLASS


async def pick_class_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _signup_state_ok(context):
        await update.message.reply_text("Session expired. Send /start again.")
        context.user_data.clear()
        return ConversationHandler.END

    class_type = _parse_class_from_text(update.message.text or "")
    if not class_type:
        await update.message.reply_text(
            "Invalid class type. Tap one of the buttons.",
            reply_markup=user_class_reply_keyboard(),
        )
        return PICK_CLASS

    context.user_data["class_type"] = class_type
    class_label = dict(CLASS_TYPES).get(class_type, class_type)

    day_hours = context.user_data.get("day_hours") or {}
    if not day_hours:
        await update.message.reply_text("No day/hour selections found. Send /start again.")
        context.user_data.clear()
        return ConversationHandler.END

    agent_name = context.user_data.get("agent_name") or ""
    if not agent_name:
        await update.message.reply_text("Session data missing. Send /start again.")
        context.user_data.clear()
        return ConversationHandler.END

    summary_lines = [
        "*Signup Summary*",
        f"Agent: *{_esc(agent_name)}*",
        f"Class: *{_esc(class_label)}*",
        "",
    ]
    for day, hours in _sorted_day_hours(day_hours):
        summary_lines.append(f"  {day}: {_hours_label(day, hours)}")
    summary_lines += [
        "",
        "*Important:* Once you confirm, you cannot cancel your OT commitment.",
        "Are you sure you want to sign up?",
    ]

    await update.message.reply_text(
        "\n".join(summary_lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=user_confirm_reply_keyboard(),
    )
    return CONFIRM


async def confirm_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _signup_state_ok(context):
        await update.message.reply_text("Session expired. Send /start again.")
        context.user_data.clear()
        return ConversationHandler.END

    text = (update.message.text or "").strip().lower()
    if text == "cancel":
        context.user_data.clear()
        await update.message.reply_text(
            "Signup cancelled. Use /start to begin again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    if text != "confirm":
        await update.message.reply_text(
            "Please choose Confirm or Cancel.",
            reply_markup=user_confirm_reply_keyboard(),
        )
        return CONFIRM

    # Save the signup(s).
    event_id = context.user_data.get("event_id")
    event = await _get_event(event_id) if event_id else None
    if event is None:
        context.user_data.clear()
        await update.message.reply_text(
            "This OT signup has closed or is no longer available. Send /start for a new OT."
        )
        return ConversationHandler.END

    agent_id = context.user_data.get("agent_id")
    if not agent_id:
        context.user_data.clear()
        await update.message.reply_text("Session expired. Send /start again.")
        return ConversationHandler.END

    agent = await _agent_by_pk(agent_id)
    if agent is None:
        context.user_data.clear()
        await update.message.reply_text("Could not load your profile. Send /start again.")
        return ConversationHandler.END

    class_type = context.user_data.get("class_type")
    day_hours = context.user_data.get("day_hours") or {}
    if not class_type or not day_hours:
        context.user_data.clear()
        await update.message.reply_text("Signup data incomplete. Send /start again.")
        return ConversationHandler.END

    saved = []
    blocked_other_open = False
    event_full = False

    for day, hours in _sorted_day_hours(day_hours):
        already = await _already_signed_up_day(agent, event, day)
        if already:
            continue
        _, status = await _create_signup(
            agent=agent,
            event=event,
            day=day,
            hours=hours,
            class_type=class_type,
        )
        if status == "created":
            saved.append((day, hours))
        elif status == "other_open_event":
            blocked_other_open = True
            break
        elif status == "full":
            event_full = True
            break
        elif status == "duplicate_day":
            await update.message.reply_text(
              "You have already signed up for this day in another open OT event. Please pick other days."
            )
            return ConversationHandler.END
        elif status in ("gone", "closed"):
            context.user_data.clear()
            await update.message.reply_text(
                "This OT event is no longer available. Send /start again."
            )
            return ConversationHandler.END

    if not saved:
        context.user_data.clear()
        if blocked_other_open:
            await update.message.reply_text(
                "You already have an active OT signup in another open event. You cannot sign up for more than one OT at the same time.",
            )
        elif event_full:
            await update.message.reply_text(
                f"The OT signup for *{_esc(event.title)}* is currently full.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text("You were already signed up for all selected days.")
        return ConversationHandler.END

    class_label = dict(CLASS_TYPES).get(class_type, "")
    lines = [
        "*Signed up!*",
        f"*{_esc(event.title)}*",
        f"Class: {_esc(class_label)}",
        "",
    ]
    for day, hours in _sorted_day_hours(dict(saved)):
        lines.append(f"  {day}: {_hours_label(day, hours)}")
    lines += ["", "Good luck with your OT!", "Your commitment is final and cannot be cancelled."]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

    # Capacity trigger (keeps existing behaviour).
    from bot.handlers.admin_handlers import begin_ot_closure_with_admin_approval
    ev2 = await _get_event_row_by_pk(event.id)
    context.user_data.clear()
    if ev2 and ev2.max_agents and ev2.is_full():
        await begin_ot_closure_with_admin_approval(context.bot, ev2.id, "capacity")

    return ConversationHandler.END


async def toggle_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _signup_state_ok(context) or "event_days" not in context.user_data:
        return await _end_signup_session(
            query,
            context,
            "This signup session is no longer valid (it may have expired). Please send /start again.",
        )
    try:
        _prefix, callback_session_id, day = query.data.split(":", 2)
    except ValueError:
        context.user_data.clear()
        try:
            await query.edit_message_text("Invalid selection.")
        except Exception:
            pass
        return ConversationHandler.END

    session_ok = await _require_wizard_session(query, context, callback_session_id)
    if session_ok is not True:
        return session_ok

    selected = context.user_data.setdefault("selected_days", [])
    event_days = context.user_data.get("event_days") or []
    disabled_days = context.user_data.get("disabled_days") or []
    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)
    try:
        await query.edit_message_reply_markup(
            reply_markup=user_day_multi_keyboard(
                event_days,
                selected,
                session_id=context.user_data["wizard_session_id"],
                disabled_days=disabled_days,
            )
        )
    except Exception:
        await query.message.reply_text(
            "Could not update that message. Please send /start to sign up again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END
    return PICK_DAYS


async def day_disabled_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show an alert when the user taps a 🚫 day that is blocked by another OT."""
    query = update.callback_query
    try:
        _prefix, _session_id, day = query.data.split(":", 2)
    except ValueError:
        day = "this day"
    await query.answer(
        f"⛔ {day} is already booked in another OT. Pick a different day.",
        show_alert=True,
    )
    return PICK_DAYS


async def days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _prefix, callback_session_id = query.data.split(":", 1)
    except ValueError:
        context.user_data.clear()
        await query.answer()
        return ConversationHandler.END

    session_ok = await _require_wizard_session(query, context, callback_session_id)
    if session_ok is not True:
        return session_ok

    if not _signup_state_ok(context) or "event_days" not in context.user_data:
        await query.answer()
        return await _end_signup_session(
            query,
            context,
            "This signup session is no longer valid. Please send /start again.",
        )
    selected = context.user_data.get("selected_days") or []
    if not selected:
        await query.answer("Please select at least one day!", show_alert=True)
        return PICK_DAYS

    await query.answer()
    # One DB check when leaving day selection; refresh slot cache for the hours wizard.
    eid = context.user_data.get("event_id")
    event_row = await _get_event_row_by_pk(eid)
    if event_row is None:
        return await _end_signup_session(
            query,
            context,
            "This OT event is no longer available. Please send /start when a new signup opens.",
        )
    if not event_row.is_open:
        return await _end_signup_session(
            query,
            context,
            "This OT signup has *closed* while you were selecting options.\n\nSend /start when a new OT is announced.",
        )
    context.user_data["signup_time_slots"] = dict(event_row.time_slots or {})
    # Queue every selected day for hours selection
    context.user_data["pending_days"] = _sorted_days(list(selected))
    return await _ask_next_hours(query, context)


async def _ask_next_hours(query_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """Ask for hours for the next pending day."""
    pending = context.user_data.get("pending_days") or []
    if not pending:
        # All days done – move to class selection
        return await _ask_class(query_or_msg, context)

    day = pending[0]
    context.user_data["current_hours_day"] = day

    ts = context.user_data.get("signup_time_slots")
    if ts is None:
        eid = context.user_data.get("event_id")
        event = await _get_event_row_by_pk(eid)
        if event is None or not event.is_open:
            return await _end_signup_session(
                query_or_msg if hasattr(query_or_msg, "answer") else None,
                context,
                "This OT signup is no longer available. Please send /start again.",
            )
        ts = dict(event.time_slots or {})
        context.user_data["signup_time_slots"] = ts

    slots = [float(s) for s in ts.get(day, [])]

    if not slots:
        try:
            await query_or_msg.edit_message_text(
                f"No time slots configured for *{day}*. Skipping.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        pd = context.user_data.get("pending_days")
        if isinstance(pd, list) and pd:
            pd.pop(0)
        return await _ask_next_hours(query_or_msg, context)

    try:
        await query_or_msg.edit_message_text(
            f"*{day}* — how many hours will you work?",
            reply_markup=user_hour_keyboard(
                day,
                slots,
                session_id=context.user_data["wizard_session_id"],
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        context.user_data.clear()
        try:
            await query_or_msg.message.reply_text(
                "Could not continue on this message. Please send /start to sign up again.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        return ConversationHandler.END
    return PICK_HOURS


async def pick_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _signup_state_ok(context):
        return await _end_signup_session(
            query,
            context,
            "Session expired. Please send /start again.",
        )
    pending = context.user_data.get("pending_days")
    day = context.user_data.get("current_hours_day")
    if pending is None or day is None:
        return await _end_signup_session(
            query,
            context,
            "This step is out of date. Please send /start again.",
        )
    try:
        _prefix, callback_session_id, slot_str = query.data.split(":", 2)
        session_ok = await _require_wizard_session(query, context, callback_session_id)
        if session_ok is not True:
            return session_ok
        # Only accept the next expected day to prevent stale double-taps / out-of-order taps.
        if not pending or pending[0] != day:
            return await _end_signup_session(
                query,
                context,
                "This hour selection is no longer valid. Please send /start again.",
            )
        hours = float(slot_str)
    except (IndexError, ValueError):
        return await _end_signup_session(query, context, "Invalid selection. Please send /start again.")
    context.user_data.setdefault("day_hours", {})[day] = hours
    context.user_data["pending_days"].pop(0)
    return await _ask_next_hours(query, context)


async def _ask_class(query_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """After all days have hours, ask for class type."""
    try:
        await query_or_msg.edit_message_text(
            "Select your *class type* (applies to all your selected days):",
            reply_markup=class_keyboard(context.user_data["wizard_session_id"]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        context.user_data.clear()
        try:
            await query_or_msg.message.reply_text(
                "Please send /start to continue signup.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        return ConversationHandler.END
    return PICK_CLASS


async def pick_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _signup_state_ok(context):
        return await _end_signup_session(
            query,
            context,
            "Session expired. Please send /start again.",
        )
    try:
        _prefix, callback_session_id, class_type = query.data.split(":", 2)
    except ValueError:
        return await _end_signup_session(query, context, "Invalid selection. Please send /start again.")
    session_ok = await _require_wizard_session(query, context, callback_session_id)
    if session_ok is not True:
        return session_ok
    context.user_data["class_type"] = class_type

    # Build confirmation summary
    day_hours = context.user_data.get("day_hours") or {}
    agent_name = context.user_data.get("agent_name")
    if not agent_name:
        return await _end_signup_session(
            query,
            context,
            "Session data was lost. Please send /start again.",
        )
    class_label = dict(CLASS_TYPES).get(class_type, class_type)

    summary_lines = ["*Signup Summary*\n", f"Agent: *{_esc(agent_name)}*", f"Class: *{_esc(class_label)}*\n"]
    for day, hours in _sorted_day_hours(day_hours):
        summary_lines.append(f"  {day}: *{_hours_label(day, hours)}*")

    summary_lines += [
        "",
        "*Important:* Once you confirm, you *cannot cancel* your OT commitment.\n",
        "Are you sure you want to sign up?",
    ]

    try:
        await query.edit_message_text(
            "\n".join(summary_lines),
            reply_markup=confirm_keyboard(context.user_data["wizard_session_id"]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        return await _end_signup_session(
            query,
            context,
            "Could not show confirmation. Please send /start again.",
        )
    return CONFIRM


async def confirm_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _prefix, callback_session_id, choice = query.data.split(":", 2)
    except ValueError:
        return await _end_signup_session(
            query,
            context,
            "Invalid selection. Please send /start again.",
        )

    session_ok = await _require_wizard_session(query, context, callback_session_id)
    if session_ok is not True:
        return session_ok

    if choice == "no":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                "Signup cancelled. You have *not* been signed up.\n"
                "Use /start to begin again if you change your mind.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                await query.message.reply_text(
                    "Signup cancelled. Use /start to begin again.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
        return ConversationHandler.END

    event_id = context.user_data.get("event_id")
    event = await _get_event(event_id) if event_id else None
    if event is None:
        context.user_data.clear()
        try:
            await query.edit_message_text(
                "This OT signup has closed or is no longer available.\n\nSend /start when a new OT opens."
            )
        except Exception:
            try:
                await query.message.reply_text(
                    "This OT signup has closed. Send /start when a new OT opens."
                )
            except Exception:
                pass
        return ConversationHandler.END

    agent_id = context.user_data.get("agent_id")
    if not agent_id:
        return await _end_signup_session(
            query,
            context,
            "Session expired. Please send /start again.",
        )

    agent = await _agent_by_pk(agent_id)
    if agent is None:
        return await _end_signup_session(
            query,
            context,
            "Could not load your profile. Please send /start again.",
        )

    class_type = context.user_data.get("class_type")
    day_hours = context.user_data.get("day_hours") or {}
    if not class_type or not day_hours:
        return await _end_signup_session(
            query,
            context,
            "Signup data was incomplete. Please send /start again.",
        )
    class_label = dict(CLASS_TYPES).get(class_type, "")

    saved = []
    blocked_other_open = False
    event_full = False
    for day, hours in _sorted_day_hours(day_hours):
        already = await _already_signed_up_day(agent, event, day)
        if not already:
            _, status = await _create_signup(
                agent=agent,
                event=event,
                day=day,
                hours=hours,
                class_type=class_type,
            )
            if status == "created":
                saved.append((day, hours))
            elif status == "other_open_event":
                blocked_other_open = True
                break
            elif status == "full":
                event_full = True
                break
            elif status == "duplicate_day":
                await query.edit_message_text(
                  "You have already signed up for this day in another open OT event. Please pick other days."
                )
                return ConversationHandler.END
            elif status == "gone":
                context.user_data.clear()
                try:
                    await query.edit_message_text(
                        "This OT event was removed. Your signup could not be completed.\n\nSend /start for a new OT."
                    )
                except Exception:
                    try:
                        await query.message.reply_text(
                            "This OT event was removed. Send /start for a new OT."
                        )
                    except Exception:
                        pass
                return ConversationHandler.END
            elif status == "closed":
                context.user_data.clear()
                try:
                    await query.edit_message_text(
                        "This OT signup has closed. Your remaining selections were not saved.\n\nSend /start when a new OT opens."
                    )
                except Exception:
                    try:
                        await query.message.reply_text(
                            "This OT signup has closed. Send /start when a new OT opens."
                        )
                    except Exception:
                        pass
                return ConversationHandler.END

    if not saved:
        context.user_data.clear()
        try:
            if blocked_other_open:
                await query.edit_message_text(
                    "You already have an active OT signup in another open event.\n"
                    "You cannot sign up for more than one OT at the same time."
                )
            elif event_full:
                await query.edit_message_text(
                    f"The OT signup for *{_esc(event.title)}* is currently full.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await query.edit_message_text("You were already signed up for all selected days.")
        except Exception:
            try:
                await query.message.reply_text(
                    "Could not complete signup. Send /start to try again.",
                )
            except Exception:
                pass
        return ConversationHandler.END

    lines = ["*Signed up!*\n", f"*{_esc(event.title)}*", f"Class: {_esc(class_label)}\n"]
    for day, hours in _sorted_day_hours(dict(saved)):
        lines.append(f"  {day}: {_hours_label(day, hours)}")
    lines += ["", "Good luck with your OT!", "Your commitment is final and cannot be cancelled."]

    try:
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        try:
            await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    from bot.handlers.admin_handlers import begin_ot_closure_with_admin_approval

    ev2 = await _get_event_row_by_pk(event.id)
    context.user_data.clear()
    if ev2 and ev2.max_agents and ev2.is_full():
        await begin_ot_closure_with_admin_approval(context.bot, ev2.id, "capacity")

    return ConversationHandler.END


async def cancel_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Signup cancelled. Use /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def _outdated_signup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles legacy inline callbacks without session_id to avoid callback-query spinners."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer("This button is outdated. Send /start to begin again.", show_alert=False)
    return await _end_signup_session(
        query,
        context,
        "This signup session is no longer valid. Please send /start again.",
    )


async def my_ot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone command for a user to see what they signed up for in the current event."""
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Please use /myot in a private chat with the bot."
        )
        return

    user_id = update.effective_user.id
    agent = await _get_agent(user_id)
    if not agent:
        await update.message.reply_text("You haven't signed up for any OT yet.")
        return
        
    from bot.models import OTSignup, OTEvent
    # Show signups for ALL active events
    signups = await sync_to_async(list)(
        OTSignup.objects.filter(agent=agent, ot_event__is_open=True).select_related("ot_event")
    )
    if not signups:
        await update.message.reply_text("You have no active OT signups at the moment.")
        return

    # Group by event ID (Django model instances are not safe dict keys here).
    from collections import defaultdict
    by_event = defaultdict(list)
    event_by_id = {}
    for s in signups:
        eid = s.ot_event_id
        by_event[eid].append(s)
        event_by_id[eid] = s.ot_event

    lines = ["*Your Active OT Signups*\n"]
    for event_id, event_signups in by_event.items():
        event = event_by_id[event_id]
        lines.append(f"📦 *{_esc(event.title)}*")
        event_signups = sorted(
            event_signups,
            key=lambda s: (_DAY_INDEX.get(s.day, 99), s.confirmed_at),
        )
        for s in event_signups:
            hrs = float(s.hours)
            class_label = dict(CLASS_TYPES).get(s.class_type, s.class_type)
            label = _hours_label(s.day, hrs)
            lines.append(f"  • {s.day}: *{label}* — {class_label}")
        lines.append("")
    
    lines.append("You cannot cancel your confirmed signups. Contact an admin if you need to make changes.")
    
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── ConversationHandler factory ───────────────────────────────────────────────

def build_user_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=filters.ChatType.PRIVATE),
        ],
        states={
            PICK_EVENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_event_message),
            ],
            _ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            PICK_DAYS: [
                # Disabled-day alert must come before the generic toggle handler.
                CallbackQueryHandler(day_disabled_alert, pattern=r"^uday_disabled:"),
                CallbackQueryHandler(toggle_day, pattern=r"^uday_toggle:"),
                CallbackQueryHandler(days_done, pattern=r"^udays_done:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_days_message),
            ],
            PICK_HOURS: [
                CallbackQueryHandler(pick_hours, pattern=r"^uhour:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_hours_message),
            ],
            PICK_CLASS: [
                CallbackQueryHandler(pick_class, pattern=r"^uclass:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_class_message),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_signup, pattern=r"^uconfirm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_message),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_signup),
            CallbackQueryHandler(_outdated_signup_callback),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )
