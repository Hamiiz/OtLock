import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from django.conf import settings
from telegram.request import HTTPXRequest

from bot.handlers.admin_handlers import (
    build_admin_conversation,
    close_signup,
    close_signup_selected,
    close_signup_confirm_callback,
    approve_closure_callback,
    skip_closure_callback,
    scan_overdue_deadlines_on_startup,
    status_ot,
    remove_ot_start,
    remove_select_event_callback,
    remove_agent_selected,
    remove_day_callback,
    remove_back_to_agents,
    cancel_event_start,
    cancel_event_confirm,
    summary_ot,
    export_ot,
    add_admin_cmd,
    remove_admin_start,
    remove_admin_callback,
    list_admins,
    editot_start,
)
from bot.handlers.user_handlers import build_user_conversation, my_ot, _outdated_signup_callback

logger = logging.getLogger(__name__)


async def _ptb_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log and swallow handler exceptions so one bad update does not stall the bot."""
    logger.exception("Exception while handling Telegram update", exc_info=context.error)
    if not isinstance(update, Update):
        return
    try:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Something went wrong. Please try /start again."
            )
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(
                "Something went wrong. Please try /start again."
            )
    except Exception:
        logger.exception("Failed to notify user after handler error")


_ptb_application = None

def get_ptb_application():
    """
    Returns the singleton instance of the Telegram Application.
    Builds it on the first call.
    """
    global _ptb_application
    if _ptb_application is None:
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN is not set.")
            return None

        # Telegram requests can be slow/unreliable when testing locally (tunnels,
        # intermittent connectivity). Increase timeouts so handlers don't crash
        # on transient latency.
        request = HTTPXRequest(
            connect_timeout=15.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=15.0,
        )

        app = (
            Application.builder()
            .token(token)
            .request(request)
            # Sequential updates reduce lag and race issues with ConversationHandler.
            .concurrent_updates(False)
            .post_init(scan_overdue_deadlines_on_startup)
            .build()
        )
        app.add_error_handler(_ptb_error_handler)

        # Register admin conversation (/newot flow)
        app.add_handler(build_admin_conversation())

        # Register user conversation (/start flow)
        app.add_handler(build_user_conversation())

        # Standalone command handlers (outside conversation)
        app.add_handler(CommandHandler("closesignup", close_signup, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("status", status_ot, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("remove", remove_ot_start, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("cancelot", cancel_event_start, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("myot", my_ot, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("summary", summary_ot, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("export", export_ot, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("addadmin", add_admin_cmd, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("removeadmin", remove_admin_start, filters=filters.ChatType.PRIVATE))
        app.add_handler(CommandHandler("listadmins", list_admins, filters=filters.ChatType.PRIVATE))

        # Inline button callbacks that live outside a conversation
        app.add_handler(CallbackQueryHandler(remove_select_event_callback, pattern=r"^rm_event:"))
        app.add_handler(CallbackQueryHandler(remove_agent_selected, pattern=r"^rm_agent:"))
        app.add_handler(CallbackQueryHandler(remove_day_callback, pattern=r"^rm_day:"))
        app.add_handler(CallbackQueryHandler(remove_back_to_agents, pattern=r"^rm_agent_back:"))
        app.add_handler(CallbackQueryHandler(cancel_event_confirm, pattern=r"^cancelot_confirm:"))
        app.add_handler(CallbackQueryHandler(cancel_event_confirm, pattern=r"^cancelot_abort$"))
        app.add_handler(CallbackQueryHandler(remove_admin_callback, pattern=r"^rmadmin:"))
        app.add_handler(CallbackQueryHandler(close_signup_selected, pattern=r"^close_event:"))
        app.add_handler(CallbackQueryHandler(close_signup_confirm_callback, pattern=r"^close_confirm:"))
        app.add_handler(CallbackQueryHandler(close_signup_confirm_callback, pattern=r"^close_abort$"))
        app.add_handler(CallbackQueryHandler(approve_closure_callback, pattern=r"^approve_closure:"))
        app.add_handler(CallbackQueryHandler(skip_closure_callback, pattern=r"^skip_closure:"))

        # User inline callbacks from old keyboards (day/hour selection). We don't use them anymore,
        # but answering them prevents the Telegram loading spinner.
        app.add_handler(
            CallbackQueryHandler(
                _outdated_signup_callback,
                pattern=r"^(user_signup:|uday_toggle:|udays_done:|uhour:|uclass:|uconfirm:)",
            )
        )

        _ptb_application = app
    return _ptb_application
