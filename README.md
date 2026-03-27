# OT Signup Telegram Bot

A Telegram bot for overtime (OT) signup built with **Django 4.2** + **PostgreSQL** + **python-telegram-bot v20**.

---

## Quick Start

### 1. Prerequisites
- Python 3.11+
- PostgreSQL running locally (or any accessible Postgres server)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- The bot must be added as an **admin** in your announcement group

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `ADMIN_IDS` | Comma-separated Telegram user IDs of admins |
| `GROUP_CHAT_ID` | Negative chat ID of the announcement group |
| `DATABASE_URL` | PostgreSQL connection string |
| `SECRET_KEY` | Django secret key |

> **Tip:** To find your Group Chat ID, temporarily add `@userinfobot` to the group, or forward a group message to it.

### 4. Set up the database

```bash
python manage.py migrate
python manage.py createsuperuser   # optional: for Django admin UI
```

### 5. Run the bot

```bash
python manage.py run_bot
```

---

## Bot Commands

### Admin Commands (only work for IDs listed in `ADMIN_IDS`)

| Command | Description |
|---|---|
| `/newot` | Start creating a new OT event (guided wizard) |
| `/closesignup` | Close the active OT event and review the signup list |
| `/cancel` | Cancel the current wizard at any step |

### User Commands (private chat with the bot)

| Command / Action | Description |
|---|---|
| `/start` | Begin the OT signup flow |
| `/cancel` | Cancel and exit the signup flow |

---

## Admin Flow

```
/newot
  → Enter title/description
  → Select days (multi-select toggle)
  → Set time slots per day (2h / 4h / custom)
       Saturday/Sunday: full shift (8h) + optional extra OT (+2h / +4h)
  → Set max agents (or 0 for unlimited)
  → Bot posts announcement to group ✅
```

When signup is ready to close:
```
/closesignup
  → Bot compiles the agent list
  → Admin reviews + taps "Approve & Send"
  → Bot posts final list to group ✅
```

---

## User Flow

```
Open bot in private → /start
  → Enter agent name (saved + linked to Telegram ID)
  → Select OT day (buttons)
  → Select hours (buttons)
  → Select class type: Dialer / IB / Toplist
  → Confirm (⚠️ cannot cancel after confirming)
  → Signed up! 🎉
```

**Returning users:** If you've used the bot before, your name is remembered — you go straight to day selection.

---

## Django Admin UI

Access at `http://127.0.0.1:8000/admin/` after running `python manage.py runserver`.

Lets you inspect and manage all `OTEvent`, `Agent`, and `OTSignup` records.

---

## Project Structure

```
tgbot/
├── manage.py
├── .env.example
├── requirements.txt
├── tgbot/               ← Django project
│   ├── settings.py
│   └── urls.py
└── bot/                 ← Django app
    ├── models.py        ← OTEvent, Agent, OTSignup
    ├── admin.py         ← Django admin config
    ├── utils.py         ← Keyboards, message formatters
    └── handlers/
        ├── admin_handlers.py   ← /newot, /closesignup, approve flow
        └── user_handlers.py    ← /start signup flow
```
