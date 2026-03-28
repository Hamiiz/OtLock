import os
import logging
import threading
import asyncio

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class BotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bot"

    def ready(self):
        """
        Called when Django starts. Only runs in the main process (not the
        StatReloader watcher process) to avoid double-registration.
        If WEBHOOK_URL is set, registers the webhook with Telegram in a
        background daemon thread so startup is never blocked.
        """
        # Auto-register webhook with Telegram in a background thread
        # (This is safe to call repeatedly on startup)

        webhook_url = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

        if not webhook_url or not token:
            logger.info("WEBHOOK_URL not set — skipping webhook registration (polling mode).")
            return

        full_url = f"{webhook_url}/bot/webhook/"

        def _register():
            try:
                from telegram import Bot

                async def _set():
                    async with Bot(token) as bot:
                        await bot.set_webhook(url=full_url)
                    logger.info(f"✅ Telegram webhook registered: {full_url}")

                asyncio.run(_set())
            except Exception as exc:
                logger.warning(f"⚠️  Could not register webhook (will retry on next start): {exc}")

        thread = threading.Thread(target=_register, daemon=True)
        thread.start()
