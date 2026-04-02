from __future__ import annotations

import re
import logging
import mimetypes
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.scoring import score_application
from app.services import (
    get_latest_status_by_phone,
    get_latest_status_by_telegram_user,
    get_applications,
    save_application,
    send_admin_telegram_alert,
    send_notification_email,
    territory_is_available,
    update_application_status,
    upload_telegram_file,
    VALID_STATUSES,
    list_open_territories,
)

app = FastAPI(title="Inter Ethiopia Agent Registration Bot")
logger = logging.getLogger(__name__)

WELCOME_MESSAGE = """Welcome to Inter Ethiopia Solutions Agent Registration Bot

You can apply as:
Solar Sales Agent
Solar Installer Agent
Sales + Installer Agent

Benefits:
Commission opportunity
Training
Promotional support
Area-based registration

Please choose your application type below."""

SUPPORT_MESSAGE = """For support, contact Inter Ethiopia Solutions
Email: agentapply@internethiopia.com
Phone: +251XXXXXXXXX
WhatsApp: +251XXXXXXXXX"""

APPLICANT_TYPE_BY_BUTTON = {
    "Register as Sales Agent": "sales_only",
    "Register as Installer": "installer_only",
    "Register as Both": "sales_installer",
}

QUESTION_FLOW = [
    ("full_name", "Please enter your full name."),
    ("phone", "Please enter your mobile number (Ethiopian format, e.g. +2519XXXXXXXX)."),
    ("region", "Region?"),
    ("zone", "Zone?"),
    ("woreda", "Woreda?"),
    ("kebele", "Kebele?"),
    ("village", "Town / Village?"),
    ("experience", "Do you have experience? (Yes/No)"),
    ("experience_years", "If yes, how many years? If no, type 0."),
    ("work_type", "What type of work do you do?"),
    ("has_shop", "Do you have a shop or business? (Yes/No)"),
    ("can_install", "Can you install solar systems? (Yes/No)"),
    ("id_front", "Please upload your National ID front image/document."),
    ("id_back", "Please upload your National ID back image/document."),
    ("profile_photo", "Please upload profile photo (optional). Type skip to continue."),
    ("preferred_territory", "Select or type your preferred territory (same town/village or nearby area)."),
    ("terms", f"{settings.terms_text}\n\nReply with: I Agree or Cancel"),
]

sessions: dict[int, dict] = {}


async def telegram_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram API error: {data}")
    return data


