"""
Shared utilities for the OT Signup Bot.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List

from django.utils import timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Telegram Bot API hard limit for sendMessage text.
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def split_text_for_telegram_messages(
    text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split text into parts each under max_len characters, preferring line breaks."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        if current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = []

    for line in text.split("\n"):
        trial = "\n".join(current_lines + [line]) if current_lines else line
        if len(trial) <= max_len:
            current_lines.append(line)
        else:
            flush()
            if len(line) <= max_len:
                current_lines.append(line)
            else:
                for i in range(0, len(line), max_len):
                    chunks.append(line[i : i + max_len])
    flush()
    return chunks


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
    ("SUPERVISOR", "Supervisor"),
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


def user_day_multi_keyboard(
    available_days: List[str],
    selected: List[str],
    session_id: str,
) -> InlineKeyboardMarkup:
    """Multi-select day keyboard for users — toggle style."""
    buttons = []
    for day in available_days:
        mark = "✅" if day in selected else "◻️"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} {day}",
                    callback_data=f"uday_toggle:{session_id}:{day}",
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                "Done - Select Hours",
                callback_data=f"udays_done:{session_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


def user_hour_keyboard(day: str, slots: List[float], session_id: str) -> InlineKeyboardMarkup:
    """Hour selection keyboard for a user, based on admin-configured slots."""
    buttons = [
        [
            InlineKeyboardButton(
                _hours_label(day, slot),
                callback_data=f"uhour:{session_id}:{slot}",
            )
        ]
        for slot in sorted(slots)
    ]
    return InlineKeyboardMarkup(buttons)


def _hours_label(day: str, hours: float) -> str:
    return f"{int(hours) if hours == int(hours) else hours} hrs"


def class_keyboard(session_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"uclass:{session_id}:{code}")]
        for code, label in CLASS_TYPES
    ]
    return InlineKeyboardMarkup(buttons)


def confirm_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm - I understand I cannot cancel",
                    callback_data=f"uconfirm:{session_id}:yes",
                ),
                InlineKeyboardButton("Cancel", callback_data=f"uconfirm:{session_id}:no"),
            ]
        ]
    )


def user_event_reply_keyboard(events) -> ReplyKeyboardMarkup:
    """Reply keyboard for message-based event picking."""
    buttons = [[KeyboardButton(f"OT {event.id}")] for event in events]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def user_days_reply_keyboard(available_days: list[str] | None = None) -> ReplyKeyboardMarkup:
    """Reply keyboard for day toggling + completion."""
    days = available_days if available_days is not None else ALL_DAYS
    buttons = [[KeyboardButton(day)] for day in days]
    buttons.append([KeyboardButton("Done")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def user_hours_reply_keyboard(slots: list[float]) -> ReplyKeyboardMarkup:
    """Reply keyboard for allowed hour values (one value per tap)."""
    def _fmt(h: float) -> str:
        return str(int(h)) if h == int(h) else str(h)

    hour_labels = [_fmt(float(s)) for s in sorted(set(slots))]
    buttons = [[KeyboardButton(lbl)] for lbl in hour_labels]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def user_class_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard for class type selection."""
    buttons = [[KeyboardButton(label)] for _code, label in CLASS_TYPES]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def user_confirm_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard for confirm/cancel."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Confirm"), KeyboardButton("Cancel")]],
        resize_keyboard=True,
        one_time_keyboard=False,
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
        local_dl = timezone.localtime(event.deadline)
        deadline_str = f"\n*Signup closes:* {local_dl.strftime('%d %b %Y %H:%M')} EAT"
    return (
        f"*OT ANNOUNCEMENT*\n\n"
        f"*{_esc(event.title)}*\n\n"
        f"*Available Days:*\n  {days_str}\n\n"
        f"*Time Slots:*\n{slots_block}\n\n"
        f"*Max Sign-ups:* {max_str}{deadline_str}"
    )


def announcement_keyboard(bot_username: str, event_id: int) -> InlineKeyboardMarkup | None:
    """Return an inline keyboard with a Sign Up button linking to bot DM. Returns
    None if bot_username is not configured, so callers can skip reply_markup safely."""
    if not bot_username:
        return None
    # Use Telegram deep-link with a payload so that opening the bot chat
    # will immediately trigger /start with an argument.
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "📝 Sign Up Now",
                url=f"https://t.me/{bot_username}?start=signup_{event_id}"
            )
        ]]
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


def select_event_keyboard(
    events,
    callback_prefix: str,
    session_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Generate a vertical list of buttons for selecting an active OT event."""
    buttons = []
    for event in events:
        days_preview = ", ".join(event.days[:2]) if getattr(event, "days", None) else ""
        if getattr(event, "days", None) and len(event.days) > 2:
            days_preview += ", ..."
        label = event.title
        if days_preview:
            label = f"{event.title} ({days_preview})"
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=(
                    f"{callback_prefix}:{session_id}:{event.id}"
                    if session_id
                    else f"{callback_prefix}:{event.id}"
                ),
            )
        ])
    return InlineKeyboardMarkup(buttons)


CLASS_TYPE_ORDER = ["TOPLIST", "IB", "DIALER", "SUPERVISOR"]


def generate_csv(event, signups) -> bytes:
    """
    Generate a CSV export with separate tables for each class type
    (Toplist, IB, Dialer). Each table uses *days as columns* and
    *agent names as rows*.
    """
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Pre-compute unique days and map for quick lookup
    days = list(event.days or [])
    day_indices = {day: idx for idx, day in enumerate(days)}

    # Group signups by (class_type, agent_name)
    from collections import defaultdict

    class_labels = dict(CLASS_TYPES)
    by_class_agent = defaultdict(lambda: defaultdict(dict))
    # structure: by_class_agent[class_type][agent_name][day] = hours

    for s in signups:
        if s.day not in day_indices:
            continue
        cls = s.class_type
        agent_name = s.agent.agent_name
        by_class_agent[cls][agent_name][s.day] = float(s.hours)

    # Write one table per class, in a stable order
    for idx, cls_code in enumerate(CLASS_TYPE_ORDER):
        agents = by_class_agent.get(cls_code)
        if not agents:
            continue

        # Blank line between tables (except before the first written section)
        if buf.tell() > 0:
            writer.writerow([])

        writer.writerow([class_labels.get(cls_code, cls_code)])
        header = ["Agent Name"] + days
        writer.writerow(header)

        for agent_name in sorted(agents.keys()):
            row = [agent_name]
            per_day = agents[agent_name]
            for day in days:
                hrs = per_day.get(day)
                row.append(hrs if hrs is not None else "")
            writer.writerow(row)

    return buf.getvalue().encode("utf-8")


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
