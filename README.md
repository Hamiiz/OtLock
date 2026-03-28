# OT Signup Telegram Bot

A Telegram bot for overtime (OT) signup built with **Django 4.2** + **PostgreSQL** + **python-telegram-bot v21** + **uvicorn**.

The bot runs on **webhook** mode by default — when `WEBHOOK_URL` is set in `.env`, it auto-registers with Telegram on startup. When `WEBHOOK_URL` is absent, use polling mode via `run_bot`.

---

## Quick Start

### 1. Prerequisites
- Python 3.13
- A PostgreSQL database (e.g. [Supabase](https://supabase.com) free tier)
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
| `WEBHOOK_URL` | Public HTTPS base URL (no trailing slash). Leave blank for polling mode. |

> **⚠️ Special characters in passwords:** If your database password contains `@` or `#`, URL-encode them in `DATABASE_URL`: `@` → `%40`, `#` → `%23`.

> **Tip:** To find your Group Chat ID, forward a message from the group to [@userinfobot](https://t.me/userinfobot).

### 4. Set up the database

```bash
python manage.py migrate
python manage.py createsuperuser   # optional: for Django admin UI
```

### 5. Run the bot

#### Webhook mode (recommended for production and local dev)

Open two terminals:

```bash
# Terminal 1 — start tunnel (ngrok recommended; avoids challenge pages)
ngrok http 8000
# Copy the https URL (e.g. https://xxxx.ngrok-free.app) into WEBHOOK_URL in .env

# Terminal 2 — start the ASGI server (uvicorn required for async webhook)
uvicorn tgbot.asgi:application --reload --port 8000
```

Telegram webhook is registered automatically on startup.

#### Polling mode (offline / no tunnel)

Remove or comment out `WEBHOOK_URL` in `.env`, then:

```bash
python manage.py run_bot
```

---

## Bot Commands

### Admin Commands (only work for IDs listed in `ADMIN_IDS`)

| Command | Description |
|---|---|
| `/newot` | Start creating a new OT event (guided wizard) |
| `/status` | Check the current signup list without closing the event |
| `/remove` | Remove a specific agent from the signup list |
| `/closesignup` | Close the active OT event and review the final signup list |
| `/cancel` | Cancel the current wizard at any step |

### User Commands (private chat with the bot)

| Command / Action | Description |
|---|---|
| `/start` | Begin the OT signup flow |
| `/myot` | View your confirmed signups for the active OT event |
| `/cancel` | Cancel and exit the `/start` signup flow |

---

## Admin Flow

```
/newot
  → Enter title/description
  → Select days (multi-select toggle)
  → Set time slots per day (multi-select toggle: e.g. check both 2h and 4h)
       Saturday/Sunday: full shift (8h) + optional extra OT (+2h / +4h)
       Custom hours can also be typed and added to the list of choices
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
  → Select OT days (multi-select toggle, e.g., Sat + Sun)
  → Select hours per chosen day (loops through each selected day)
  → Select class type: Dialer / IB / Toplist (applies to all signups)
  → Review summary and confirm (⚠️ cannot cancel after confirming)
  → Signed up for all selected days! 🎉
```

**Returning users:** If you've used the bot before, your name is remembered — you go straight to day selection.

---

## Django Admin UI

Access at `http://127.0.0.1:8000/admin/` while the server is running.

Lets you inspect and manage all `OTEvent`, `Agent`, and `OTSignup` records.

---

## Project Structure

```
otlock/
├── manage.py
├── .env                  ← local config (not committed)
├── .env.example          ← config template
├── requirements.txt
├── Procfile              ← production: uvicorn tgbot.asgi:application
├── runtime.txt           ← python-3.13.0
├── tgbot/                ← Django project
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py           ← ASGI entry point (uvicorn / production)
│   └── wsgi.py           ← WSGI entry point (legacy)
└── bot/                  ← Django app
    ├── models.py         ← OTEvent, Agent, OTSignup
    ├── admin.py          ← Django admin config
    ├── apps.py           ← BotConfig: auto-registers webhook on startup
    ├── bot_app.py        ← Singleton PTB Application factory
    ├── views.py          ← Async webhook endpoint (/bot/webhook/)
    ├── urls.py           ← URL routing
    ├── utils.py          ← Keyboards, message formatters
    └── handlers/
        ├── admin_handlers.py   ← /newot, /closesignup, approve flow
        └── user_handlers.py    ← /start signup flow
```

---

## Deploying to Production

Set the following environment variables on your platform (Railway, Render, Heroku, etc.):

```
SECRET_KEY=...
DEBUG=False
ALLOWED_HOSTS=yourdomain.com
DATABASE_URL=postgresql://...
TELEGRAM_BOT_TOKEN=...
ADMIN_IDS=...
GROUP_CHAT_ID=...
WEBHOOK_URL=https://yourdomain.com
```

The `Procfile` runs `uvicorn tgbot.asgi:application` automatically.
