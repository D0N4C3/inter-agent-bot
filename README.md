# Inter Ethiopia Agent Registration Bot (Phase 3)

Python + FastAPI Telegram webhook bot for Inter Ethiopia Solutions with:

- Guided Telegram agent application flow.
- Territory conflict detection before submission and via `/territory` command.
- Qualification scoring with stronger candidate auto-flagging.
- Admin dashboard for operations and approvals.
- Territory locking on approval.
- SMTP email + Telegram admin alerts for new applications.
- English + Amharic language selection for user-facing flow.
- Incomplete application draft recovery + reminder job endpoint.
- File validation (size/format) and safer randomized storage filenames.

## 1) Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Required env values (in addition to Phase 1):

- `ADMIN_TELEGRAM_CHAT_ID` (optional): chat id to receive new application alerts.
- `ADMIN_DASHBOARD_TOKEN` (optional): if set, required to access `/admin`.

## 2) Run API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 3) Telegram commands

- `/start`
- `/register`
- language selection via `/start` then choosing **English** or **አማርኛ**
- `/status` (or `/status <phone>`)
- `/territory` and `/territory <village>`
- `/territory <region|zone|woreda|kebele|village>`
- `/help`
- `/contact`
- `/admin` (opens admin management menu for bot admins)
- `/send` (bootstrap first admin if none exists)
- `/addadmin <telegram_user_id>` (admin only)

Main Telegram menu also includes:

- Check Territory Availability
- Contact Support options (Phone / WhatsApp / Email)
- Admin Management submenu for bot admins (view/filter/update/add admin/dashboard link)

Background job endpoint:

- `POST /jobs/remind-incomplete` (sends reminder messages for stale drafts).

## 4) Admin dashboard

Open:

- `/admin` (or `/admin?token=<ADMIN_DASHBOARD_TOKEN>` if token is configured)

Features:

- View applications
- Filter by region/type/status
- Open upload links
- Approve / Reject / Under Review / More Info Required
- Assign territory while approving
- Add internal notes

## 5) Supabase SQL

Run `sql/schema.sql` in Supabase SQL editor.
