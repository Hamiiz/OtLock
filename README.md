# OT Signup Telegram Bot & Dashboard

A comprehensive system for overtime (OT) signups and administration. Originally designed as a pure Telegram bot, it has been massively upgraded into a hybrid **Telegram Bot + Django Web Dashboard** platform.

Built with **Django 4.2**, **PostgreSQL** (Supabase), **python-telegram-bot v21**, and **Uvicorn** for production-grade ASGI serving.

---

## 🌟 Key Features

### 1. The Web Admin Dashboard
A sleek, responsive, glassmorphism-themed web interface for management:
- **`otlock.fly.dev/login/`**: Secure Django-authenticated portal.
- **Visual OT Creation**: Create complex multi-day OTs with precise hour configurations (e.g., locking weekdays to 2h/4h and weekends to 8h/10h/12h).
- **Instant Telegram Integration**: Hitting `Publish` on the web dashboard instantly syncs with Telegram and fires a beautifully formatted API Markdown announcement to the group chat—completely bypassing standard bot loops.

### 2. The Private Telegram Agent Experience
To reduce spam in the main group channel, the bot is explicitly configured to **only process commands in Private Direct Messages**. 
- Agents start by messaging the bot directly. 
- Features interactive inline keyboards for choosing Days, Hours, and Class Types (Dialer, IB, Toplists).
- The Telegram auto-complete command menu (`/star`, `/myot`) is injected directly into Private DM contexts and **erased** from the main group chat contexts. 

### 3. Deep Administrative Bot Tools
Fully-fledged administration exists natively inside Telegram using the `/newot` wizard, `/editot` dynamic updater, `/status` live-tracking, and `/closesignup` report compiler.

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.13
- A PostgreSQL database 
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- The bot must be added as an **admin** in your announcement group

### 2. Configure Environment

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in the following variables:
- `TELEGRAM_BOT_TOKEN`: Token from BotFather
- `ADMIN_IDS`: Comma-separated Telegram user IDs of the super admins
- `GROUP_CHAT_ID`: Negative chat ID of the announcement group
- `DATABASE_URL`: PostgreSQL connection string
- `SECRET_KEY`: Django secret key
- `WEBHOOK_URL`: Your Fly.io domain (e.g., `https://otlock.fly.dev`). Leave blank if running local polling.

### 3. Database & Admin Setup
```bash
python manage.py migrate
python manage.py createsuperuser   # Creates the login for the Web Dashboard
```

### 4. Sync Bot Commands
You must run this command *once* to push the interactive slash-menu to Telegram. This locks the commands to Private Messages and hides them in the group.
```bash
python manage.py sync_commands
```

### 5. Running Locally 

**Option A: Local Polling (No tunnels needed)**
1. Ensure `WEBHOOK_URL` is empty in `.env`.
2. Run bot: `python manage.py run_bot`
3. Run dashboard (new terminal): `python manage.py runserver`

**Option B: Production Webhooks**
The production server uses standard ASGI routing.
```bash
uvicorn tgbot.asgi:application --reload --port 8000
```
*(The webhook registers itself automatically as long as `WEBHOOK_URL` is configured).*

---

## 🛠 Command Reference

### User Commands (Private DM Only)
| Command | Description |
|---|---|
| `/start` | Begin the OT signup flow. Supports multi-day & multi-slot toggles. |
| `/myot` | View a clear list of what days and hours you have currently confirmed. |
| `/cancelot` | Exit out of an active wizard state. |

### Administrator Commands (Private DM Only)
| Command | Description |
|---|---|
| `/newot` | Wizard to create and announce a new multi-day OT event. |
| `/editot` | Interactive wizard that modifies the active OT (adds/removes days and shifts) and securely updates the original announcement without spamming the group. |
| `/status` | Instantly views the compiled table of signups at that exact moment. |
| `/remove` | Offers inline buttons to force-remove specific agent signups from the pool.  |
| `/closesignup` | Closes the active OT, locks out future signups, and publishes the final compiled Markdown table to the group chat. |
| `/summary` | Export an interactive day-by-day summary of participants. |
| `/export` | Generates and sends a downloadable CSV file of all signup data. |
| `/listadmins` | View the currently registered group of dynamic bot administrators. |
| `/addadmin` / `/removeadmin`  | Add or remove admin privileges dynamically. |

---

## 📂 Architecture overview

- **`bot/views.py`**: Central ASGI endpoint for the `/bot/webhook/` route. Contains Django views serving `bot/templates/bot/` for the web dashboard.
- **`bot/bot_app.py`**: Python-Telegram-Bot wrapper linking the webhook to `ConversationHandlers`. 
- **`bot/handlers/admin_handlers.py` & `user_handlers.py`**: Complex state machines defining the step-by-step chat wizard logic.
- **`bot/management/commands/`**: Contains `sync_commands.py` (menu builder) and `run_bot.py` (offline polling loop).
