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

from bot.models import Agent, OTEvent, OTSignup
from bot.utils import (
    user_day_multi_keyboard,
    user_hour_keyboard,
    class_keyboard,
    confirm_keyboard,
    select_event_keyboard,
    CLASS_TYPES,
    _hours_label,
    _esc,
)

# ── Conversation states ──────────────────────────────────────────────────────
PICK_EVENT = 4
PICK_DAYS = 0
PICK_HOURS = 1
PICK_CLASS = 2
CONFIRM = 3


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
def _has_signup_in_any_open_event(agent):
    """True if agent has a signup in any currently open OT event."""
    from bot.models import OTEvent
    return OTSignup.objects.filter(agent=agent, ot_event__is_open=True).exists()


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
        has_other_open = OTSignup.objects.filter(
            agent=agent,
            ot_event__is_open=True,
        ).exclude(ot_event=ev).exists()
        if has_other_open:
            return None, "other_open_event"
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


# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point – /start or first message in private chat."""
    context.user_data.clear()

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

    # 2. Check for ANY signup if they are a known agent
    existing_agent = await _get_agent(update.effective_user.id)
    if existing_agent and await _has_signup_in_any_open_event(existing_agent):
        await update.message.reply_text(
            "You already have an active OT signup.\n"
            "You cannot sign up for more than one OT at the same time.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

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
            keyboard = select_event_keyboard(events, "user_signup")
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
    
    data = query.data  # "user_signup:123"
    try:
        event_id = int(data.split(":")[1])
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
    except (IndexError, ValueError):
        context.user_data.clear()
        try:
            await query.edit_message_text("Invalid selection.")
        except Exception:
            pass
        return ConversationHandler.END


async def _start_signup_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, event):
    """Internal helper to shared logic for starting the actual signup steps."""
    # Check for existing agent
    existing_agent = await _get_agent(update.effective_user.id)
    if existing_agent:
        # Check if already signed up specifically for this event
        already = await _any_signup_for_event(existing_agent, event)
        if already:
            msg = f"You already have signups for *{_esc(event.title)}*!\nYou cannot change or cancel your signup."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

        context.user_data["agent_name"] = existing_agent.agent_name
        context.user_data["agent_id"] = existing_agent.pk
        context.user_data["agent_known"] = True
    else:
        context.user_data["agent_known"] = False

    context.user_data["selected_days"] = []
    context.user_data["day_hours"] = {}   # {day: hours}

    async def _send_text(text, **kwargs):
        if update.callback_query:
            await update.callback_query.message.reply_text(text, **kwargs)
        else:
            await update.message.reply_text(text, **kwargs)

    if not context.user_data["agent_known"]:
        await _send_text(
            f"Welcome! You're signing up for *{_esc(event.title)}*.\n\n"
            "Please enter your *agent name* exactly as it appears in your roster:",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Temporarily store event days for after name entry
        context.user_data["event_days"] = event.days
        return _ASK_NAME

    await _send_text(
        f"Welcome back, *{_esc(existing_agent.agent_name)}*!\n\n"
        f"Signing up for: *{_esc(event.title)}*\n\n"
        "Select the days you want to work OT.\n"
        "Tap a day to toggle it, then press Done.",
        reply_markup=user_day_multi_keyboard(event.days, []),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["event_days"] = event.days
    return PICK_DAYS


_ASK_NAME = 10  # Extra state only used for new agents


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agent_name = update.message.text.strip()
    if not agent_name:
        await update.message.reply_text("Please enter a valid name.")
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
    await update.message.reply_text(
        f"Name saved: *{_esc(agent_name)}*\n\n"
        "Select the days you want to work OT.\n"
        "Tap a day to toggle it, then press Done.",
        reply_markup=user_day_multi_keyboard(event_days, []),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_DAYS


async def toggle_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _signup_state_ok(context) or "event_days" not in context.user_data:
        return await _end_signup_session(
            query,
            context,
            "This signup session is no longer valid (it may have expired). Please send /start again.",
        )
    day = query.data.split(":", 1)[1]
    selected = context.user_data.setdefault("selected_days", [])
    event_days = context.user_data.get("event_days") or []
    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)
    try:
        await query.edit_message_reply_markup(
            reply_markup=user_day_multi_keyboard(event_days, selected)
        )
    except Exception:
        await query.message.reply_text(
            "Could not update that message. Please send /start to sign up again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END
    return PICK_DAYS


async def days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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
    # Queue every selected day for hours selection
    context.user_data["pending_days"] = list(selected)
    return await _ask_next_hours(query, context)


async def _ask_next_hours(query_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """Ask for hours for the next pending day."""
    pending = context.user_data.get("pending_days") or []
    if not pending:
        # All days done – move to class selection
        return await _ask_class(query_or_msg, context)

    day = pending[0]
    context.user_data["current_hours_day"] = day

    eid = context.user_data.get("event_id")
    event = await _get_event_row_by_pk(eid)
    if event is None:
        return await _end_signup_session(
            query_or_msg if hasattr(query_or_msg, "answer") else None,
            context,
            "This OT event is no longer available. Please send /start when a new signup opens.",
        )
    if not event.is_open:
        return await _end_signup_session(
            query_or_msg if hasattr(query_or_msg, "answer") else None,
            context,
            "This OT signup has *closed* while you were selecting options.\n\nSend /start when a new OT is announced.",
        )

    slots = [float(s) for s in (event.time_slots or {}).get(day, [])]

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
            reply_markup=user_hour_keyboard(day, slots),
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
        hours = float(query.data.split(":", 1)[1])
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
            reply_markup=class_keyboard(),
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
    class_type = query.data.split(":", 1)[1]
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
    for day, hours in day_hours.items():
        summary_lines.append(f"  {day}: *{_hours_label(day, hours)}*")

    summary_lines += [
        "",
        "*Important:* Once you confirm, you *cannot cancel* your OT commitment.\n",
        "Are you sure you want to sign up?",
    ]

    try:
        await query.edit_message_text(
            "\n".join(summary_lines),
            reply_markup=confirm_keyboard(),
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
    choice = query.data.split(":", 1)[1]

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
    for day, hours in day_hours.items():
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
    for day, hours in saved:
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
    await update.message.reply_text("Signup cancelled. Use /start to begin again.")
    return ConversationHandler.END


async def my_ot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone command for a user to see what they signed up for in the current event."""
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    agent = await _get_agent(user_id)
    if not agent:
        await update.message.reply_text("You haven't signed up for any OT yet.")
        return
        
    from bot.models import OTSignup, OTEvent
    # Show signups for ALL active events
    signups = await sync_to_async(list)(
        OTSignup.objects.filter(agent=agent, ot_event__is_open=True)
    )
    if not signups:
        await update.message.reply_text("You have no active OT signups at the moment.")
        return

    # Group by event
    from collections import defaultdict
    by_event = defaultdict(list)
    for s in signups:
        by_event[s.ot_event].append(s)

    lines = ["*Your Active OT Signups*\n"]
    for event, event_signups in by_event.items():
        lines.append(f"📦 *{_esc(event.title)}*")
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
            MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, start),
        ],
        states={
            PICK_EVENT: [CallbackQueryHandler(select_event, pattern=r"^user_signup:")],
            _ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            PICK_DAYS: [
                CallbackQueryHandler(toggle_day, pattern=r"^uday_toggle:"),
                CallbackQueryHandler(days_done, pattern=r"^udays_done$"),
            ],
            PICK_HOURS: [CallbackQueryHandler(pick_hours, pattern=r"^uhour:")],
            PICK_CLASS: [CallbackQueryHandler(pick_class, pattern=r"^uclass:")],
            CONFIRM: [CallbackQueryHandler(confirm_signup, pattern=r"^uconfirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_signup)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        # per_message must stay False: with True, PTB ignores any update without callback_query,
        # so /start and plain text entry never match this handler (see ConversationHandler.check_update).
        per_message=False,
    )
