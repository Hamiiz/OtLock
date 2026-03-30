import logging
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from django.conf import settings

from bot.handlers.admin_handlers import (
    build_admin_conversation,
    close_signup,
    approve_and_send,
    status_ot,
    remove_ot_start,
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
    admin_panel,
)
from telegram.ext import MessageHandler, filters
from bot.handlers.user_handlers import build_user_conversation, my_ot

logger = logging.getLogger(__name__)

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
            
        app = Application.builder().token(token).concurrent_updates(False).build()

        # Register admin conversation (/newot flow)
        app.add_handler(build_admin_conversation())

        # Register user conversation (/start flow)
        app.add_handler(build_user_conversation())

        # Standalone command handlers (outside conversation)
        app.add_handler(CommandHandler("admin", admin_panel))
        app.add_handler(CommandHandler("closesignup", close_signup))
        app.add_handler(MessageHandler(filters.Regex("^🚪 Close Signups$"), close_signup))
        
        app.add_handler(CommandHandler("status", status_ot))
        app.add_handler(MessageHandler(filters.Regex("^📊 Status$"), status_ot))
        
        app.add_handler(CommandHandler("remove", remove_ot_start))
        app.add_handler(MessageHandler(filters.Regex("^❌ Remove Agent$"), remove_ot_start))
        
        app.add_handler(CommandHandler("cancelot", cancel_event_start))
        app.add_handler(MessageHandler(filters.Regex("^🗑️ Cancel OT$"), cancel_event_start))
        
        app.add_handler(CommandHandler("summary", summary_ot))
        app.add_handler(MessageHandler(filters.Regex("^📈 Summary$"), summary_ot))
        
        app.add_handler(CommandHandler("export", export_ot))
        app.add_handler(MessageHandler(filters.Regex("^📥 Export Signups$"), export_ot))
        
        # User commands and existing specific commands
        app.add_handler(CommandHandler("myot", my_ot))
        app.add_handler(CommandHandler("addadmin", add_admin_cmd))
        app.add_handler(CommandHandler("removeadmin", remove_admin_start))
        app.add_handler(CommandHandler("listadmins", list_admins))
        app.add_handler(MessageHandler(filters.Regex("^👥 Manage Admins$"), list_admins))

        # Inline button callbacks that live outside a conversation
        app.add_handler(CallbackQueryHandler(approve_and_send, pattern=r"^approve_list:"))
        app.add_handler(CallbackQueryHandler(remove_agent_selected, pattern=r"^rm_agent:"))
        app.add_handler(CallbackQueryHandler(remove_day_callback, pattern=r"^rm_day:"))
        app.add_handler(CallbackQueryHandler(remove_back_to_agents, pattern=r"^rm_agent_back:"))
        app.add_handler(CallbackQueryHandler(cancel_event_confirm, pattern=r"^cancelot_confirm:"))
        app.add_handler(CallbackQueryHandler(cancel_event_confirm, pattern=r"^cancelot_abort$"))
        app.add_handler(CallbackQueryHandler(remove_admin_callback, pattern=r"^rmadmin:"))

        _ptb_application = app
    return _ptb_application
