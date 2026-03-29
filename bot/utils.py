"""
Shared utilities for the OT Signup Bot.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _esc(text: str) -> str:
    """Escape special Telegram MarkdownV1 characters in user-supplied strings."""
    for char in ('*', '_', '`', '['):
        text = str(text).replace(char, f'\\{char}')
    return text

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKEND_DAYS = {"Saturday", "Sunday"}

# Standard weekday OT options (in hours)
WEEKDAY_SLOTS = [2.0, 4.0]
# Full regular shift hours for Sat/Sun
WEEKEND_FULL_SHIFT = 8.0
# Extra OT options that can be stacked on top of the weekend full shift
WEEKEND_EXTRA_SLOTS = [2.0, 4.0]

CLASS_TYPES = [
    ("DIALER", "Dialer"),
    ("IB", "IB"),
    ("TOPLIST", "Toplist"),
]


def days_keyboard(selected: List[str], available: List[str] = None) -> InlineKeyboardMarkup:
    """Build the day multi-select keyboard for admin OT creation."""
    day_list = available if available is not None else ALL_DAYS
    buttons = []
    for day in day_list:
        mark = "✅" if day in selected else "◻️"
        buttons.append(
            [InlineKeyboardButton(f"{mark} {day}", callback_data=f"day_toggle:{day}")]
        )
    buttons.append([InlineKeyboardButton("Done - Set Time Slots", callback_data="days_done")])
    return InlineKeyboardMarkup(buttons)


def slot_keyboard_weekday(day: str, selected: List[float]) -> InlineKeyboardMarkup:
    """Weekday slot selection for the admin — multi-select."""
    mark_2 = "✅" if 2.0 in selected else "◻️"
    mark_4 = "✅" if 4.0 in selected else "◻️"
    buttons = [
        [
            InlineKeyboardButton(f"{mark_2} 2 hrs", callback_data=f"slot_toggle:{day}:2"),
            InlineKeyboardButton(f"{mark_4} 4 hrs", callback_data=f"slot_toggle:{day}:4"),
        ],
        [InlineKeyboardButton("Add custom hours", callback_data=f"slot_custom:{day}")],
        [InlineKeyboardButton("Done for this day", callback_data=f"slot_done:{day}")],
    ]
    return InlineKeyboardMarkup(buttons)


def slot_keyboard_weekend(day: str, selected: List[float]) -> InlineKeyboardMarkup:
    """
    Weekend slot selection for the admin — multi-select.
    Weekend always includes the full shift (8 hrs).
    Admin can optionally add an extra OT block on top.
    """
    mark_8 = "✅" if 8.0 in selected else "◻️"
    mark_10 = "✅" if 10.0 in selected else "◻️"
    mark_12 = "✅" if 12.0 in selected else "◻️"
    buttons = [
        [InlineKeyboardButton(f"{mark_8} 8 hrs", callback_data=f"slot_toggle:{day}:8")],
        [
            InlineKeyboardButton(f"{mark_10} 10 hrs", callback_data=f"slot_toggle:{day}:10"),
            InlineKeyboardButton(f"{mark_12} 12 hrs", callback_data=f"slot_toggle:{day}:12"),
        ],
        [InlineKeyboardButton("Add custom hours", callback_data=f"slot_custom:{day}")],
        [InlineKeyboardButton("Done for this day", callback_data=f"slot_done:{day}")],
    ]
    return InlineKeyboardMarkup(buttons)


def user_day_multi_keyboard(available_days: List[str], selected: List[str]) -> InlineKeyboardMarkup:
    """Multi-select day keyboard for users — toggle style."""
    buttons = []
    for day in available_days:
        mark = "✅" if day in selected else "◻️"
        buttons.append(
            [InlineKeyboardButton(f"{mark} {day}", callback_data=f"uday_toggle:{day}")]
        )
    buttons.append([InlineKeyboardButton("Done - Select Hours", callback_data="udays_done")])
    return InlineKeyboardMarkup(buttons)


def user_hour_keyboard(day: str, slots: List[float]) -> InlineKeyboardMarkup:
    """Hour selection keyboard for a user, based on admin-configured slots."""
    buttons = [
        [InlineKeyboardButton(_hours_label(day, slot), callback_data=f"uhour:{slot}")]
        for slot in sorted(slots)
    ]
    return InlineKeyboardMarkup(buttons)


def _hours_label(day: str, hours: float) -> str:
    return f"{int(hours) if hours == int(hours) else hours} hrs"


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
                InlineKeyboardButton("Confirm - I understand I cannot cancel", callback_data="uconfirm:yes"),
                InlineKeyboardButton("Cancel", callback_data="uconfirm:no"),
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
    deadline_str = ""
    if event.deadline:
        deadline_str = f"\n*Signup closes:* {event.deadline.strftime('%d %b %Y %H:%M')} UTC"
    return (
        f"*OT ANNOUNCEMENT*\n\n"
        f"*{_esc(event.title)}*\n\n"
        f"*Available Days:*\n  {days_str}\n\n"
        f"*Time Slots:*\n{slots_block}\n\n"
        f"*Max Sign-ups:* {max_str}{deadline_str}\n\n"
        f"To sign up: open the bot in private chat and follow the prompts!"
    )


def format_signup_list(event, signups) -> str:
    """Format the final compiled sign-up list for the admin approval message."""
    if not signups:
        return f"*{_esc(event.title)}* - No agents signed up."

    from collections import defaultdict
    by_day = defaultdict(list)
    for signup in signups:
        by_day[signup.day].append(signup)

    lines = [f"*OT SIGN-UP LIST - {_esc(event.title)}*\n"]
    total_slots = len(signups)

    for day in event.days:
        if day not in by_day:
            continue
        lines.append(f"\n*{day}:*")
        for i, signup in enumerate(by_day[day], 1):
            hrs = float(signup.hours)
            class_label = dict(CLASS_TYPES).get(signup.class_type, signup.class_type)
            label = _hours_label(day, hrs)
            lines.append(
                f"  {i}. {_esc(signup.agent.agent_name)} - {label} - {class_label}"
            )

    # Count unique agents
    unique_agents = len({s.agent_id for s in signups})
    lines.append(f"\nTotal: {unique_agents} agent(s), {total_slots} slot(s)")
    return "\n".join(lines)


def approve_list_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Approve & Send to Group", callback_data=f"approve_list:{event_id}"
                )
            ]
        ]
    )
