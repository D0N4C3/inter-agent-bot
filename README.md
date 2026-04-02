# Inter Ethiopia Agent Registration Bot (Phase 1 MVP)

Python + FastAPI Telegram webhook bot for Inter Ethiopia Solutions that:

- Guides users through a full agent application flow.
- Stores applications in Supabase (schema: `inter_agent_apply`).
- Uploads ID files and optional profile photo to Supabase Storage.
- Calculates qualification score + flag.
- Sends submission email notification via SMTP.
- Supports status checks from Telegram.

## 1) Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# then fill your real values in .env
```

> A `.env` file has been created locally for your provided credentials.

## 2) Run API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health endpoint:

```bash
GET /health
```

Telegram webhook endpoint:

```bash
POST /telegram/webhook
```

## 3) Configure Telegram webhook

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://<your-domain>/telegram/webhook"}'
```

## 4) Supabase SQL

Run `sql/schema.sql` in Supabase SQL editor to create required schema/tables.

## 5) Supported commands

- `/start`
- `/register`
- `/status` (or `/status <phone>`)
- `/territory <area>`
- `/help`
- `/contact`

## 6) Registration flow fields

1. Applicant type (`sales_only`, `installer_only`, `sales_installer`)
2. Full name
3. Mobile number
4. Region
5. Zone
6. Woreda
7. Kebele
8. Town / Village
9. Experience (Yes/No)
10. Experience years
11. Work type
12. Has shop/business (Yes/No)
13. Can install (Yes/No)
14. National ID front upload (required)
15. National ID back upload (required)
16. Profile photo (optional)
17. Preferred territory
18. Terms acceptance (`I Agree`)

## 7) Notes

- Territory availability is checked before final save.
- If locked, user is asked to choose another territory.
- Status values expected: `Submitted`, `Under Review`, `Approved`, `Rejected`, `More Information Required`.

## 8) Supabase key requirement

For backend/server usage with `supabase-py`, use a valid **Supabase `anon` JWT key** or **`service_role` / secret key** in `SUPABASE_KEY`.
Do **not** use a `sb_publishable_...` key here, as it can fail with `SupabaseException: Invalid API key` in server-side Python clients.

