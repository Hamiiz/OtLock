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
    CLASS_TYPES,
    _hours_label,
    _esc,
)

# ── Conversation states ──────────────────────────────────────────────────────
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
def _get_open_event():
    """Return the most recently created open OT event, or None."""
    return OTEvent.objects.filter(is_open=True).order_by("-created_at").first()


@sync_to_async
def _already_signed_up_day(agent, event, day):
    """True if agent already has a signup for this event+day combination."""
    return OTSignup.objects.filter(agent=agent, ot_event=event, day=day).exists()


@sync_to_async
def _any_signup_for_event(agent, event):
    """True if agent has any signup at all for this event."""
    return OTSignup.objects.filter(agent=agent, ot_event=event).exists()


@sync_to_async
def _create_signup(agent, event, day, hours, class_type):
    from django.db import transaction
    with transaction.atomic():
        # Re-fetch the event with a row-level lock to prevent race conditions
        # on max_agents when two users confirm at the same moment.
        ev = OTEvent.objects.select_for_update().get(pk=event.pk)
        if ev.is_full():
            return None, False   # caller handles the "event is full" case
        return OTSignup.objects.get_or_create(
            agent=agent,
            ot_event=ev,
            day=day,
            defaults=dict(hours=hours, class_type=class_type),
        )


@sync_to_async
def _event_is_full(event):
    return event.is_full()


# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point – /start or first message in private chat."""
    context.user_data.clear()

    event = await _get_open_event()
    if event is None:
        await update.message.reply_text(
            "There is no active OT signup at the moment.\n"
            "Watch the group for announcements!"
        )
        return ConversationHandler.END

    if await _event_is_full(event):
        await update.message.reply_text(
            "The OT signup is currently full. No more spots available."
        )
        return ConversationHandler.END

    context.user_data["event_id"] = event.id

    # Check for existing agent
    existing_agent = await _get_agent(update.effective_user.id)
    if existing_agent:
        already = await _any_signup_for_event(existing_agent, event)
        if already:
            await update.message.reply_text(
                f"You already have signups for *{_esc(event.title)}*!\n"
                "You cannot change or cancel your signup.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        context.user_data["agent_name"] = existing_agent.agent_name
        context.user_data["agent_id"] = existing_agent.pk
        context.user_data["agent_known"] = True
    else:
        context.user_data["agent_known"] = False

    context.user_data["selected_days"] = []
    context.user_data["day_hours"] = {}   # {day: hours}

    if not context.user_data["agent_known"]:
        await update.message.reply_text(
            f"Welcome! You're signing up for *{_esc(event.title)}*.\n\n"
            "Please enter your *agent name* exactly as it appears in your roster:",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Temporarily store event days for after name entry
        context.user_data["event_days"] = event.days
        return _ASK_NAME

    await update.message.reply_text(
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

    context.user_data["agent_name"] = agent_name
    user = update.effective_user
    agent = await _get_or_create_agent(user.id, user.username or "", agent_name)
    context.user_data["agent_id"] = agent.pk

    event_days = context.user_data["event_days"]
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
    day = query.data.split(":", 1)[1]
    selected = context.user_data["selected_days"]
    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)
    await query.edit_message_reply_markup(
        reply_markup=user_day_multi_keyboard(context.user_data["event_days"], selected)
    )
    return PICK_DAYS


async def days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    selected = context.user_data["selected_days"]
    if not selected:
        await query.answer("Please select at least one day!", show_alert=True)
        return PICK_DAYS

    await query.answer()
    # Queue every selected day for hours selection
    context.user_data["pending_days"] = list(selected)
    return await _ask_next_hours(query, context)


async def _ask_next_hours(query_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """Ask for hours for the next pending day."""
    pending = context.user_data["pending_days"]
    if not pending:
        # All days done – move to class selection
        return await _ask_class(query_or_msg, context)

    day = pending[0]
    context.user_data["current_hours_day"] = day

    # Reload event to get current time_slots
    from bot.models import OTEvent
    event = await sync_to_async(OTEvent.objects.get)(pk=context.user_data["event_id"])
    slots = [float(s) for s in event.time_slots.get(day, [])]

    if not slots:
        await query_or_msg.edit_message_text(
            f"No time slots configured for *{day}*. Skipping.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["pending_days"].pop(0)
        return await _ask_next_hours(query_or_msg, context)

    await query_or_msg.edit_message_text(
        f"*{day}* — how many hours will you work?",
        reply_markup=user_hour_keyboard(day, slots),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_HOURS


async def pick_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hours = float(query.data.split(":", 1)[1])
    day = context.user_data["current_hours_day"]
    context.user_data["day_hours"][day] = hours
    context.user_data["pending_days"].pop(0)
    return await _ask_next_hours(query, context)


async def _ask_class(query_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """After all days have hours, ask for class type."""
    await query_or_msg.edit_message_text(
        "Select your *class type* (applies to all your selected days):",
        reply_markup=class_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_CLASS


async def pick_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    class_type = query.data.split(":", 1)[1]
    context.user_data["class_type"] = class_type

    # Build confirmation summary
    day_hours = context.user_data["day_hours"]
    agent_name = context.user_data["agent_name"]
    class_label = dict(CLASS_TYPES).get(class_type, class_type)

    summary_lines = ["*Signup Summary*\n", f"Agent: *{_esc(agent_name)}*", f"Class: *{_esc(class_label)}*\n"]
    for day, hours in day_hours.items():
        summary_lines.append(f"  {day}: *{_hours_label(day, hours)}*")

    summary_lines += [
        "",
        "*Important:* Once you confirm, you *cannot cancel* your OT commitment.\n",
        "Are you sure you want to sign up?",
    ]

    await query.edit_message_text(
        "\n".join(summary_lines),
        reply_markup=confirm_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return CONFIRM


async def confirm_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "no":
        await query.edit_message_text(
            "Signup cancelled. You have *not* been signed up.\n"
            "Use /start to begin again if you change your mind.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END

    event = await _get_open_event()
    if event is None:
        await query.edit_message_text("The OT event has been closed. Signup could not be saved.")
        return ConversationHandler.END

    from bot.models import Agent as AgentModel
    agent = await sync_to_async(AgentModel.objects.get)(pk=context.user_data["agent_id"])
    class_type = context.user_data["class_type"]
    day_hours = context.user_data["day_hours"]
    class_label = dict(CLASS_TYPES).get(class_type, "")

    saved = []
    for day, hours in day_hours.items():
        already = await _already_signed_up_day(agent, event, day)
        if not already:
            await _create_signup(agent=agent, event=event, day=day, hours=hours, class_type=class_type)
            saved.append((day, hours))

    if not saved:
        await query.edit_message_text("You were already signed up for all selected days.")
        return ConversationHandler.END

    lines = ["*Signed up!*\n", f"*{_esc(event.title)}*", f"Class: {_esc(class_label)}\n"]
    for day, hours in saved:
        lines.append(f"  {day}: {_hours_label(day, hours)}")
    lines += ["", "Good luck with your OT!", "Your commitment is final and cannot be cancelled."]

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Signup cancelled. Use /start to begin again.")
    return ConversationHandler.END


async def my_ot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone command for a user to see what they signed up for in the current event."""
    if update.effective_chat.type != "private":
        return

    event = await _get_open_event()
    if event is None:
        await update.message.reply_text("There is no active OT event.")
        return

    user_id = update.effective_user.id
    agent = await _get_agent(user_id)
    if not agent:
        await update.message.reply_text("You haven't signed up for any OT yet.")
        return
        
    from bot.models import OTSignup
    signups = await sync_to_async(list)(OTSignup.objects.filter(agent=agent, ot_event=event))
    if not signups:
        await update.message.reply_text(f"You haven't signed up for *{_esc(event.title)}* yet.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"*Your OT Signups for {_esc(event.title)}*\n"]
    for signup in signups:
        hrs = float(signup.hours)
        class_label = dict(CLASS_TYPES).get(signup.class_type, signup.class_type)
        label = _hours_label(signup.day, hrs)
        lines.append(f"  {signup.day}: *{label}* — {class_label}")
    
    lines.append("\nYou cannot cancel your confirmed signups. Contact an admin if you need to make changes.")
    
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
        per_user=True,
        per_chat=True,
        per_message=False,
    )
