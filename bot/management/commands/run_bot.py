"""
Management command: python manage.py run_bot

Starts the Telegram bot using long-polling (no webhook needed).
"""
import logging

from django.core.management.base import BaseCommand
from django.conf import settings

from bot.bot_app import get_ptb_application

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

        app = get_ptb_application()
        if not app:
            self.stderr.write(self.style.ERROR("Failed to initialize bot application."))
            return
        
        self.stdout.write(self.style.SUCCESS("✅ Bot is running. Press Ctrl+C to stop."))
        app.run_polling(drop_pending_updates=True)
