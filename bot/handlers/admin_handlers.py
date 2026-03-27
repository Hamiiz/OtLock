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

from django.conf import settings
from bot.models import OTEvent, OTSignup
from bot.utils import (
    days_keyboard,
    slot_keyboard_weekday,
    slot_keyboard_weekend,
    WEEKEND_DAYS,
    format_announcement,
    format_signup_list,
    approve_list_keyboard,
)

# ── Conversation states ──────────────────────────────────────────────────────
ASK_TITLE = 0
ASK_DAYS = 1
ASK_SLOTS = 2
ASK_SLOT_CUSTOM = 3
ASK_MAX = 4


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def admin_only(func):
    """Decorator that silently ignores non-admin users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid):
            await update.message.reply_text("⛔ You are not authorised to use this command.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


@sync_to_async
def _create_event(title, telegram_id, days, time_slots, max_agents):
    return OTEvent.objects.create(
        title=title,
        created_by_telegram_id=telegram_id,
        days=days,
        time_slots=time_slots,
        max_agents=max_agents,
        group_chat_id=settings.GROUP_CHAT_ID,
    )


@sync_to_async
def _get_open_events():
    return list(OTEvent.objects.filter(is_open=True).order_by("-created_at"))


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


# ── /newot flow ──────────────────────────────────────────────────────────────

@admin_only
async def newot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin triggers /newot."""
    context.user_data.clear()
    context.user_data["selected_days"] = []
    context.user_data["time_slots"] = {}
    await update.message.reply_text(
        "🆕 *New OT Event*\n\nEnter a title / description for this OT event:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 Select the days for this OT event.\nTap a day to toggle it, then press *Done*.",
        reply_markup=days_keyboard(context.user_data["selected_days"]),
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
    await query.edit_message_reply_markup(reply_markup=days_keyboard(selected))
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
            "👥 Set the *maximum number of agents* who can sign up.\n"
            "Send a number, or type `0` for no limit.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_MAX

    day = pending[0]
    context.user_data["current_slot_day"] = day
    if day in WEEKEND_DAYS:
        kb = slot_keyboard_weekend(day)
    else:
        kb = slot_keyboard_weekday(day)

    await query_or_msg.edit_message_text(
        f"🕐 Configure time slot for *{day}*:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_SLOTS


async def receive_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, day, value = query.data.split(":", 2)

    if value == "custom":
        context.user_data["current_slot_day"] = day
        await query.edit_message_text(
            f"✏️ Enter custom hours for *{day}* (e.g. `6` or `3.5`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_SLOT_CUSTOM

    hours = float(value)
    context.user_data["time_slots"][day] = [hours]
    context.user_data["pending_days"].pop(0)
    return await _ask_next_slot(query, context)


async def slot_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin skips slot config for a day (uses defaults later)."""
    query = update.callback_query
    await query.answer()
    _, day = query.data.split(":", 1)
    if day not in context.user_data["time_slots"]:
        # Use a sensible default
        default = [8.0] if day in WEEKEND_DAYS else [2.0, 4.0]
        context.user_data["time_slots"][day] = default
    context.user_data["pending_days"].pop(0)
    return await _ask_next_slot(query, context)


async def receive_custom_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid positive number.")
        return ASK_SLOT_CUSTOM

    day = context.user_data["current_slot_day"]
    context.user_data["time_slots"][day] = [hours]
    context.user_data["pending_days"].pop(0)

    pending = context.user_data.get("pending_days", [])
    if not pending:
        await update.message.reply_text(
            "👥 Set the *maximum number of agents* who can sign up.\n"
            "Send a number, or type `0` for no limit.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_MAX

    next_day = pending[0]
    context.user_data["current_slot_day"] = next_day
    if next_day in WEEKEND_DAYS:
        kb = slot_keyboard_weekend(next_day)
    else:
        kb = slot_keyboard_weekday(next_day)
    await update.message.reply_text(
        f"🕐 Configure time slot for *{next_day}*:",
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
        await update.message.reply_text("⚠️ Please enter a valid non-negative integer (0 = no limit).")
        return ASK_MAX

    title = context.user_data["title"]
    days = context.user_data["selected_days"]
    time_slots = context.user_data["time_slots"]
    uid = update.effective_user.id

    # Fill in any days that didn't get explicit slots
    for day in days:
        if day not in time_slots:
            time_slots[day] = [8.0] if day in WEEKEND_DAYS else [2.0, 4.0]

    event = await _create_event(title, uid, days, time_slots, max_agents)
    announcement = format_announcement(event)

    # Send to group
    msg = await context.bot.send_message(
        chat_id=settings.GROUP_CHAT_ID,
        text=announcement,
        parse_mode=ParseMode.MARKDOWN,
    )
    await _save_message_id(event, msg.message_id)

    max_str = str(max_agents) if max_agents else "Unlimited"
    await update.message.reply_text(
        f"✅ OT Event *{title}* published!\n"
        f"📅 Days: {', '.join(days)}\n"
        f"👥 Max: {max_str}\n\n"
        f"Use /closesignup to close enrollment and send the list.",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_newot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ OT creation cancelled.")
    return ConversationHandler.END


# ── /closesignup flow ────────────────────────────────────────────────────────

async def close_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin closes the active OT event and gets the signup list for approval."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    events = await _get_open_events()
    if not events:
        await update.message.reply_text("ℹ️ There are no open OT events right now.")
        return

    # Close all open events (or adjust to handle multiple events if needed)
    for event in events:
        signups = await _get_signups(event)
        await _close_event(event)
        list_text = format_signup_list(event, signups)
        approval_keyboard = approve_list_keyboard(event.id)
        await update.message.reply_text(
            f"📋 Signup closed for *{event.title}*.\n\n"
            f"Review the list below and tap *Approve* to send it to the group:\n\n"
            f"{list_text}",
            reply_markup=approval_keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )


async def approve_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin taps 'Approve & Send' – forwards signup list to group."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Not authorised.", show_alert=True)
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
        f"✅ Signup list for *{event.title}* has been sent to the group!",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── ConversationHandler factory ───────────────────────────────────────────────

def build_admin_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("newot", newot_start)],
        states={
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            ASK_DAYS: [
                CallbackQueryHandler(toggle_day, pattern=r"^day_toggle:"),
                CallbackQueryHandler(days_done, pattern=r"^days_done$"),
            ],
            ASK_SLOTS: [
                CallbackQueryHandler(receive_slot, pattern=r"^slot:"),
                CallbackQueryHandler(slot_skip, pattern=r"^slot_skip:"),
            ],
            ASK_SLOT_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_slot)
            ],
            ASK_MAX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_max_agents)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_newot)],
        per_user=True,
        per_chat=True,
    )
