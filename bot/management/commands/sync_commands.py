"""
Management command: python manage.py sync_commands

Syncs Telegram bot commands. Sets them for Private Chats and clears them for Group Chats.
"""
import sys
import asyncio
import logging

from django.core.management.base import BaseCommand
from django.conf import settings

from telegram import Bot, BotCommand
from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeDefault

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Sync Telegram bot commands (hide in groups, show in private)"

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stderr.write(self.style.ERROR("TELEGRAM_BOT_TOKEN not found in environment."))
            sys.exit(1)

        async def _sync_commands():
            bot = Bot(token)
            
            # The full list of commands for ALL users in Private chat
            # (Admins are protected by @admin_only decorator inside the handlers)
            private_commands = [
                BotCommand("start", "Start signup / Main Menu"),
                BotCommand("myot", "View your signups"),
                BotCommand("cancelot", "Cancel your signup"),
                BotCommand("newot", "Create a new OT Event (Admin)"),
                BotCommand("editot", "Edit active OT Event (Admin)"),
                BotCommand("closesignup", "Close active OT (Admin)"),
                BotCommand("status", "Check signup status (Admin)"),
                BotCommand("summary", "Get day-by-day summary (Admin)"),
                BotCommand("export", "Export signups to CSV (Admin)"),
                BotCommand("addadmin", "Add an Admin (SuperAdmin)"),
                BotCommand("removeadmin", "Remove an Admin (SuperAdmin)"),
                BotCommand("listadmins", "List all Admins (Admin)"),
                BotCommand("remove", "Remove a user's signup (Admin)"),
            ]

            # 1. Update commands for Private Chats
            self.stdout.write("Setting commands for Private Chats...")
            await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
            
            # 2. Clear commands for Group Chats
            self.stdout.write("Erasing commands from Group Chats...")
            await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())

            # 3. Clear default scope (just in case they were set globally via BotFather)
            self.stdout.write("Erasing default global commands...")
            await bot.set_my_commands([], scope=BotCommandScopeDefault())

        try:
            asyncio.run(_sync_commands())
            self.stdout.write(self.style.SUCCESS("✅ Successfully synced Telegram command scopes!"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to sync commands: {e}"))
