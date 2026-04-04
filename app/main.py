from __future__ import annotations

import logging
import mimetypes
import re
import secrets
import asyncio
from datetime import datetime, timezone
from html import escape

import httpx
import csv
from io import StringIO

from flask import Flask, Response, abort, redirect, request

from app.config import settings
from app.scoring import score_application
from app.services import (
    add_bot_admin,
    count_admins,
    default_agent_tag,
    delete_application_draft,
    get_application_draft,
    get_application,
    get_agent_dashboard,
    get_latest_status_by_phone,
    get_latest_status_by_telegram_user,
    get_applications,
    get_rankings,
    get_stale_drafts,
    is_bot_admin,
    list_territories_for_map,
    mark_draft_reminder_sent,
    save_application,
    save_application_draft,
    send_admin_telegram_alert,
    send_notification_email,
    suggest_nearest_territories,
    territory_is_available,
    update_agent_profile,
    update_application_status,
    upsert_training_progress,
    upload_telegram_file,
    VALID_AGENT_TAGS,
    VALID_PERFORMANCE_EVENT_TYPES,
    VALID_STATUSES,
    create_performance_event,
    list_open_territories,
)

app = Flask(__name__)
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


def language_selection_pending(user_id: int) -> bool:
    return sessions.get(user_id, {}).get("awaiting_language", False)

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

VALID_PERFORMANCE_LEVELS = {"High", "Medium", "Low"}


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


def _is_image_url(url: str | None) -> bool:
    if not url:
        return False
    return url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))


async def send_application_preview(chat_id: int, app_row: dict) -> None:
    summary = (
        "🧾 Application Snapshot\n"
        f"ID: {app_row['application_id']}\n"
        f"👤 Name: {app_row['full_name']}\n"
        f"🧭 Type: {app_row['applicant_type']}\n"
        f"🔖 Status: {app_row['status']}\n"
        f"📍 Region: {app_row['region']}\n"
        f"🗺️ Territory: {app_row['preferred_territory']}\n"
        f"📊 Score: {app_row.get('qualification_score', 'N/A')} ({app_row.get('qualification_flag', 'N/A')})"
    )
    await send_message(chat_id, summary)

    uploads = [
        ("ID Front", app_row.get("id_file_front_url")),
        ("ID Back", app_row.get("id_file_back_url")),
        ("Profile", app_row.get("profile_photo_url")),
    ]
    for label, url in uploads:
        if not url:
            continue
        if _is_image_url(url):
            await send_photo(chat_id, url, caption=label)
        else:
            await send_message(chat_id, f"{label}: {url}")


async def telegram_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
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


