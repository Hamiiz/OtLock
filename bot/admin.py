"""
Django admin registration for OT Signup Bot models.
"""
from django.contrib import admin
from .models import OTEvent, Agent, OTSignup


@admin.register(OTEvent)
class OTEventAdmin(admin.ModelAdmin):
    list_display = ("title", "is_open", "max_agents", "signup_count", "created_at")
    list_filter = ("is_open",)
    readonly_fields = ("created_at", "announcement_message_id")


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("agent_name", "telegram_username", "telegram_id", "created_at")
    search_fields = ("agent_name", "telegram_username")


@admin.register(OTSignup)
class OTSignupAdmin(admin.ModelAdmin):
    list_display = ("agent", "ot_event", "day", "hours", "class_type", "confirmed_at")
    list_filter = ("ot_event", "day", "class_type")
    search_fields = ("agent__agent_name",)
