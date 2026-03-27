"""
Management command: python manage.py run_bot

Starts the Telegram bot using long-polling (no webhook needed).
"""
import logging

from django.core.management.base import BaseCommand
from django.conf import settings

from telegram.ext import Application, CallbackQueryHandler

from bot.handlers.admin_handlers import (
    build_admin_conversation,
    close_signup,
    approve_and_send,
)
from bot.handlers.user_handlers import build_user_conversation

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the OT Signup Telegram bot (long-polling)"

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stderr.write(
                self.style.ERROR(
                    "TELEGRAM_BOT_TOKEN is not set. Add it to your .env file."
                )
            )
            return

        self.stdout.write(self.style.SUCCESS("🤖 Starting OT Signup Bot…"))

        app = Application.builder().token(token).build()

        # Register admin conversation (/newot flow)
        app.add_handler(build_admin_conversation())

        # Register user conversation (/start flow)
        app.add_handler(build_user_conversation())

        # Standalone command handlers (outside conversation)
        from telegram.ext import CommandHandler
        app.add_handler(CommandHandler("closesignup", close_signup))

        # Inline button callbacks that live outside a conversation
        app.add_handler(
            CallbackQueryHandler(approve_and_send, pattern=r"^approve_list:")
        )

        self.stdout.write(self.style.SUCCESS("✅ Bot is running. Press Ctrl+C to stop."))
        app.run_polling(drop_pending_updates=True)
