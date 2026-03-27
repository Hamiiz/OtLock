"""
Shared utilities for the OT Signup Bot.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKEND_DAYS = {"Saturday", "Sunday"}

# Standard weekday OT options (in hours)
WEEKDAY_SLOTS = [2.0, 4.0]
# Full regular shift hours for Sat/Sun
WEEKEND_FULL_SHIFT = 8.0
# Extra OT options that can be stacked on top of the weekend full shift
WEEKEND_EXTRA_SLOTS = [2.0, 4.0]

CLASS_TYPES = [
    ("DIALER", "🎯 Dialer"),
    ("IB", "📞 IB"),
    ("TOPLIST", "📋 Toplist"),
]


def days_keyboard(selected: List[str]) -> InlineKeyboardMarkup:
    """Build the day multi-select keyboard for admin OT creation."""
    buttons = []
    for day in ALL_DAYS:
        mark = "✅" if day in selected else "◻️"
        buttons.append(
            [InlineKeyboardButton(f"{mark} {day}", callback_data=f"day_toggle:{day}")]
        )
    buttons.append([InlineKeyboardButton("✔️ Done – Set Time Slots", callback_data="days_done")])
    return InlineKeyboardMarkup(buttons)


def slot_keyboard_weekday(day: str) -> InlineKeyboardMarkup:
    """Weekday slot selection for the admin."""
    buttons = [
        [
            InlineKeyboardButton("2 hrs", callback_data=f"slot:{day}:2"),
            InlineKeyboardButton("4 hrs", callback_data=f"slot:{day}:4"),
        ],
        [InlineKeyboardButton("✏️ Custom hours", callback_data=f"slot:{day}:custom")],
        [InlineKeyboardButton("➡️ Next day", callback_data=f"slot_skip:{day}")],
    ]
    return InlineKeyboardMarkup(buttons)


def slot_keyboard_weekend(day: str) -> InlineKeyboardMarkup:
    """
    Weekend slot selection for the admin.
    Weekend always includes the full shift (8 hrs).
    Admin can optionally add an extra OT block on top.
    """
    buttons = [
        [InlineKeyboardButton("Full shift only (8h)", callback_data=f"slot:{day}:8")],
        [
            InlineKeyboardButton("Full shift + 2h extra", callback_data=f"slot:{day}:10"),
            InlineKeyboardButton("Full shift + 4h extra", callback_data=f"slot:{day}:12"),
        ],
        [InlineKeyboardButton("✏️ Custom hours", callback_data=f"slot:{day}:custom")],
        [InlineKeyboardButton("➡️ Next day", callback_data=f"slot_skip:{day}")],
    ]
    return InlineKeyboardMarkup(buttons)


def user_day_keyboard(available_days: List[str]) -> InlineKeyboardMarkup:
    """Keyboard showing available OT days for the user to pick."""
    buttons = [
        [InlineKeyboardButton(day, callback_data=f"uday:{day}")]
        for day in available_days
    ]
    return InlineKeyboardMarkup(buttons)


def user_hour_keyboard(day: str, slots: List[float]) -> InlineKeyboardMarkup:
    """Hour selection keyboard for a user, based on admin-configured slots."""
    buttons = []
    for slot in slots:
        label = _hours_label(day, slot)
        buttons.append([InlineKeyboardButton(label, callback_data=f"uhour:{slot}")])
    return InlineKeyboardMarkup(buttons)


def _hours_label(day: str, hours: float) -> str:
    if day in WEEKEND_DAYS:
        if hours == 8:
            return "8h – Full shift"
        elif hours == 10:
            return "8h full shift + 2h extra OT"
        elif hours == 12:
            return "8h full shift + 4h extra OT"
    return f"{int(hours) if hours == int(hours) else hours} hrs OT"


def class_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"uclass:{code}")]
        for code, label in CLASS_TYPES
    ]
    return InlineKeyboardMarkup(buttons)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm – I understand I cannot cancel", callback_data="uconfirm:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="uconfirm:no"),
            ]
        ]
    )


def format_announcement(event) -> str:
    """Format the OT announcement message sent to the group."""
    days_str = ", ".join(event.days) if event.days else "TBD"
    slots_lines = []
    for day, slots in event.time_slots.items():
        slot_str = " / ".join(_hours_label(day, float(s)) for s in sorted(set(slots)))
        slots_lines.append(f"  • {day}: {slot_str}")
    slots_block = "\n".join(slots_lines) if slots_lines else "  • See admin for details"
    max_str = str(event.max_agents) if event.max_agents else "Unlimited"
    return (
        f"📢 *OT ANNOUNCEMENT*\n\n"
        f"📋 *{event.title}*\n\n"
        f"📅 *Available Days:*\n  {days_str}\n\n"
        f"🕐 *Time Slots:*\n{slots_block}\n\n"
        f"👥 *Max Sign-ups:* {max_str}\n\n"
        f"To sign up: open the bot in private chat and follow the prompts!"
    )


def format_signup_list(event, signups) -> str:
    """Format the final compiled sign-up list for the admin approval message."""
    if not signups:
        return f"📋 *{event.title}* – No agents signed up."

    # Group by day
    from collections import defaultdict
    by_day = defaultdict(list)
    for signup in signups:
        by_day[signup.day].append(signup)

    lines = [f"✅ *OT SIGN-UP LIST – {event.title}*\n"]
    for day in event.days:
        if day not in by_day:
            continue
        lines.append(f"\n📅 *{day}:*")
        for i, signup in enumerate(by_day[day], 1):
            hrs = float(signup.hours)
            class_label = dict(CLASS_TYPES).get(signup.class_type, signup.class_type)
            label = _hours_label(day, hrs)
            lines.append(
                f"  {i}. {signup.agent.agent_name} — {label} — {class_label}"
            )

    lines.append(f"\n👥 Total: {len(signups)} agent(s)")
    return "\n".join(lines)


def approve_list_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve & Send to Group", callback_data=f"approve_list:{event_id}"
                )
            ]
        ]
    )