async def send_message(chat_id: int, text: str, keyboard: list[list[str]] | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {
            "keyboard": [[{"text": button} for button in row] for row in keyboard],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    await telegram_api("sendMessage", payload)


def parse_yes_no(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"yes", "y", "true"}


def phone_is_valid(phone: str) -> bool:
    return bool(re.fullmatch(r"(\+251|0)?9\d{8}", phone.strip()))


async def ask_next(chat_id: int, user_id: int) -> None:
    session = sessions[user_id]
    index = session["step_index"]
    if index >= len(QUESTION_FLOW):
        await finalize_application(chat_id, user_id)
        return
    field, prompt = QUESTION_FLOW[index]

    if field == "preferred_territory":
        answers = session["answers"]
        options = list_open_territories(
            region=answers.get("region"),
            zone=answers.get("zone"),
            woreda=answers.get("woreda"),
        )
        keyboard = []
        for item in options[:6]:
            keyboard.append([item["village"]])
        if answers.get("village"):
            keyboard.insert(0, [answers["village"]])
        await send_message(chat_id, prompt, keyboard=keyboard or None)
        return

    await send_message(chat_id, prompt)


async def finalize_application(chat_id: int, user_id: int) -> None:
    session = sessions[user_id]
    answers = session["answers"]

    territory_valid = territory_is_available(
        answers["preferred_territory"],
        region=answers.get("region"),
        zone=answers.get("zone"),
        woreda=answers.get("woreda"),
        kebele=answers.get("kebele"),
    )
    if not territory_valid:
        session["step_index"] = next(i for i, (k, _) in enumerate(QUESTION_FLOW) if k == "preferred_territory")
        await send_message(chat_id, "Sorry, this area is already reserved. Please select another nearby area.")
        return

    answers["territory_valid"] = True
    score = score_application(answers)

    record = {
        "telegram_user_id": str(user_id),
        "full_name": answers["full_name"],
        "phone": answers["phone"],
        "applicant_type": answers["applicant_type"],
        "region": answers["region"],
        "zone": answers["zone"],
        "woreda": answers["woreda"],
        "kebele": answers["kebele"],
        "village": answers["village"],
        "experience": answers["experience"],
        "experience_years": answers["experience_years"],
        "work_type": answers["work_type"],
        "has_shop": answers["has_shop"],
        "can_install": answers["can_install"],
        "preferred_territory": answers["preferred_territory"],
        "id_file_front_url": answers["id_file_front_url"],
        "id_file_back_url": answers["id_file_back_url"],
        "profile_photo_url": answers.get("profile_photo_url"),
        "qualification_score": score.qualification_score,
        "qualification_flag": score.qualification_flag,
        "status": "Submitted",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    save_application(record)
    send_notification_email(record)
    send_admin_telegram_alert(record)
    await send_message(chat_id, "Your application has been submitted successfully.\nOur team will review it and contact you soon.")
    sessions.pop(user_id, None)


async def process_registration_input(chat_id: int, user_id: int, text: str | None, message: dict) -> None:
    session = sessions[user_id]
    field, _ = QUESTION_FLOW[session["step_index"]]

    if field in {"id_front", "id_back", "profile_photo"}:
        if field == "profile_photo" and text and text.strip().lower() == "skip":
            session["answers"]["profile_photo_url"] = None
            session["step_index"] += 1
            await ask_next(chat_id, user_id)
            return

        doc = message.get("document")
        photos = message.get("photo", [])
        if not doc and not photos:
            await send_message(chat_id, "Please upload a file or image.")
            return

        file_id = doc["file_id"] if doc else photos[-1]["file_id"]
        file_ext = "jpg"
        if doc and doc.get("file_name") and "." in doc["file_name"]:
            file_ext = doc["file_name"].split(".")[-1]

        file_info = await telegram_api("getFile", {"file_id": file_id})
        file_path = file_info["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"

        async with httpx.AsyncClient(timeout=30) as client:
            file_resp = await client.get(file_url)
            file_resp.raise_for_status()

        guessed_type = mimetypes.guess_type(f"file.{file_ext}")[0]
        content_type = guessed_type or "application/octet-stream"

        if field == "id_front":
            filename = f"front-id.{file_ext}"
        elif field == "id_back":
            filename = f"back-id.{file_ext}"
        else:
            filename = f"profile-photo.{file_ext}"

        uploaded_url = upload_telegram_file(
            file_resp.content,
            folder=f"applications/{user_id}",
            filename=filename,
            content_type=content_type,
            upsert=True,
        )

        if field == "id_front":
            session["answers"]["id_file_front_url"] = uploaded_url
        elif field == "id_back":
            session["answers"]["id_file_back_url"] = uploaded_url
        else:
            session["answers"]["profile_photo_url"] = uploaded_url

        session["step_index"] += 1
        await ask_next(chat_id, user_id)
        return

    if text is None:
        await send_message(chat_id, "Please send text input.")
        return

    value = text.strip()

    if field == "phone" and not phone_is_valid(value):
        await send_message(chat_id, "Invalid phone format. Use +2519XXXXXXXX or 09XXXXXXXX.")
        return

    if field in {"experience", "has_shop", "can_install"}:
        if value.lower() not in {"yes", "no", "y", "n"}:
            await send_message(chat_id, "Please reply Yes or No.")
            return
        session["answers"][field] = parse_yes_no(value)
    elif field == "experience_years":
        if not value.isdigit():
            await send_message(chat_id, "Please enter a valid number (0,1,2...).")
            return
        session["answers"][field] = int(value)
    elif field == "terms":
        if value.lower() == "cancel":
            sessions.pop(user_id, None)
            await send_message(chat_id, "Application cancelled.")
            return
        if value.lower() != "i agree":
            await send_message(chat_id, "Please type I Agree to continue or Cancel to stop.")
            return
        session["answers"]["terms_accepted"] = True
    else:
        if not value:
            await send_message(chat_id, "This field is required.")
            return
        session["answers"][field] = value

    session["step_index"] += 1
    await ask_next(chat_id, user_id)


async def start_registration(chat_id: int, user_id: int, applicant_type: str) -> None:
    sessions[user_id] = {
        "step_index": 0,
        "answers": {"applicant_type": applicant_type},
    }
    await send_message(chat_id, "Great! Let's begin your registration.")
    await ask_next(chat_id, user_id)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    try:
        update = await request.json()
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text")

        if text == "/start":
            keyboard = [
                ["Register as Sales Agent"],
                ["Register as Installer"],
                ["Register as Both"],
                ["Check Application Status"],
                ["Contact Support"],
            ]
            await send_message(chat_id, WELCOME_MESSAGE, keyboard=keyboard)
            return {"ok": True}

        if text in {"/help", "/contact", "Contact Support"}:
            await send_message(chat_id, SUPPORT_MESSAGE)
            return {"ok": True}

        if text and (text.startswith("/status") or text == "Check Application Status"):
            parts = text.split(maxsplit=1)
            status = None
            if len(parts) == 2:
                status = get_latest_status_by_phone(parts[1].strip())
            if status is None:
                status = get_latest_status_by_telegram_user(str(user_id))

            if status:
                await send_message(chat_id, f"Your application status: {status}\nOur team will contact you soon.")
            else:
                await send_message(chat_id, "No application found yet. Use /register to submit your application.")
            return {"ok": True}

        if text == "/territory":
            await send_message(chat_id, "Send /territory <TownOrVillage> to check if an area is reserved.")
            return {"ok": True}

        if text and text.startswith("/territory "):
            territory = text.replace("/territory", "", 1).strip()
            parts = [p.strip() for p in territory.split("|")]
            if len(parts) == 5:
                region, zone, woreda, kebele, village = parts
                available = territory_is_available(village, region=region, zone=zone, woreda=woreda, kebele=kebele)
            else:
                available = territory_is_available(territory)
            if available:
                await send_message(chat_id, "This territory is available.")
            else:
                await send_message(chat_id, "Sorry, this area is already reserved. Please select another nearby area.")
            return {"ok": True}

        if text == "/register":
            await send_message(
                chat_id,
                "Choose your application type: Sales Agent / Installer Agent / Sales + Installer Agent",
                keyboard=[["Register as Sales Agent"], ["Register as Installer"], ["Register as Both"]],
            )
            return {"ok": True}

        if text in APPLICANT_TYPE_BY_BUTTON:
            await start_registration(chat_id, user_id, APPLICANT_TYPE_BY_BUTTON[text])
            return {"ok": True}

        if user_id in sessions:
            await process_registration_input(chat_id, user_id, text, message)
            return {"ok": True}

        await send_message(chat_id, "Use /start to begin.")
    except Exception:
        logger.exception("Failed to handle telegram webhook update.")
        return {"ok": True}
    return {"ok": True}


def _require_admin(request: Request) -> None:
    expected = settings.admin_dashboard_token
    if not expected:
        return
    provided = request.query_params.get("token") or request.headers.get("x-admin-token")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    region: str | None = None,
    applicant_type: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    _require_admin(request)
    apps = get_applications(region=region, applicant_type=applicant_type, status=status)

    rows = []
    for app_row in apps:
        rows.append(
            f"""
            <tr>
                <td>{app_row['application_id']}</td>
                <td>{app_row['full_name']}</td>
                <td>{app_row['region']}</td>
                <td>{app_row['applicant_type']}</td>
                <td>{app_row['status']}</td>
                <td><a href=\"{app_row['id_file_front_url']}\" target=\"_blank\">Front</a> |
                    <a href=\"{app_row['id_file_back_url']}\" target=\"_blank\">Back</a> |
                    <a href=\"{app_row.get('profile_photo_url') or '#'}\" target=\"_blank\">Profile</a></td>
                <td>
                    <form method=\"post\" action=\"/admin/applications/{app_row['application_id']}/status?token={request.query_params.get('token','')}\">
                        <select name=\"status\">{''.join([f'<option value="{s}">{s}</option>' for s in sorted(VALID_STATUSES)])}</select>
                        <input name=\"territory_village\" placeholder=\"territory\" value=\"{app_row['preferred_territory']}\" />
                        <input name=\"admin_notes\" placeholder=\"internal notes\" value=\"{app_row.get('admin_notes') or ''}\" />
                        <button type=\"submit\">Update</button>
                    </form>
                </td>
            </tr>
            """
        )

    html = f"""
    <html><body>
    <h2>Agent Applications Dashboard</h2>
    <form method=\"get\" action=\"/admin\">
      <input type=\"hidden\" name=\"token\" value=\"{request.query_params.get('token', '')}\" />
      <input name=\"region\" value=\"{region or ''}\" placeholder=\"Region\" />
      <input name=\"applicant_type\" value=\"{applicant_type or ''}\" placeholder=\"Type\" />
      <input name=\"status\" value=\"{status or ''}\" placeholder=\"Status\" />
      <button type=\"submit\">Filter</button>
    </form>
    <table border=\"1\" cellpadding=\"6\">
      <tr><th>ID</th><th>Name</th><th>Region</th><th>Type</th><th>Status</th><th>Uploads</th><th>Actions</th></tr>
      {''.join(rows)}
    </table>
    </body></html>
    """
    return HTMLResponse(content=html)


@app.post("/admin/applications/{application_id}/status")
async def admin_update_status(
    application_id: str,
    request: Request,
) -> dict:
    _require_admin(request)
    form = await request.form()
    status = str(form.get("status") or "").strip()
    notes = str(form.get("admin_notes") or "").strip() or None
    territory_village = str(form.get("territory_village") or "").strip() or None

    updated = update_application_status(
        application_id=application_id,
        status=status,
        admin_notes=notes,
        territory_village=territory_village,
    )
    return {"ok": True, "application": updated}
