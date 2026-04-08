"""
Django models for the OT Signup Bot.
"""
from django.db import models


class OTEvent(models.Model):
    """An overtime event created by an admin."""

    title = models.CharField(max_length=255)
    created_by_telegram_id = models.BigIntegerField()
    # JSON list of day names, e.g. ["Monday", "Saturday", "Sunday"]
    days = models.JSONField(default=list)
    # JSON dict mapping day name -> list of hour options (floats)
    # e.g. {"Monday": [2, 4], "Saturday": [8, 10]}
    time_slots = models.JSONField(default=dict)
    # Maximum number of agents allowed to sign up (null = unlimited)
    max_agents = models.PositiveIntegerField(null=True, blank=True)
    is_open = models.BooleanField(default=True)
    deadline = models.DateTimeField(
        null=True, blank=True,
        help_text="Auto-close after this UTC datetime (null = no deadline)"
    )
    # The group chat where the announcement was posted
    group_chat_id = models.BigIntegerField()
    # Message ID of the announcement so we can reference it later
    announcement_message_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({'open' if self.is_open else 'closed'})"

    @property
    def signup_count(self):
        return self.signups.count()

    def is_full(self):
        if self.max_agents is None:
            return False
        return self.signup_count >= self.max_agents


class Agent(models.Model):
    """A call-centre agent who can sign up for OT."""

    telegram_id = models.BigIntegerField(unique=True)
    telegram_username = models.CharField(max_length=255, blank=True, null=True)
    agent_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.agent_name} (@{self.telegram_username})"


class OTSignup(models.Model):
    """Records an agent's signup for a particular OT event."""

    CLASS_DIALER = "DIALER"
    CLASS_IB = "IB"
    CLASS_TOPLIST = "TOPLIST"
    CLASS_SUPERVISOR = "SUPERVISOR"

    CLASS_CHOICES = [
        (CLASS_DIALER, "Dialer"),
        (CLASS_IB, "IB"),
        (CLASS_TOPLIST, "Toplist"),
        (CLASS_SUPERVISOR, "Supervisor"),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="signups")
    ot_event = models.ForeignKey(OTEvent, on_delete=models.CASCADE, related_name="signups")
    day = models.CharField(max_length=20)
    hours = models.DecimalField(max_digits=4, decimal_places=1)
    class_type = models.CharField(max_length=10, choices=CLASS_CHOICES)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One signup per agent per event per day (agents can sign up for multiple days)
        unique_together = [("agent", "ot_event", "day")]

    def __str__(self):
        return (
            f"{self.agent.agent_name} → {self.ot_event.title} "
            f"({self.day}, {self.hours}h, {self.class_type})"
        )


class AdminUser(models.Model):
    """Dynamically added bot admin (supplements ADMIN_IDS env var)."""

    telegram_id = models.BigIntegerField(unique=True)
    telegram_username = models.CharField(max_length=255, blank=True)
    telegram_name = models.CharField(max_length=255, blank=True)
    added_by = models.BigIntegerField()   # Telegram ID of who added them
    added_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        name = f"@{self.telegram_username}" if self.telegram_username else self.telegram_name
        return f"Admin {name} ({self.telegram_id})"
