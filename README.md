# Inter Ethiopia Agent Registration Bot (Phase 5)

Python + Flask Telegram webhook bot for Inter Ethiopia Solutions with:

- Guided Telegram agent application flow.
- Territory conflict detection before submission and via `/territory` command.
- Qualification scoring with stronger candidate auto-flagging.
- Admin dashboard for operations and approvals.
- Admin runtime settings for training materials + mini-app default language.
- Territory locking on approval.
- Post-approval onboarding message (welcome + training + next steps).
- Agent lifecycle fields: agent tag, performance potential, internal remarks.
- Training delivery links configurable via environment.
- CSV/Excel exports from admin dashboard.
- SMTP email + Telegram admin alerts for new applications.
- English + Amharic language selection for user-facing flow.
- File validation (size/format) and safer randomized storage filenames.
- Telegram Mini App UI upgraded to a premium multi-workspace experience:
  registration, territory intelligence, agent dashboard/profile updates,
  training tracking, performance event entry, and live rankings.
- GPS nearest-territory suggestions.
- Agent-side dashboard APIs (status, territory, training, profile updates).
- Performance event tracking APIs for sales/installers.
- Ranking APIs for top sales agents and top installers.

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
- `TRAINING_PDF_URL` (optional): onboarding PDF guide link.
- `TRAINING_VIDEO_URL` (optional): onboarding video link.
- `SALES_PLAYBOOK_URL` (optional): sales playbook link.
- `GOOGLE_MAPS_SDK_KEY` (optional): enables Google Maps in the mini app territory workspace.

## 2) Run API

```bash
flask --app app.main run --host 0.0.0.0 --port 8000 --debug
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

## 3.1) Mini App + Platform APIs

- `GET /mini-app` — premium Telegram mini app for the full workflow:
  all agent/installer data entry via UI forms + map + dashboards.
  now includes a language selector (English/Amharic).
- `POST /api/mini-app/register` — register directly from mini app payload.
- `POST /api/mini-app/upload` — upload ID/profile files from device storage.
- `GET /api/territories/map` — map dataset (with coordinates, availability).
- `POST /api/territories/nearest` — GPS-based nearest available territories.
- `GET /api/agent/dashboard/<telegram_user_id>` — agent dashboard payload.
- `PATCH /api/agent/dashboard/<telegram_user_id>/profile` — agent profile updates.
- `POST /api/agent/training/<application_id>` — mark training module completion.
- `POST /api/performance/events` — admin-auth performance events (sales/jobs/training).
- `GET /api/rankings` — top sales and installer ranking feed.

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
- Track agent tagging and performance potential
- Manage training material links and upload files from admin dashboard
- Export dashboard data as CSV/Excel

## 5) Supabase SQL

Run `sql/schema.sql` in Supabase SQL editor.


## Codebase structure

To keep the project maintainable as features grow, the app now uses a modular layout:

- `app/main.py`: Telegram bot/webhook flow and application bootstrap.
- `app/web_module.py`: OOP-style `WebModule` class that encapsulates admin + mini-app web routes and auth helpers.
- `app/templates/admin_dashboard.html`: admin dashboard UI template.
- `app/templates/mini_app.html`: Telegram mini app UI template.
