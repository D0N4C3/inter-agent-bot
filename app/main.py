from __future__ import annotations

import logging
import mimetypes
import re
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.scoring import score_application
from app.services import (
    add_bot_admin,
    count_admins,
    delete_application_draft,
    get_application_draft,
    get_application,
    get_latest_status_by_phone,
    get_latest_status_by_telegram_user,
    get_applications,
    get_stale_drafts,
    is_bot_admin,
    mark_draft_reminder_sent,
    save_application,
    save_application_draft,
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

SUPPORTED_LANGUAGES = {"en", "am"}
ETHIOPIA_REGIONS = [
    "Addis Ababa",
    "Amhara",
    "Oromia",
    "Tigray",
    "Sidama",
    "SNNPR",
    "Afar",
    "Somali",
    "Dire Dawa",
]
YES_NO_KEYBOARD = [["Yes", "No"]]
YES_NO_KEYBOARD_AM = [["አዎ", "አይደለም"]]
LANGUAGE_KEYBOARD = [["English", "አማርኛ"]]

I18N = {
    "en": {
        "welcome": "Welcome to Inter Ethiopia Solutions Agent Registration Bot\n\nPlease choose your application type below.",
        "support": "For support, contact Inter Ethiopia Solutions\nEmail: agentapply@internethiopia.com\nPhone: +251XXXXXXXXX\nWhatsApp: +251XXXXXXXXX",
        "choose_language": "Please choose your language / ቋንቋ ይምረጡ",
        "resume_prompt": "You have an incomplete application. Would you like to resume where you stopped?",
        "resume_yes": "Resume Application",
        "resume_no": "Start New Application",
        "submitted": "Your application has been submitted successfully.",
        "timeline": settings.expected_review_timeline,
    },
    "am": {
        "welcome": "እንኳን ወደ Inter Ethiopia Solutions የወኪል ምዝገባ ቦት በደህና መጡ።\n\nእባክዎ የማመልከቻ አይነት ይምረጡ።",
        "support": "ለእገዛ Inter Ethiopia Solutions ያግኙ\nኢሜይል: agentapply@internethiopia.com\nስልክ: +251XXXXXXXXX\nዋትስአፕ: +251XXXXXXXXX",
        "choose_language": "Please choose your language / ቋንቋ ይምረጡ",
        "resume_prompt": "ያልተጠናቀቀ ማመልከቻ አለዎት። ከተወውበት ቦታ መቀጠል ይፈልጋሉ?",
        "resume_yes": "ማመልከቻ ቀጥል",
        "resume_no": "አዲስ ጀምር",
        "submitted": "ማመልከቻዎ በተሳካ ሁኔታ ተልኳል።",
        "timeline": "ቡድናችን በ3-5 የስራ ቀናት ውስጥ ምላሽ ይሰጣል።",
    },
}


def tr(user_id: int, key: str) -> str:
    lang = sessions.get(user_id, {}).get("language", "en")
    return I18N.get(lang, I18N["en"]).get(key, I18N["en"].get(key, key))

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
admin_sessions: dict[int, dict] = {}

ADMIN_MENU_KEYBOARD = [
    ["View Recent Applications", "Filter Applications"],
    ["Update Application Status", "Add Admin User"],
    ["Admin Dashboard Link", "Back to Main Menu"],
]


def start_keyboard_for_user(user_id: int) -> list[list[str]]:
    keyboard = [
        ["Register as Sales Agent"],
        ["Register as Installer"],
        ["Register as Both"],
        ["Check Territory Availability"],
        ["Check Application Status"],
        ["Contact Support"],
    ]
    if is_bot_admin(str(user_id)):
        keyboard.insert(0, ["Admin Management"])
    return keyboard


def support_keyboard() -> list[list[str]]:
    return [
        ["Email Support", "WhatsApp Support"],
        ["Call Support"],
    ]


def admin_menu_text() -> str:
    return (
        "Admin management menu:\n"
        "- View Recent Applications\n"
        "- Filter Applications\n"
        "- Update Application Status\n"
        "- Add Admin User\n"
        "- Admin Dashboard Link"
    )


async def show_admin_menu(chat_id: int, user_id: int) -> None:
    if not is_bot_admin(str(user_id)):
        await send_message(chat_id, "Only bot admins can access admin management.")
        return
    await send_message(chat_id, admin_menu_text(), keyboard=ADMIN_MENU_KEYBOARD)


def _format_application_summary(app_row: dict) -> str:
    return (
        f"ID: {app_row['application_id']}\n"
        f"Name: {app_row['full_name']}\n"
        f"Type: {app_row['applicant_type']}\n"
        f"Status: {app_row['status']}\n"
        f"Region: {app_row['region']}\n"
        f"Territory: {app_row['preferred_territory']}\n"
        f"Score: {app_row.get('qualification_score', 'N/A')} ({app_row.get('qualification_flag', 'N/A')})"
    )


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
    return normalized in {"yes", "y", "true", "አዎ"}


def yes_no_keyboard(user_id: int) -> list[list[str]]:
    lang = sessions.get(user_id, {}).get("language", "en")
    return YES_NO_KEYBOARD_AM if lang == "am" else YES_NO_KEYBOARD


def phone_is_valid(phone: str) -> bool:
    return bool(re.fullmatch(r"(\+251|0)?9\d{8}", phone.strip()))


async def ask_next(chat_id: int, user_id: int) -> None:
    session = sessions[user_id]
    index = session["step_index"]
    if index >= len(QUESTION_FLOW):
        await finalize_application(chat_id, user_id)
        return
    field, prompt = QUESTION_FLOW[index]
    if field in {"experience", "has_shop", "can_install"}:
        await send_message(chat_id, prompt, keyboard=yes_no_keyboard(user_id))
        return

    if field == "region":
        keyboard = [[region] for region in ETHIOPIA_REGIONS]
        await send_message(chat_id, prompt, keyboard=keyboard)
        return

    if field in {"zone", "woreda", "kebele", "village"}:
        prior = session["answers"].get(field)
        if prior:
            await send_message(chat_id, f"{prompt}\nSuggestion: {prior}")
            return

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
    delete_application_draft(str(user_id))
    await send_message(chat_id, f"{tr(user_id, 'submitted')}\n{tr(user_id, 'timeline')}")
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
        file_size = int(file_info["result"].get("file_size") or 0)
        max_size = settings.max_upload_size_mb * 1024 * 1024
        if file_size > max_size:
            await send_message(chat_id, f"File too large. Max size is {settings.max_upload_size_mb}MB.")
            return
        file_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"

        async with httpx.AsyncClient(timeout=30) as client:
            file_resp = await client.get(file_url)
            file_resp.raise_for_status()

        guessed_type = mimetypes.guess_type(f"file.{file_ext}")[0]
        content_type = guessed_type or "application/octet-stream"
        allowed_types = {"image/jpeg", "image/jpg", "image/png", "application/pdf"}
        if content_type not in allowed_types:
            await send_message(chat_id, "Unsupported file format. Please upload JPG, PNG, or PDF.")
            return

        if field == "id_front":
            filename = f"front-id-{secrets.token_hex(6)}.{file_ext}"
        elif field == "id_back":
            filename = f"back-id-{secrets.token_hex(6)}.{file_ext}"
        else:
            filename = f"profile-photo-{secrets.token_hex(6)}.{file_ext}"

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
        if value.lower() not in {"yes", "no", "y", "n", "አዎ", "አይደለም"}:
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
    save_application_draft(
        telegram_user_id=str(user_id),
        applicant_type=session["answers"]["applicant_type"],
        language=session.get("language", "en"),
        step_index=session["step_index"],
        answers=session["answers"],
    )
    await ask_next(chat_id, user_id)


async def process_admin_input(chat_id: int, user_id: int, text: str | None) -> bool:
    session = admin_sessions.get(user_id)
    if not session:
        return False
    if text is None:
        await send_message(chat_id, "Please provide text input for the admin action.")
        return True

    state = session.get("state")
    value = text.strip()

    if state == "await_filter":
        if value.lower() == "cancel":
            admin_sessions.pop(user_id, None)
            await show_admin_menu(chat_id, user_id)
            return True

        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 3:
            await send_message(chat_id, "Use format: region|applicant_type|status, or type Cancel.")
            return True

        region, applicant_type, status = [part or None for part in parts]
        apps = get_applications(region=region, applicant_type=applicant_type, status=status)
        if not apps:
            await send_message(chat_id, "No applications matched your filter.")
        else:
            previews = "\n\n---\n\n".join(_format_application_summary(item) for item in apps[:5])
            await send_message(chat_id, f"Top matches:\n\n{previews}")
        admin_sessions.pop(user_id, None)
        await show_admin_menu(chat_id, user_id)
        return True

    if state == "await_add_admin":
        if not value.isdigit():
            await send_message(chat_id, "Please enter a numeric telegram user id, or Cancel.")
            return True
        created, _ = add_bot_admin(value, created_by=str(user_id))
        if created:
            await send_message(chat_id, f"User {value} is now a bot admin.")
        else:
            await send_message(chat_id, f"User {value} is already a bot admin.")
        admin_sessions.pop(user_id, None)
        await show_admin_menu(chat_id, user_id)
        return True

    if state == "await_application_for_update":
        app_row = get_application(value)
        if not app_row:
            await send_message(chat_id, "Application ID not found. Try again or type Cancel.")
            return True
        session["application_id"] = value
        session["state"] = "await_status_update"
        await send_message(
            chat_id,
            "Reply using:\n<Status>|<Territory Village or blank>|<Admin note or blank>\n"
            "Example: Approved|Bole 05|Verified documents",
        )
        return True

    if state == "await_status_update":
        if value.lower() == "cancel":
            admin_sessions.pop(user_id, None)
            await show_admin_menu(chat_id, user_id)
            return True
        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 3:
            await send_message(chat_id, "Use format: <Status>|<Territory>|<Admin Note> or Cancel.")
            return True
        status, territory_village, admin_notes = parts
        try:
            updated = update_application_status(
                application_id=session["application_id"],
                status=status,
                admin_notes=admin_notes or None,
                territory_village=territory_village or None,
            )
        except ValueError as exc:
            await send_message(chat_id, f"Failed to update: {exc}")
            return True

        await send_message(chat_id, f"Application updated.\n\n{_format_application_summary(updated)}")
        admin_sessions.pop(user_id, None)
        await show_admin_menu(chat_id, user_id)
        return True

    return False


async def start_registration(chat_id: int, user_id: int, applicant_type: str, force_new: bool = False) -> None:
    lang = sessions.get(user_id, {}).get("language", "en")
    draft = None if force_new else get_application_draft(str(user_id))
    if draft and isinstance(draft.get("answers"), dict):
        sessions[user_id] = {
            "step_index": int(draft.get("step_index") or 0),
            "answers": draft["answers"],
            "language": draft.get("language") or lang,
            "resume_pending": False,
        }
        await send_message(chat_id, "Resuming your saved application.")
        await ask_next(chat_id, user_id)
        return

    sessions[user_id] = {
        "step_index": 0,
        "answers": {"applicant_type": applicant_type},
        "language": lang,
    }
    save_application_draft(
        telegram_user_id=str(user_id),
        applicant_type=applicant_type,
        language=lang,
        step_index=0,
        answers=sessions[user_id]["answers"],
    )
    await send_message(chat_id, "Great! Let's begin your registration.")
    await ask_next(chat_id, user_id)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs/remind-incomplete")
async def remind_incomplete_applications() -> dict:
    stale_drafts = get_stale_drafts(hours=24)
    sent = 0
    for draft in stale_drafts:
        user_id = int(draft["telegram_user_id"])
        sessions.setdefault(user_id, {})
        sessions[user_id]["language"] = draft.get("language") or "en"
        await send_message(
            user_id,
            f"Reminder: your application is incomplete.\n{tr(user_id, 'timeline')}\nSend /register to continue.",
        )
        mark_draft_reminder_sent(str(user_id))
        sent += 1
    return {"ok": True, "reminders_sent": sent}


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
            if user_id not in sessions:
                sessions[user_id] = {"language": "en"}
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            keyboard = start_keyboard_for_user(user_id)
            await send_message(chat_id, tr(user_id, "welcome"), keyboard=keyboard)
            return {"ok": True}

        if text in {"English", "አማርኛ"}:
            sessions.setdefault(user_id, {})
            sessions[user_id]["language"] = "en" if text == "English" else "am"
            await send_message(chat_id, tr(user_id, "welcome"), keyboard=start_keyboard_for_user(user_id))
            return {"ok": True}

        if text in {"/help", "/contact", "Contact Support"}:
            await send_message(chat_id, tr(user_id, "support"), keyboard=support_keyboard())
            return {"ok": True}

        if text in {"Email Support", "WhatsApp Support", "Call Support"}:
            channel_map = {
                "Email Support": "Email: agentapply@internethiopia.com",
                "WhatsApp Support": "WhatsApp: +251XXXXXXXXX",
                "Call Support": "Phone: +251XXXXXXXXX",
            }
            await send_message(chat_id, f"Support channel:\n{channel_map[text]}")
            return {"ok": True}

        if text == "/send":
            total_admins = count_admins()
            if total_admins == 0:
                add_bot_admin(str(user_id), created_by=str(user_id))
                await send_message(chat_id, "You are now the first bot admin.")
                return {"ok": True}

            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can use /send.")
                return {"ok": True}

            await send_message(
                chat_id,
                "Admin command is active. To assign another admin, use:\n/addadmin <telegram_user_id>",
            )
            return {"ok": True}

        if text and text.startswith("/addadmin"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can assign admins.")
                return {"ok": True}

            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().isdigit():
                await send_message(chat_id, "Usage: /addadmin <telegram_user_id>")
                return {"ok": True}

            target_user_id = parts[1].strip()
            created, _ = add_bot_admin(target_user_id, created_by=str(user_id))
            if created:
                await send_message(chat_id, f"User {target_user_id} is now a bot admin.")
            else:
                await send_message(chat_id, f"User {target_user_id} is already a bot admin.")
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

        if text in {"/territory", "Check Territory Availability"}:
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

        if text in {"/admin", "Admin Management", "/adminmenu"}:
            await show_admin_menu(chat_id, user_id)
            return {"ok": True}

        if text == "Back to Main Menu":
            await send_message(chat_id, "Back to main menu.", keyboard=start_keyboard_for_user(user_id))
            admin_sessions.pop(user_id, None)
            return {"ok": True}

        if text == "Admin Dashboard Link":
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can access admin features.")
                return {"ok": True}
            dashboard_url = "/admin"
            if settings.admin_dashboard_token:
                dashboard_url = f"/admin?token={settings.admin_dashboard_token}"
            await send_message(chat_id, f"Open admin dashboard:\n{dashboard_url}")
            return {"ok": True}

        if text == "View Recent Applications":
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can access admin features.")
                return {"ok": True}
            apps = get_applications()[:5]
            if not apps:
                await send_message(chat_id, "No applications yet.")
                return {"ok": True}
            previews = "\n\n---\n\n".join(_format_application_summary(item) for item in apps)
            await send_message(chat_id, f"Recent applications:\n\n{previews}")
            return {"ok": True}

        if text == "Filter Applications":
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can access admin features.")
                return {"ok": True}
            admin_sessions[user_id] = {"state": "await_filter"}
            await send_message(
                chat_id,
                "Send filters in format:\nregion|applicant_type|status\nUse blanks to skip.\nExample: Addis Ababa||Under Review\nType Cancel to stop.",
            )
            return {"ok": True}

        if text == "Update Application Status":
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can access admin features.")
                return {"ok": True}
            admin_sessions[user_id] = {"state": "await_application_for_update"}
            await send_message(chat_id, "Send the application ID you want to update, or type Cancel.")
            return {"ok": True}

        if text == "Add Admin User":
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, "Only bot admins can access admin features.")
                return {"ok": True}
            admin_sessions[user_id] = {"state": "await_add_admin"}
            await send_message(chat_id, "Send the telegram user ID to grant admin access, or type Cancel.")
            return {"ok": True}

        if text == "/register":
            existing_draft = get_application_draft(str(user_id))
            if existing_draft:
                sessions.setdefault(user_id, {})
                sessions[user_id]["resume_pending"] = True
                await send_message(chat_id, tr(user_id, "resume_prompt"), keyboard=[[tr(user_id, "resume_yes")], [tr(user_id, "resume_no")]])
                return {"ok": True}
            await send_message(
                chat_id,
                "Choose your application type: Sales Agent / Installer Agent / Sales + Installer Agent",
                keyboard=[["Register as Sales Agent"], ["Register as Installer"], ["Register as Both"]],
            )
            return {"ok": True}

        if text in {tr(user_id, "resume_yes"), tr(user_id, "resume_no")} and sessions.get(user_id, {}).get("resume_pending"):
            sessions[user_id]["resume_pending"] = False
            applicant_type = get_application_draft(str(user_id)).get("applicant_type", "sales_only")
            if text == tr(user_id, "resume_yes"):
                await start_registration(chat_id, user_id, applicant_type, force_new=False)
            else:
                delete_application_draft(str(user_id))
                await start_registration(chat_id, user_id, applicant_type, force_new=True)
            return {"ok": True}

        if text in APPLICANT_TYPE_BY_BUTTON:
            await start_registration(chat_id, user_id, APPLICANT_TYPE_BY_BUTTON[text])
            return {"ok": True}

        if user_id in admin_sessions:
            handled = await process_admin_input(chat_id, user_id, text)
            if handled:
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

    update_application_status(
        application_id=application_id,
        status=status,
        admin_notes=notes,
        territory_village=territory_village,
    )
    token = request.query_params.get("token")
    redirect_url = "/admin"
    if token:
        redirect_url = f"/admin?token={token}"
    return RedirectResponse(url=redirect_url, status_code=303)