async def send_photo(chat_id: int, photo_url: str, caption: str | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
    await telegram_api("sendPhoto", payload)


def send_post_approval_onboarding(application: dict) -> None:
    chat_id = application.get("telegram_user_id")
    if not chat_id:
        return
    text = (
        "🎉 Welcome to Inter Ethiopia Solutions!\n\n"
        "Your application has been approved.\n\n"
        "Training materials:\n"
        f"- Solar installation guide (PDF): {settings.training_pdf_url}\n"
        f"- Solar installation training video: {settings.training_video_url}\n"
        f"- Sales playbook: {settings.sales_playbook_url}\n\n"
        "Next steps:\n"
        "1) Review all training materials.\n"
        "2) Reply to this chat confirming completion.\n"
        "3) Wait for territory activation and manager onboarding call."
    )
    base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    httpx.post(f"{base_url}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=20)


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
        "agent_tag": default_agent_tag(answers["applicant_type"]),
        "performance_potential": "Medium",
        "internal_remarks": None,
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
            await send_message(chat_id, f"Top matches: {len(apps)} (showing up to 5 with media previews).")
            for item in apps[:5]:
                await send_application_preview(chat_id, item)
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
            "Reply using:\n"
            "<Status>|<Territory Village or blank>|<Admin note>|<Agent Tag>|<Performance Potential>|<Internal Remarks>\n"
            "Example: Approved|Bole 05|Verified docs|Hybrid|High|Fast learner",
        )
        return True

    if state == "await_status_update":
        if value.lower() == "cancel":
            admin_sessions.pop(user_id, None)
            await show_admin_menu(chat_id, user_id)
            return True
        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 6:
            await send_message(
                chat_id,
                "Use format: <Status>|<Territory>|<Admin Note>|<Agent Tag>|<Performance Potential>|<Internal Remarks> or Cancel.",
            )
            return True
        status, territory_village, admin_notes, agent_tag, performance_potential, internal_remarks = parts
        try:
            old_application = get_application(session["application_id"]) or {}
            updated = update_application_status(
                application_id=session["application_id"],
                status=status,
                admin_notes=admin_notes or None,
                territory_village=territory_village or None,
                agent_tag=agent_tag or None,
                performance_potential=performance_potential or None,
                internal_remarks=internal_remarks or None,
            )
        except ValueError as exc:
            await send_message(chat_id, f"Failed to update: {exc}")
            return True
        if old_application.get("status") != "Approved" and updated.get("status") == "Approved":
            send_post_approval_onboarding(updated)

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


@app.route("/health", methods=["GET"])
def health() -> dict:
    return {"status": "ok"}


async def _remind_incomplete_applications() -> dict:
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


@app.route("/jobs/remind-incomplete", methods=["POST"])
def remind_incomplete_applications() -> dict:
    return asyncio.run(_remind_incomplete_applications())


async def _telegram_webhook(update: dict) -> dict:
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text")

        if text == "/start":
            sessions.setdefault(user_id, {})
            sessions[user_id]["language"] = sessions[user_id].get("language", "en")
            sessions[user_id]["awaiting_language"] = True
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return {"ok": True}

        if text in {"English", "አማርኛ"}:
            sessions.setdefault(user_id, {})
            sessions[user_id]["language"] = "en" if text == "English" else "am"
            sessions[user_id]["awaiting_language"] = False
            await send_message(chat_id, tr(user_id, "welcome"), keyboard=start_keyboard_for_user(user_id))
            return {"ok": True}

        if language_selection_pending(user_id):
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
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
            await send_message(chat_id, "Recent applications (premium preview mode):")
            for item in apps:
                await send_application_preview(chat_id, item)
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


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook() -> dict:
    update = request.get_json(silent=True) or {}
    return asyncio.run(_telegram_webhook(update))


def _require_admin() -> None:
    expected = settings.admin_dashboard_token
    if not expected:
        return
    provided = request.args.get("token") or request.headers.get("x-admin-token")
    if provided != expected:
        abort(401, description="Unauthorized")


@app.route("/admin", methods=["GET"])
def admin_dashboard() -> Response:
    _require_admin()
    region = request.args.get("region")
    applicant_type = request.args.get("applicant_type")
    status = request.args.get("status")
    apps = get_applications(region=region, applicant_type=applicant_type, status=status)

    rows = []
    for app_row in apps:
        profile_url = app_row.get("profile_photo_url")
        uploads = []
        for label, url in (
            ("Front ID", app_row.get("id_file_front_url")),
            ("Back ID", app_row.get("id_file_back_url")),
            ("Profile", profile_url),
        ):
            if not url:
                continue
            safe_url = escape(url, quote=True)
            if _is_image_url(url):
                uploads.append(
                    f'<a class="thumb-link" href="{safe_url}" target="_blank" rel="noopener">'
                    f'<img src="{safe_url}" alt="{escape(label)}" class="thumb" /></a>'
                )
            else:
                uploads.append(f'<a href="{safe_url}" target="_blank" rel="noopener">{escape(label)}</a>')

        rows.append(
            f"""
            <tr class="app-row">
                <td>{escape(app_row['application_id'])}</td>
                <td>{escape(app_row['full_name'])}</td>
                <td>{escape(app_row['region'])}</td>
                <td>{escape(app_row['applicant_type'])}</td>
                <td>{escape(app_row.get('agent_tag') or '')}</td>
                <td><span class="status-badge">{escape(app_row['status'])}</span></td>
                <td>{escape(app_row.get('performance_potential') or '')}</td>
                <td>{escape(app_row.get('internal_remarks') or '')}</td>
                <td>
                    <div class="uploads">{''.join(uploads) if uploads else '<span class="muted">No uploads</span>'}</div>
                </td>
                <td>
                    <form method=\"post\" action=\"/admin/applications/{escape(app_row['application_id'])}/status?token={request.args.get('token','')}\">
                        <select name=\"status\">{''.join([f'<option value="{s}">{s}</option>' for s in sorted(VALID_STATUSES)])}</select>
                        <select name=\"agent_tag\">{''.join([f'<option value="{tag}" {"selected" if app_row.get("agent_tag")==tag else ""}>{tag}</option>' for tag in sorted(VALID_AGENT_TAGS)])}</select>
                        <select name=\"performance_potential\">{''.join([f'<option value="{p}" {"selected" if app_row.get("performance_potential")==p else ""}>{p}</option>' for p in sorted(VALID_PERFORMANCE_LEVELS)])}</select>
                        <input name=\"territory_village\" placeholder=\"territory\" value=\"{escape(app_row['preferred_territory'])}\" />
                        <input name=\"admin_notes\" placeholder=\"internal notes\" value=\"{escape(app_row.get('admin_notes') or '')}\" />
                        <input name=\"internal_remarks\" placeholder=\"remarks\" value=\"{escape(app_row.get('internal_remarks') or '')}\" />
                        <button type=\"submit\">Update</button>
                    </form>
                </td>
            </tr>
            """
        )

    html = f"""
    <html><head><style>
    body {{
      font-family: Inter, Arial, sans-serif; background: #f4f7fb; color: #1b2430; margin: 0; padding: 24px;
    }}
    .card {{ background: #fff; border-radius: 16px; padding: 18px; box-shadow: 0 8px 20px rgba(27,36,48,.08); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ border-bottom: 1px solid #e9eef5; padding: 10px; vertical-align: top; text-align: left; }}
    th {{ background: #f8fafe; }}
    .uploads {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .thumb {{ width: 74px; height: 74px; border-radius: 10px; object-fit: cover; border: 1px solid #d7e0ed; }}
    .status-badge {{ background: #ebf5ff; color: #165dff; border-radius: 999px; padding: 4px 10px; font-size: 12px; }}
    form {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    input, select, button {{ padding: 8px; border-radius: 8px; border: 1px solid #d7e0ed; }}
    button {{ background: #165dff; color: #fff; border: none; cursor: pointer; }}
    .muted {{ color: #8a94a3; }}
    </style></head><body>
    <div class="card">
    <h2>✨ Agent Applications Dashboard</h2>
    <form method=\"get\" action=\"/admin\">
      <input type=\"hidden\" name=\"token\" value=\"{request.args.get('token', '')}\" />
      <input name=\"region\" value=\"{region or ''}\" placeholder=\"Region\" />
      <input name=\"applicant_type\" value=\"{applicant_type or ''}\" placeholder=\"Type\" />
      <input name=\"status\" value=\"{status or ''}\" placeholder=\"Status\" />
      <button type=\"submit\">Filter</button>
    </form>
    <p>
      <a href="/admin/export.csv?token={request.args.get('token', '')}">Export CSV</a> |
      <a href="/admin/export.xlsx?token={request.args.get('token', '')}">Export Excel</a>
    </p>
    <table>
      <tr><th>ID</th><th>Name</th><th>Region</th><th>Type</th><th>Tag</th><th>Status</th><th>Potential</th><th>Remarks</th><th>Uploads</th><th>Actions</th></tr>
      {''.join(rows)}
    </table>
    </div></body></html>
    """
    return Response(html, mimetype="text/html")


@app.route("/admin/applications/<application_id>/status", methods=["POST"])
def admin_update_status(application_id: str):
    _require_admin()
    form = request.form
    status = str(form.get("status") or "").strip()
    notes = str(form.get("admin_notes") or "").strip() or None
    territory_village = str(form.get("territory_village") or "").strip() or None
    agent_tag = str(form.get("agent_tag") or "").strip() or None
    performance_potential = str(form.get("performance_potential") or "").strip() or None
    internal_remarks = str(form.get("internal_remarks") or "").strip() or None

    previous = get_application(application_id) or {}
    updated = update_application_status(
        application_id=application_id,
        status=status,
        admin_notes=notes,
        territory_village=territory_village,
        agent_tag=agent_tag,
        performance_potential=performance_potential,
        internal_remarks=internal_remarks,
    )
    if previous.get("status") != "Approved" and updated.get("status") == "Approved":
        send_post_approval_onboarding(updated)
    token = request.args.get("token")
    redirect_url = "/admin"
    if token:
        redirect_url = f"/admin?token={token}"
    return redirect(redirect_url, code=303)


@app.route("/admin/export.csv", methods=["GET"])
@app.route("/admin/export.xlsx", methods=["GET"])
def admin_export() -> Response:
    _require_admin()
    apps = get_applications(
        region=request.args.get("region"),
        applicant_type=request.args.get("applicant_type"),
        status=request.args.get("status"),
    )
    output = StringIO()
    fieldnames = [
        "application_id",
        "full_name",
        "phone",
        "applicant_type",
        "agent_tag",
        "status",
        "region",
        "zone",
        "woreda",
        "kebele",
        "village",
        "preferred_territory",
        "qualification_score",
        "qualification_flag",
        "performance_potential",
        "admin_notes",
        "internal_remarks",
        "submitted_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in apps:
        writer.writerow({key: row.get(key) for key in fieldnames})

    content = output.getvalue()
    is_excel = request.path.endswith(".xlsx")
    mimetype = "application/vnd.ms-excel" if is_excel else "text/csv"
    filename = "agent_lifecycle_export.xlsx" if is_excel else "agent_lifecycle_export.csv"
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/mini-app", methods=["GET"])
def mini_app() -> Response:
    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{settings.mini_app_name}</title>
      <style>
        :root {{
          --brand: {settings.mini_app_primary_color};
          --bg: #0f172a;
          --card: rgba(15, 23, 42, 0.62);
          --line: rgba(148, 163, 184, 0.28);
          --text: #e2e8f0;
          --muted: #94a3b8;
          --ok: #16a34a;
          --warn: #eab308;
          --danger: #dc2626;
        }}
        * {{ box-sizing: border-box; }}
        body {{
          margin: 0;
          font-family: Inter, ui-sans-serif, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
          color: var(--text);
          background:
            radial-gradient(circle at 20% 0%, rgba(59,130,246,.24), transparent 40%),
            radial-gradient(circle at 80% 10%, rgba(34,197,94,.14), transparent 35%),
            linear-gradient(180deg, #020617 0%, #0b1120 70%, #020617 100%);
          min-height: 100vh;
          padding: 20px;
        }}
        .shell {{ max-width: 1200px; margin: 0 auto; }}
        .hero {{
          display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
          margin-bottom: 16px;
        }}
        .hero-card {{
          background: linear-gradient(140deg, rgba(30,41,59,.85), rgba(15,23,42,.7));
          border: 1px solid var(--line);
          border-radius: 18px;
          padding: 18px;
          flex: 1;
          min-width: 260px;
          box-shadow: 0 16px 35px rgba(0,0,0,.3);
        }}
        .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
        .kpi {{ border: 1px solid var(--line); border-radius: 14px; padding: 10px; background: rgba(15,23,42,.45); }}
        .kpi b {{ display: block; font-size: 18px; margin-bottom: 4px; }}
        .grid {{ display: grid; grid-template-columns: 260px 1fr; gap: 14px; }}
        .nav, .panel {{
          border: 1px solid var(--line);
          border-radius: 16px;
          background: var(--card);
          backdrop-filter: blur(8px);
        }}
        .nav {{ padding: 10px; position: sticky; top: 12px; height: fit-content; }}
        .nav button {{
          width: 100%;
          margin: 6px 0;
          border-radius: 10px;
          border: 1px solid var(--line);
          background: rgba(15,23,42,.5);
          color: var(--text);
          font-weight: 600;
          padding: 10px;
          cursor: pointer;
          text-align: left;
        }}
        .nav button.active {{ background: linear-gradient(90deg, var(--brand), #1d4ed8); border-color: transparent; }}
        .panel {{ padding: 16px; }}
        .tab {{ display: none; }}
        .tab.active {{ display: block; }}
        .section-title {{ margin: 0 0 8px; font-size: 20px; }}
        .muted {{ color: var(--muted); font-size: 14px; margin-top: 0; }}
        .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }}
        label {{ font-size: 12px; color: #cbd5e1; display: block; margin-bottom: 4px; }}
        input, select, textarea, button {{
          width: 100%;
          border: 1px solid var(--line);
          border-radius: 10px;
          background: rgba(15,23,42,.48);
          color: var(--text);
          padding: 10px;
        }}
        textarea {{ min-height: 92px; resize: vertical; }}
        button {{
          background: linear-gradient(90deg, var(--brand), #1d4ed8);
          border: 0;
          font-weight: 700;
          cursor: pointer;
        }}
        .secondary {{ background: rgba(15,23,42,.4); border: 1px solid var(--line); }}
        #map {{ height: 280px; border-radius: 14px; margin: 12px 0; border: 1px solid var(--line); }}
        pre {{
          white-space: pre-wrap; word-break: break-word; font-size: 12px;
          background: rgba(2,6,23,.6); border: 1px solid var(--line); border-radius: 10px; padding: 10px;
        }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; }}
        .pill {{ padding: 3px 9px; border-radius: 999px; font-size: 12px; display: inline-block; }}
        .ok {{ background: rgba(22,163,74,.2); color: #86efac; }}
        .warn {{ background: rgba(234,179,8,.18); color: #fde047; }}
        .danger {{ background: rgba(220,38,38,.2); color: #fecaca; }}
        @media (max-width: 860px) {{
          .grid {{ grid-template-columns: 1fr; }}
          .nav {{ position: static; }}
        }}
      </style>
      <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    </head>
    <body>
      <div class="shell">
        <div class="hero">
          <div class="hero-card">
            <h2 style="margin-top:0">⚡ {settings.mini_app_name}</h2>
            <p class="muted">Premium mini app to operate the full agent funnel: registration, territory intelligence, dashboards, training, performance tracking, and rankings.</p>
            <div class="kpis">
              <div class="kpi"><b id="kpiTerritories">0</b><span class="muted">Mapped territories</span></div>
              <div class="kpi"><b id="kpiOpen">0</b><span class="muted">Open territories</span></div>
              <div class="kpi"><b id="kpiLocked">0</b><span class="muted">Locked territories</span></div>
            </div>
          </div>
        </div>
        <div class="grid">
          <aside class="nav">
            <button class="tab-btn active" data-tab="registration">🧾 Registration</button>
            <button class="tab-btn" data-tab="territories">🗺 Territory Intelligence</button>
            <button class="tab-btn" data-tab="agent">👤 Agent Dashboard</button>
            <button class="tab-btn" data-tab="training">🎓 Training Progress</button>
            <button class="tab-btn" data-tab="performance">📈 Performance Input</button>
            <button class="tab-btn" data-tab="rankings">🏆 Rankings</button>
          </aside>
          <main class="panel">
            <section id="tab-registration" class="tab active">
              <h3 class="section-title">Agent / Installer Application</h3>
              <p class="muted">All candidate data is collected from this form. Fields are mapped to your existing bot schema and API.</p>
              <div class="form-grid">
                <div><label>Telegram User ID</label><input id="telegram_user_id" /></div>
                <div><label>Full name</label><input id="full_name" /></div>
                <div><label>Phone</label><input id="phone" placeholder="+2519..." /></div>
                <div><label>Applicant type</label><select id="applicant_type"><option value="sales_only">Sales Agent</option><option value="installer_only">Installer</option><option value="sales_installer" selected>Both</option></select></div>
                <div><label>Region</label><input id="region" /></div>
                <div><label>Zone</label><input id="zone" /></div>
                <div><label>Woreda</label><input id="woreda" /></div>
                <div><label>Kebele</label><input id="kebele" /></div>
                <div><label>Village</label><input id="village" /></div>
                <div><label>Preferred territory</label><input id="preferred_territory" /></div>
                <div><label>Experience</label><select id="experience"><option value="true">Yes</option><option value="false">No</option></select></div>
                <div><label>Experience years</label><input id="experience_years" type="number" min="0" value="0" /></div>
                <div><label>Work type</label><input id="work_type" placeholder="Sales, install, technician..." /></div>
                <div><label>Has shop/business</label><select id="has_shop"><option value="true">Yes</option><option value="false">No</option></select></div>
                <div><label>Can install solar</label><select id="can_install"><option value="true">Yes</option><option value="false">No</option></select></div>
                <div><label>ID front URL</label><input id="id_file_front_url" placeholder="https://..." /></div>
                <div><label>ID back URL</label><input id="id_file_back_url" placeholder="https://..." /></div>
                <div><label>Profile photo URL (optional)</label><input id="profile_photo_url" placeholder="https://..." /></div>
              </div>
              <button onclick="submitRegistration()">Submit Registration</button>
            </section>

            <section id="tab-territories" class="tab">
              <h3 class="section-title">Territory Intelligence</h3>
              <p class="muted">Visualize open vs locked territories, pick map location, and get nearest suggestions from GPS.</p>
              <div class="form-grid">
                <div><label>Region filter</label><input id="filter_region" /></div>
                <div><label>Zone filter</label><input id="filter_zone" /></div>
                <div><label>Woreda filter</label><input id="filter_woreda" /></div>
              </div>
              <button class="secondary" onclick="loadTerritories()">Refresh map data</button>
              <button onclick="suggestNearest()">Suggest nearest territories by GPS</button>
              <div id="map"></div>
            </section>

            <section id="tab-agent" class="tab">
              <h3 class="section-title">Agent Dashboard Lookup + Profile Update</h3>
              <div class="form-grid">
                <div><label>Telegram User ID</label><input id="dashboard_user_id" /></div>
              </div>
              <button onclick="loadDashboard()">Load Dashboard</button>
              <div class="form-grid">
                <div><label>Full name</label><input id="upd_full_name" /></div>
                <div><label>Phone</label><input id="upd_phone" /></div>
                <div><label>Region</label><input id="upd_region" /></div>
                <div><label>Preferred territory</label><input id="upd_territory" /></div>
              </div>
              <button class="secondary" onclick="updateProfile()">Update Profile</button>
            </section>

            <section id="tab-training" class="tab">
              <h3 class="section-title">Training Completion</h3>
              <div class="form-grid">
                <div><label>Application ID</label><input id="training_application_id" /></div>
                <div><label>Module key</label><input id="training_module_key" placeholder="safety_intro" /></div>
                <div><label>Completed</label><select id="training_completed"><option value="true">Yes</option><option value="false">No</option></select></div>
              </div>
              <button onclick="markTraining()">Save Training Progress</button>
            </section>

            <section id="tab-performance" class="tab">
              <h3 class="section-title">Performance Event Entry (Admin)</h3>
              <div class="form-grid">
                <div><label>Application ID</label><input id="perf_application_id" /></div>
                <div><label>Event type</label><select id="perf_event_type"><option value="sale_closed">Sale Closed</option><option value="installer_job_completed">Installer Job Completed</option><option value="training_completed">Training Completed</option></select></div>
                <div><label>Event value</label><input id="perf_event_value" type="number" value="1" step="0.01" /></div>
                <div><label>Occurred at (ISO optional)</label><input id="perf_occurred_at" placeholder="2026-04-04T12:00:00Z" /></div>
                <div style="grid-column: 1 / -1"><label>Metadata JSON</label><textarea id="perf_metadata">{{}}</textarea></div>
              </div>
              <button onclick="submitPerformance()">Submit Event</button>
            </section>

            <section id="tab-rankings" class="tab">
              <h3 class="section-title">Top Performers</h3>
              <button onclick="loadRankings()">Refresh Rankings</button>
              <div id="rankingsContainer"></div>
            </section>

            <h4 style="margin-bottom:8px">API Output</h4>
            <pre id="result"></pre>
          </main>
        </div>
      </div>
      <script>
        function asBool(id) {{
          return document.getElementById(id).value === 'true';
        }}
        function setResult(obj) {{
          document.getElementById('result').innerText = JSON.stringify(obj, null, 2);
        }}
        document.querySelectorAll('.tab-btn').forEach((btn) => {{
          btn.addEventListener('click', () => {{
            document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
            document.querySelectorAll('.tab').forEach((s) => s.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(`tab-${{btn.dataset.tab}}`).classList.add('active');
          }});
        }});
        const map = L.map('map').setView([8.9806, 38.7578], 6);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 19 }}).addTo(map);
        let territoryMarkers = [];
        let pickedLatLng = null;
        map.on('click', (e) => {{
          pickedLatLng = e.latlng;
          setResult({{picked_location: [e.latlng.lat.toFixed(6), e.latlng.lng.toFixed(6)]}});
        }});
        async function loadTerritories() {{
          const qp = new URLSearchParams();
          ['region','zone','woreda'].forEach((k) => {{
            const v = document.getElementById(`filter_${{k}}`)?.value?.trim();
            if (v) qp.set(k, v);
          }});
          const res = await fetch(`/api/territories/map?${{qp.toString()}}`);
          const data = await res.json();
          territoryMarkers.forEach(m => m.remove());
          territoryMarkers = [];
          const items = data.items || [];
          document.getElementById('kpiTerritories').innerText = items.length;
          document.getElementById('kpiOpen').innerText = items.filter((t) => !t.is_locked).length;
          document.getElementById('kpiLocked').innerText = items.filter((t) => t.is_locked).length;
          (data.items || []).forEach((t) => {{
            if (t.latitude == null || t.longitude == null) return;
            const color = t.is_locked ? '#e74c3c' : '#2ecc71';
            const marker = L.circleMarker([t.latitude, t.longitude], {{radius: 7, color}}).addTo(map)
              .bindPopup(`${{t.village}} (${{t.availability_status || (t.is_locked ? 'locked' : 'open')}})`);
            territoryMarkers.push(marker);
          }});
          setResult(data);
        }}
        async function suggestNearest() {{
          if (!navigator.geolocation) {{
            setResult({{ok: false, error: 'Geolocation not available on this device/browser.'}});
            return;
          }}
          navigator.geolocation.getCurrentPosition(async (pos) => {{
            const r = await fetch('/api/territories/nearest', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{latitude: pos.coords.latitude, longitude: pos.coords.longitude}})
            }});
            setResult(await r.json());
          }});
        }}
        async function submitRegistration() {{
          const payload = {{
            telegram_user_id: document.getElementById('telegram_user_id').value,
            full_name: document.getElementById('full_name').value,
            phone: document.getElementById('phone').value,
            region: document.getElementById('region').value,
            zone: document.getElementById('zone').value,
            woreda: document.getElementById('woreda').value,
            kebele: document.getElementById('kebele').value,
            village: document.getElementById('village').value,
            preferred_territory: document.getElementById('preferred_territory').value,
            applicant_type: document.getElementById('applicant_type').value,
            experience: asBool('experience'),
            experience_years: Number(document.getElementById('experience_years').value || 0),
            work_type: document.getElementById('work_type').value,
            has_shop: asBool('has_shop'),
            can_install: asBool('can_install'),
            id_file_front_url: document.getElementById('id_file_front_url').value,
            id_file_back_url: document.getElementById('id_file_back_url').value,
            profile_photo_url: document.getElementById('profile_photo_url').value || null,
            picked_latitude: pickedLatLng ? pickedLatLng.lat : null,
            picked_longitude: pickedLatLng ? pickedLatLng.lng : null
          }};
          const res = await fetch('/api/mini-app/register', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
          }});
          setResult(await res.json());
        }}
        async function loadDashboard() {{
          const uid = document.getElementById('dashboard_user_id').value.trim();
          if (!uid) return setResult({{ok:false,error:'dashboard_user_id is required'}});
          const res = await fetch(`/api/agent/dashboard/${{encodeURIComponent(uid)}}`);
          const data = await res.json();
          if (data.dashboard) {{
            document.getElementById('upd_full_name').value = data.dashboard.full_name || '';
            document.getElementById('upd_phone').value = data.dashboard.phone || '';
            document.getElementById('upd_region').value = data.dashboard.region || '';
            document.getElementById('upd_territory').value = data.dashboard.preferred_territory || '';
            if (data.dashboard.application_id) {{
              document.getElementById('training_application_id').value = data.dashboard.application_id;
              document.getElementById('perf_application_id').value = data.dashboard.application_id;
            }}
          }}
          setResult(data);
        }}
        async function updateProfile() {{
          const uid = document.getElementById('dashboard_user_id').value.trim();
          if (!uid) return setResult({{ok:false,error:'dashboard_user_id is required'}});
          const payload = {{
            full_name: document.getElementById('upd_full_name').value,
            phone: document.getElementById('upd_phone').value,
            region: document.getElementById('upd_region').value,
            preferred_territory: document.getElementById('upd_territory').value
          }};
          const res = await fetch(`/api/agent/dashboard/${{encodeURIComponent(uid)}}/profile`, {{
            method: 'PATCH',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
          }});
          setResult(await res.json());
        }}
        async function markTraining() {{
          const appId = document.getElementById('training_application_id').value.trim();
          const moduleKey = document.getElementById('training_module_key').value.trim();
          if (!appId || !moduleKey) return setResult({{ok:false,error:'application_id and module_key are required'}});
          const res = await fetch(`/api/agent/training/${{encodeURIComponent(appId)}}`, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{module_key: moduleKey, completed: asBool('training_completed')}})
          }});
          setResult(await res.json());
        }}
        async function submitPerformance() {{
          const rawMetadata = document.getElementById('perf_metadata').value || '{{}}';
          let metadata = {{}};
          try {{
            metadata = JSON.parse(rawMetadata);
          }} catch (err) {{
            return setResult({{ok:false,error:'Metadata must be valid JSON'}});
          }}
          const payload = {{
            application_id: document.getElementById('perf_application_id').value,
            event_type: document.getElementById('perf_event_type').value,
            event_value: Number(document.getElementById('perf_event_value').value || 0),
            occurred_at: document.getElementById('perf_occurred_at').value || null,
            metadata
          }};
          const res = await fetch('/api/performance/events', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
          }});
          setResult(await res.json());
        }}
        function drawRankTable(title, items, valueKey) {{
          if (!items?.length) return `<h4>${{title}}</h4><p class="muted">No data yet.</p>`;
          const rows = items.map((i, idx) =>
            `<tr><td>#${{idx + 1}}</td><td>${{i.full_name || 'N/A'}}</td><td>${{i.phone || 'N/A'}}</td><td><span class="pill ok">${{i[valueKey] ?? 0}}</span></td></tr>`
          ).join('');
          return `<h4>${{title}}</h4><table><thead><tr><th>Rank</th><th>Name</th><th>Phone</th><th>Total</th></tr></thead><tbody>${{rows}}</tbody></table>`;
        }}
        async function loadRankings() {{
          const res = await fetch('/api/rankings');
          const data = await res.json();
          document.getElementById('rankingsContainer').innerHTML =
            drawRankTable('Top Sales Agents', data.rankings?.top_sales_agents || [], 'total_sales') +
            drawRankTable('Top Installer Agents', data.rankings?.top_installer_agents || [], 'completed_jobs');
          setResult(data);
        }}
        loadTerritories();
        loadRankings();
      </script>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.route("/api/mini-app/register", methods=["POST"])
