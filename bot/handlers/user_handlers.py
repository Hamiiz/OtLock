"""
User conversation handlers for OT Signup Bot.

Flow:
  /start (in private chat)
    → ASK_NAME    : User enters agent name
    → PICK_DAY    : User selects a day (inline buttons)
    → PICK_HOURS  : User selects hours (inline buttons)
    → PICK_CLASS  : User selects class type
    → CONFIRM     : Confirmation prompt – cannot undo after ✅
"""
from __future__ import annotations

import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tgbot.settings")
django.setup()

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
    user_day_keyboard,
    user_hour_keyboard,
    class_keyboard,
    confirm_keyboard,
    CLASS_TYPES,
    _hours_label,
)

# ── Conversation states ──────────────────────────────────────────────────────
ASK_NAME = 0
PICK_DAY = 1
PICK_HOURS = 2
PICK_CLASS = 3
CONFIRM = 4


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
def _already_signed_up(agent, event):
    return OTSignup.objects.filter(agent=agent, ot_event=event).exists()


@sync_to_async
def _create_signup(agent, event, day, hours, class_type):
    return OTSignup.objects.create(
        agent=agent,
        ot_event=event,
        day=day,
        hours=hours,
        class_type=class_type,
    )


@sync_to_async
def _event_is_full(event):
    return event.is_full()


# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point – /start or first message in private chat."""
    context.user_data.clear()

    # Check if there's an active event at all
    event = await _get_open_event()
    if event is None:
        await update.message.reply_text(
            "ℹ️ There is no active OT signup at the moment.\n"
            "Watch the group for announcements!"
        )
        return ConversationHandler.END

    if await _event_is_full(event):
        await update.message.reply_text(
            "⛔ The OT signup is currently full. No more spots available."
        )
        return ConversationHandler.END

    context.user_data["event_id"] = event.id

    # Check if this user already has a linked agent name
    existing_agent = await _get_agent(update.effective_user.id)
    if existing_agent:
        # Check if they already signed up for this event
        already = await _already_signed_up(existing_agent, event)
        if already:
            await update.message.reply_text(
                f"✅ You are already signed up for *{event.title}*!\n"
                "You cannot change or cancel your signup.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        # Re-use the existing agent name, skip name entry
        context.user_data["agent_name"] = existing_agent.agent_name
        context.user_data["agent_id"] = existing_agent.pk
        await update.message.reply_text(
            f"👋 Welcome back, *{existing_agent.agent_name}*!\n\n"
            f"📋 Signing up for: *{event.title}*\n\n"
            "📅 Select the day you want to work OT:",
            reply_markup=user_day_keyboard(event.days),
            parse_mode=ParseMode.MARKDOWN,
        )
        return PICK_DAY

    # New user – ask for their name
    await update.message.reply_text(
        f"👋 Welcome! You're signing up for *{event.title}*.\n\n"
        "Please enter your *agent name* exactly as it appears in your roster:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agent_name = update.message.text.strip()
    if not agent_name:
        await update.message.reply_text("⚠️ Please enter a valid name.")
        return ASK_NAME

    context.user_data["agent_name"] = agent_name

    event = await _get_open_event()
    if event is None:
        await update.message.reply_text("ℹ️ No active OT event found. Please try again later.")
        return ConversationHandler.END

    # Create / update the agent record right away so the name is linked
    user = update.effective_user
    agent = await _get_or_create_agent(user.id, user.username or "", agent_name)
    context.user_data["agent_id"] = agent.pk

    await update.message.reply_text(
        f"✅ Name saved: *{agent_name}*\n\n"
        "📅 Select the day you want to work OT:",
        reply_markup=user_day_keyboard(event.days),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_DAY


async def pick_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = query.data.split(":", 1)[1]
    context.user_data["day"] = day

    event = await _get_open_event()
    if event is None:
        await query.edit_message_text("ℹ️ The OT event has been closed.")
        return ConversationHandler.END

    slots = event.time_slots.get(day, [])
    if not slots:
        await query.edit_message_text("⚠️ No time slots available for this day. Please contact admin.")
        return ConversationHandler.END

    # Convert stored values to floats for display
    slots = [float(s) for s in slots]
    await query.edit_message_text(
        f"📅 Day selected: *{day}*\n\n🕐 How many hours will you work?",
        reply_markup=user_hour_keyboard(day, slots),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_HOURS


async def pick_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hours = float(query.data.split(":", 1)[1])
    context.user_data["hours"] = hours

    await query.edit_message_text(
        f"📅 Day: *{context.user_data['day']}* | 🕐 Hours: *{hours}*\n\n"
        "🎛️ Select your class type:",
        reply_markup=class_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PICK_CLASS


async def pick_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    class_type = query.data.split(":", 1)[1]
    context.user_data["class_type"] = class_type

    class_label = dict(CLASS_TYPES).get(class_type, class_type)
    day = context.user_data["day"]
    hours = context.user_data["hours"]
    agent_name = context.user_data["agent_name"]
    hours_label = _hours_label(day, hours)

    await query.edit_message_text(
        f"📝 *Signup Summary*\n\n"
        f"👤 Agent: *{agent_name}*\n"
        f"📅 Day: *{day}*\n"
        f"🕐 Hours: *{hours_label}*\n"
        f"🎛️ Class: *{class_label}*\n\n"
        "⚠️ *Important:* Once you confirm, you *cannot cancel* your OT commitment.\n\n"
        "Are you sure you want to sign up?",
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
            "❌ Signup cancelled. You have *not* been signed up for OT.\n"
            "Use /start to begin again if you change your mind.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.clear()
        return ConversationHandler.END

    # choice == "yes" – commit the signup
    event = await _get_open_event()
    if event is None:
        await query.edit_message_text("ℹ️ The OT event has been closed. Signup could not be saved.")
        return ConversationHandler.END

    if await _event_is_full(event):
        await query.edit_message_text(
            "⛔ Sorry, the OT event just reached its maximum capacity. You couldn't be signed up."
        )
        return ConversationHandler.END

    agent_id = context.user_data["agent_id"]
    from bot.models import Agent as AgentModel
    agent = await sync_to_async(AgentModel.objects.get)(pk=agent_id)

    already = await _already_signed_up(agent, event)
    if already:
        await query.edit_message_text(
            "⚠️ You're already signed up for this OT event!"
        )
        return ConversationHandler.END

    await _create_signup(
        agent=agent,
        event=event,
        day=context.user_data["day"],
        hours=context.user_data["hours"],
        class_type=context.user_data["class_type"],
    )

    class_label = dict(CLASS_TYPES).get(context.user_data["class_type"], "")
    day = context.user_data["day"]
    hours = context.user_data["hours"]

    await query.edit_message_text(
        f"🎉 *You're signed up!*\n\n"
        f"📋 *{event.title}*\n"
        f"📅 {day} | 🕐 {_hours_label(day, hours)} | 🎛️ {class_label}\n\n"
        "Good luck with your OT! 💪\n\n"
        "_Remember: your commitment is final and cannot be cancelled._",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Signup cancelled. Use /start to begin again.")
    return ConversationHandler.END


# ── ConversationHandler factory ───────────────────────────────────────────────

def build_user_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, start),
        ],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            PICK_DAY: [CallbackQueryHandler(pick_day, pattern=r"^uday:")],
            PICK_HOURS: [CallbackQueryHandler(pick_hours, pattern=r"^uhour:")],
            PICK_CLASS: [CallbackQueryHandler(pick_class, pattern=r"^uclass:")],
            CONFIRM: [CallbackQueryHandler(confirm_signup, pattern=r"^uconfirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_signup)],
        per_user=True,
        per_chat=True,
    )