def mini_app_register() -> dict:
    payload = request.get_json(silent=True) or {}
    required = [
        "telegram_user_id", "full_name", "phone", "region", "zone", "woreda", "kebele", "village", "preferred_territory",
    ]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return {"ok": False, "error": f"Missing fields: {', '.join(missing)}"}, 400
    if not territory_is_available(
        payload["preferred_territory"],
        region=payload.get("region"),
        zone=payload.get("zone"),
        woreda=payload.get("woreda"),
        kebele=payload.get("kebele"),
    ):
        return {"ok": False, "error": "Territory is not available"}, 409

    score = score_application(payload)
    record = {
        "telegram_user_id": str(payload["telegram_user_id"]),
        "full_name": payload["full_name"],
        "phone": payload["phone"],
        "applicant_type": payload.get("applicant_type", "sales_installer"),
        "region": payload["region"],
        "zone": payload["zone"],
        "woreda": payload["woreda"],
        "kebele": payload["kebele"],
        "village": payload["village"],
        "experience": bool(payload.get("experience", False)),
        "experience_years": int(payload.get("experience_years") or 0),
        "work_type": payload.get("work_type", "N/A"),
        "has_shop": bool(payload.get("has_shop", False)),
        "can_install": bool(payload.get("can_install", False)),
        "preferred_territory": payload["preferred_territory"],
        "id_file_front_url": payload.get("id_file_front_url") or "mini-app-placeholder",
        "id_file_back_url": payload.get("id_file_back_url") or "mini-app-placeholder",
        "profile_photo_url": payload.get("profile_photo_url"),
        "qualification_score": score.qualification_score,
        "qualification_flag": score.qualification_flag,
        "agent_tag": default_agent_tag(payload.get("applicant_type", "sales_installer")),
        "performance_potential": "Medium",
        "internal_remarks": payload.get("internal_remarks"),
        "status": "Submitted",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    saved = save_application(record)
    send_notification_email(record)
    send_admin_telegram_alert(record)
    return {"ok": True, "application": saved}


@app.route("/api/territories/map", methods=["GET"])
def territories_map() -> dict:
    items = list_territories_for_map(
        region=request.args.get("region"),
        zone=request.args.get("zone"),
        woreda=request.args.get("woreda"),
    )
    return {"ok": True, "items": items}


@app.route("/api/territories/nearest", methods=["POST"])
def nearest_territories() -> dict:
    payload = request.get_json(silent=True) or {}
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if latitude is None or longitude is None:
        return {"ok": False, "error": "latitude and longitude are required"}, 400
    items = suggest_nearest_territories(float(latitude), float(longitude), settings.territory_suggestion_limit)
    return {"ok": True, "items": items}


@app.route("/api/agent/dashboard/<telegram_user_id>", methods=["GET"])
def agent_dashboard_api(telegram_user_id: str) -> dict:
    dashboard = get_agent_dashboard(telegram_user_id)
    if not dashboard:
        return {"ok": False, "error": "Agent not found"}, 404
    dashboard["training_links"] = {
        "pdf": settings.training_pdf_url,
        "video": settings.training_video_url,
        "sales_playbook": settings.sales_playbook_url,
    }
    return {"ok": True, "dashboard": dashboard}


@app.route("/api/agent/dashboard/<telegram_user_id>/profile", methods=["PATCH"])
def agent_profile_update_api(telegram_user_id: str) -> dict:
    payload = request.get_json(silent=True) or {}
    updated = update_agent_profile(telegram_user_id, payload)
    return {"ok": True, "application": updated}


@app.route("/api/agent/training/<application_id>", methods=["POST"])
def agent_training_progress_api(application_id: str) -> dict:
    payload = request.get_json(silent=True) or {}
    module_key = str(payload.get("module_key") or "").strip()
    if not module_key:
        return {"ok": False, "error": "module_key is required"}, 400
    completed = bool(payload.get("completed", False))
    result = upsert_training_progress(application_id, module_key, completed)
    return {"ok": True, "training_progress": result}


@app.route("/api/performance/events", methods=["POST"])
def performance_event_api() -> dict:
    _require_admin()
    payload = request.get_json(silent=True) or {}
    application_id = str(payload.get("application_id") or "").strip()
    event_type = str(payload.get("event_type") or "").strip()
    if not application_id or event_type not in VALID_PERFORMANCE_EVENT_TYPES:
        return {"ok": False, "error": "Valid application_id and event_type are required"}, 400
    event = create_performance_event(
        application_id=application_id,
        event_type=event_type,
        event_value=float(payload.get("event_value") or 0),
        metadata=payload.get("metadata"),
        occurred_at=payload.get("occurred_at"),
    )
    return {"ok": True, "event": event}


@app.route("/api/rankings", methods=["GET"])
def rankings_api() -> dict:
    return {"ok": True, "rankings": get_rankings()}
