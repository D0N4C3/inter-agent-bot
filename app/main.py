from __future__ import annotations

import logging
import mimetypes
import re
import secrets
import asyncio
import hashlib
import hmac
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from html import escape

import httpx
import csv
from io import StringIO
from telegram import Bot, ReplyKeyboardMarkup, Update

from flask import Flask, Response, abort, redirect, request

from app.config import settings
from app.i18n import load_translations
from app.scoring import score_application
from app.services import (
    add_bot_admin,
    count_admins,
    default_agent_tag,
    get_application,
    get_agent_dashboard,
    get_latest_status_by_phone,
    get_latest_status_by_telegram_user,
    get_applications,
    get_rankings,
    get_training_links,
    is_bot_admin,
    list_territories_for_map,
    save_application,
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
    list_woreda_regions,
)

app = Flask(__name__)
application = app
logger = logging.getLogger(__name__)
app.secret_key = settings.flask_secret_key or settings.admin_dashboard_token or "change-me-in-production"


def create_telegram_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)

SUPPORTED_LANGUAGES = {"en", "am", "om", "ti"}
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
LANGUAGE_KEYBOARD = [["English", "አማርኛ"], ["Afaan Oromo", "ትግርኛ"]]
LANGUAGE_LABELS = {
    "English": "en",
    "አማርኛ": "am",
    "Afaan Oromo": "om",
    "ትግርኛ": "ti",
}

I18N = load_translations()


def tr(user_id: int, key: str) -> str:
    lang = sessions.get(user_id, {}).get("language", "en")
    return I18N.get(lang, I18N["en"]).get(key, I18N["en"].get(key, key))


def trf(user_id: int, key: str, **kwargs) -> str:
    return tr(user_id, key).format(**kwargs)


def language_selection_pending(user_id: int) -> bool:
    return sessions.get(user_id, {}).get("awaiting_language", False)


def registration_in_progress(user_id: int) -> bool:
    session = sessions.get(user_id, {})
    if session.get("registration_active"):
        return True

    step_index = session.get("step_index")
    answers = session.get("answers")
    return isinstance(step_index, int) and 0 <= step_index < len(QUESTION_FLOW) and isinstance(answers, dict)

QUESTION_FLOW = [
    ("full_name", "prompt_full_name"),
    ("phone", "prompt_phone"),
    ("region", "prompt_region"),
    ("zone", "prompt_zone"),
    ("woreda", "prompt_woreda"),
    ("experience", "prompt_experience"),
    ("experience_years", "prompt_experience_years"),
    ("work_type", "prompt_work_type"),
    ("has_shop", "prompt_has_shop"),
    ("can_install", "prompt_can_install"),
    ("id_front", "prompt_id_front"),
    ("id_back", "prompt_id_back"),
    ("profile_photo", "prompt_profile_photo"),
    ("preferred_territory", "prompt_preferred_territory"),
    ("terms", "prompt_terms"),
]

sessions: dict[int, dict] = {}
admin_sessions: dict[int, dict] = {}

VALID_PERFORMANCE_LEVELS = {"High", "Medium", "Low"}


def localized_values(key: str) -> set[str]:
    return {I18N.get(lang, {}).get(key, key) for lang in SUPPORTED_LANGUAGES}


def start_keyboard_for_user(user_id: int) -> list[list[str]]:
    keyboard = [
        [tr(user_id, "btn_register_sales")],
        [tr(user_id, "btn_register_installer")],
        [tr(user_id, "btn_register_both")],
        [tr(user_id, "btn_check_territory")],
        [tr(user_id, "btn_check_status")],
        [tr(user_id, "btn_contact_support")],
        [tr(user_id, "btn_change_language")],
    ]
    if is_bot_admin(str(user_id)):
        keyboard.insert(0, [tr(user_id, "btn_admin_management")])
    return keyboard


def support_keyboard(user_id: int) -> list[list[str]]:
    return [
        [tr(user_id, "btn_email_support"), tr(user_id, "btn_whatsapp_support")],
        [tr(user_id, "btn_call_support")],
    ]


def admin_menu_text(user_id: int) -> str:
    return tr(user_id, "admin_menu_text")


def admin_menu_keyboard(user_id: int) -> list[list[str]]:
    return [
        [tr(user_id, "btn_view_recent_applications"), tr(user_id, "btn_filter_applications")],
        [tr(user_id, "btn_update_application_status"), tr(user_id, "btn_add_admin_user")],
        [tr(user_id, "btn_admin_dashboard_link"), tr(user_id, "btn_back_main_menu")],
    ]


async def show_admin_menu(chat_id: int, user_id: int) -> None:
    if not is_bot_admin(str(user_id)):
        await send_message(chat_id, tr(user_id, "admin_only_management"))
        return
    await send_message(chat_id, admin_menu_text(user_id), keyboard=admin_menu_keyboard(user_id))


def _format_application_summary(app_row: dict, user_id: int) -> str:
    return (
        f"{tr(user_id, 'field_id')}: {app_row['application_id']}\n"
        f"{tr(user_id, 'field_name')}: {app_row['full_name']}\n"
        f"{tr(user_id, 'field_type')}: {app_row['applicant_type']}\n"
        f"{tr(user_id, 'field_status')}: {app_row['status']}\n"
        f"{tr(user_id, 'field_region')}: {app_row['region']}\n"
        f"{tr(user_id, 'field_territory')}: {app_row['preferred_territory']}\n"
        f"{tr(user_id, 'field_score')}: {app_row.get('qualification_score', 'N/A')} ({app_row.get('qualification_flag', 'N/A')})"
    )


def _is_image_url(url: str | None) -> bool:
    if not url:
        return False
    return url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))


async def send_application_preview(chat_id: int, app_row: dict, user_id: int) -> None:
    summary = (
        f"🧾 {tr(user_id, 'application_snapshot')}\n"
        f"{tr(user_id, 'field_id')}: {app_row['application_id']}\n"
        f"👤 {tr(user_id, 'field_name')}: {app_row['full_name']}\n"
        f"🧭 {tr(user_id, 'field_type')}: {app_row['applicant_type']}\n"
        f"🔖 {tr(user_id, 'field_status')}: {app_row['status']}\n"
        f"📍 {tr(user_id, 'field_region')}: {app_row['region']}\n"
        f"🗺️ {tr(user_id, 'field_territory')}: {app_row['preferred_territory']}\n"
        f"📊 {tr(user_id, 'field_score')}: {app_row.get('qualification_score', 'N/A')} ({app_row.get('qualification_flag', 'N/A')})"
    )
    await send_message(chat_id, summary)

    uploads = [
        (tr(user_id, "upload_id_front"), app_row.get("id_file_front_url")),
        (tr(user_id, "upload_id_back"), app_row.get("id_file_back_url")),
        (tr(user_id, "upload_profile"), app_row.get("profile_photo_url")),
    ]
    for label, url in uploads:
        if not url:
            continue
        if _is_image_url(url):
            await send_photo(chat_id, url, caption=label)
        else:
            await send_message(chat_id, f"{label}: {url}")


async def send_message(chat_id: int, text: str, keyboard: list[list[str]] | None = None) -> None:
    reply_markup = None
    if keyboard:
        reply_markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)
    await create_telegram_bot().send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def send_photo(chat_id: int, photo_url: str, caption: str | None = None) -> None:
    await create_telegram_bot().send_photo(chat_id=chat_id, photo=photo_url, caption=caption)


def send_post_approval_onboarding(application: dict) -> None:
    chat_id = application.get("telegram_user_id")
    if not chat_id:
        return
    training_links = get_training_links()
    text = (
        "🎉 Welcome to Inter Ethiopia Solutions!\n\n"
        "Your application has been approved.\n\n"
        "Training materials:\n"
        f"- Solar installation guide (PDF): {training_links['pdf']}\n"
        f"- Solar installation training video: {training_links['video']}\n"
        f"- Sales playbook: {training_links['sales_playbook']}\n\n"
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


def _location_keyboard(field: str, answers: dict) -> list[list[str]] | None:
    rows = list_woreda_regions(region=answers.get("region"), zone=answers.get("zone"))
    if field == "region":
        values = sorted({row.get("region") for row in rows if row.get("region")})
    elif field == "zone":
        values = sorted({row.get("zone") for row in rows if row.get("zone")})
    elif field == "woreda":
        values = sorted({row.get("woreda") for row in rows if row.get("woreda")})
    else:
        values = []
    if not values:
        return None
    return [[value] for value in values[:60]]


def normalize_phone(phone: str) -> str:
    value = re.sub(r"[\s\-()]+", "", phone.strip())
    if value.startswith("0"):
        return f"+251{value[1:]}"
    return value


def phone_is_valid(phone: str) -> bool:
    normalized = normalize_phone(phone)
    return bool(re.fullmatch(r"\+251[79]\d{8}", normalized))


async def ask_next(chat_id: int, user_id: int) -> None:
    session = sessions[user_id]
    index = session["step_index"]
    if index >= len(QUESTION_FLOW):
        await finalize_application(chat_id, user_id)
        return
    field, prompt_key = QUESTION_FLOW[index]
    prompt = f"{settings.terms_text}\n\n{tr(user_id, 'prompt_terms_reply')}" if field == "terms" else tr(user_id, prompt_key)
    if field in {"experience", "has_shop", "can_install"}:
        await send_message(chat_id, prompt, keyboard=yes_no_keyboard(user_id))
        return

    if field == "region":
        keyboard = _location_keyboard("region", session["answers"]) or [[region] for region in ETHIOPIA_REGIONS]
        await send_message(chat_id, prompt, keyboard=keyboard)
        return

    if field in {"zone", "woreda"}:
        keyboard = _location_keyboard(field, session["answers"])
        if keyboard:
            await send_message(chat_id, prompt, keyboard=keyboard)
            return

    if field in {"zone", "woreda"}:
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
    )
    if not territory_valid:
        session["step_index"] = next(i for i, (k, _) in enumerate(QUESTION_FLOW) if k == "preferred_territory")
        await send_message(chat_id, tr(user_id, "territory_unavailable"))
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
        "kebele": "N/A",
        "village": answers["preferred_territory"],
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
    await send_message(chat_id, f"{tr(user_id, 'submitted')}\n{tr(user_id, 'timeline')}")
    sessions.pop(user_id, None)


async def process_registration_input(chat_id: int, user_id: int, text: str | None, message: dict) -> None:
    session = sessions[user_id]
    field, _ = QUESTION_FLOW[session["step_index"]]

    if field in {"id_front", "id_back", "profile_photo"}:
        if field == "profile_photo" and text and text.strip().lower() in {"skip", "ዝለል"}:
            session["answers"]["profile_photo_url"] = None
            session["step_index"] += 1
            await ask_next(chat_id, user_id)
            return

        doc = message.get("document")
        photos = message.get("photo", [])
        if not doc and not photos:
            await send_message(chat_id, tr(user_id, "file_required"))
            return

        file_id = doc["file_id"] if doc else photos[-1]["file_id"]
        file_ext = "jpg"
        if doc and doc.get("file_name") and "." in doc["file_name"]:
            file_ext = doc["file_name"].split(".")[-1]

        tg_file = await create_telegram_bot().get_file(file_id)
        file_size = int(getattr(tg_file, "file_size", 0) or 0)
        max_size = settings.max_upload_size_mb * 1024 * 1024
        if file_size > max_size:
            await send_message(chat_id, trf(user_id, "file_too_large", size=settings.max_upload_size_mb))
            return
        file_bytes = bytes(await tg_file.download_as_bytearray())

        guessed_type = mimetypes.guess_type(f"file.{file_ext}")[0]
        content_type = guessed_type or "application/octet-stream"
        allowed_types = {"image/jpeg", "image/jpg", "image/png", "application/pdf"}
        if content_type not in allowed_types:
            await send_message(chat_id, tr(user_id, "file_unsupported"))
            return

        if field == "id_front":
            filename = f"front-id-{secrets.token_hex(6)}.{file_ext}"
        elif field == "id_back":
            filename = f"back-id-{secrets.token_hex(6)}.{file_ext}"
        else:
            filename = f"profile-photo-{secrets.token_hex(6)}.{file_ext}"

        uploaded_url = upload_telegram_file(
            file_bytes,
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
        await send_message(chat_id, tr(user_id, "text_required"))
        return

    value = text.strip()

    if field == "phone":
        if not phone_is_valid(value):
            await send_message(chat_id, tr(user_id, "phone_invalid"))
            return
        value = normalize_phone(value)

    if field in {"experience", "has_shop", "can_install"}:
        if value.lower() not in {"yes", "no", "y", "n", "አዎ", "አይደለም"}:
            await send_message(chat_id, tr(user_id, "yes_no_required"))
            return
        session["answers"][field] = parse_yes_no(value)
    elif field == "experience_years":
        if not value.isdigit():
            await send_message(chat_id, tr(user_id, "number_required"))
            return
        session["answers"][field] = int(value)
    elif field == "terms":
        if value.lower() in {"cancel", "ሰርዝ"}:
            sessions.pop(user_id, None)
            await send_message(chat_id, tr(user_id, "application_cancelled"))
            return
        if value.lower() not in {"i agree", "እስማማለሁ"}:
            await send_message(chat_id, tr(user_id, "terms_required"))
            return
        session["answers"]["terms_accepted"] = True
    else:
        if not value:
            await send_message(chat_id, tr(user_id, "field_required"))
            return
        session["answers"][field] = value

    session["step_index"] += 1
    await ask_next(chat_id, user_id)


async def process_admin_input(chat_id: int, user_id: int, text: str | None) -> bool:
    session = admin_sessions.get(user_id)
    if not session:
        return False
    if text is None:
        await send_message(chat_id, tr(user_id, "admin_text_input_required"))
        return True

    state = session.get("state")
    value = text.strip()

    if state == "await_filter":
        if value.lower() in {"cancel", "ሰርዝ"}:
            admin_sessions.pop(user_id, None)
            await show_admin_menu(chat_id, user_id)
            return True

        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 3:
            await send_message(chat_id, tr(user_id, "admin_filter_format"))
            return True

        region, applicant_type, status = [part or None for part in parts]
        apps = get_applications(region=region, applicant_type=applicant_type, status=status)
        if not apps:
            await send_message(chat_id, tr(user_id, "no_applications_match_filter"))
        else:
            await send_message(chat_id, trf(user_id, "top_matches_found", count=len(apps)))
            for item in apps[:5]:
                await send_application_preview(chat_id, item, user_id)
        admin_sessions.pop(user_id, None)
        await show_admin_menu(chat_id, user_id)
        return True

    if state == "await_add_admin":
        if not value.isdigit():
            await send_message(chat_id, tr(user_id, "enter_numeric_telegram_id"))
            return True
        created, _ = add_bot_admin(value, created_by=str(user_id))
        if created:
            await send_message(chat_id, trf(user_id, "user_now_admin", user_id=value))
        else:
            await send_message(chat_id, trf(user_id, "user_already_admin", user_id=value))
        admin_sessions.pop(user_id, None)
        await show_admin_menu(chat_id, user_id)
        return True

    if state == "await_application_for_update":
        app_row = get_application(value)
        if not app_row:
            await send_message(chat_id, tr(user_id, "application_id_not_found"))
            return True
        session["application_id"] = value
        session["state"] = "await_status_update"
        await send_message(
            chat_id,
            tr(user_id, "reply_status_update_format"),
        )
        return True

    if state == "await_status_update":
        if value.lower() in {"cancel", "ሰርዝ"}:
            admin_sessions.pop(user_id, None)
            await show_admin_menu(chat_id, user_id)
            return True
        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 6:
            await send_message(
                chat_id,
                tr(user_id, "status_update_format_hint"),
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
            await send_message(chat_id, trf(user_id, "failed_to_update", error=str(exc)))
            return True
        if old_application.get("status") != "Approved" and updated.get("status") == "Approved":
            send_post_approval_onboarding(updated)

        await send_message(chat_id, f"{tr(user_id, 'application_updated')}\n\n{_format_application_summary(updated, user_id)}")
        admin_sessions.pop(user_id, None)
        await show_admin_menu(chat_id, user_id)
        return True

    return False


async def start_registration(chat_id: int, user_id: int, applicant_type: str) -> None:
    lang = sessions.get(user_id, {}).get("language", "en")

    sessions[user_id] = {
        "registration_active": True,
        "step_index": 0,
        "answers": {"applicant_type": applicant_type},
        "language": lang,
        "awaiting_language": False,
    }
    await send_message(chat_id, tr(user_id, "registration_started"))
    await ask_next(chat_id, user_id)


@app.route("/health", methods=["GET"])
def health() -> dict:
    return {"status": "ok"}


async def _telegram_webhook(update: Update) -> dict:
    try:
        message_obj = update.effective_message
        if not message_obj:
            return {"ok": True}

        message = message_obj.to_dict()
        if not message:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message_obj.text

        if text == "/start":
            sessions.setdefault(user_id, {})
            sessions[user_id]["language"] = sessions[user_id].get("language", "en")
            sessions[user_id]["awaiting_language"] = True
            sessions[user_id]["registration_active"] = False
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return {"ok": True}

        if text in LANGUAGE_LABELS:
            sessions.setdefault(user_id, {})
            sessions[user_id]["language"] = LANGUAGE_LABELS[text]
            sessions[user_id]["awaiting_language"] = False
            await send_message(chat_id, tr(user_id, "welcome"), keyboard=start_keyboard_for_user(user_id))
            return {"ok": True}

        if text in {"/language", tr(user_id, "btn_change_language")}:
            sessions.setdefault(user_id, {})
            sessions[user_id]["awaiting_language"] = True
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return {"ok": True}

        if language_selection_pending(user_id):
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return {"ok": True}

        if text in {"/help", "/contact", tr(user_id, "btn_contact_support")}:
            await send_message(chat_id, tr(user_id, "support"), keyboard=support_keyboard(user_id))
            return {"ok": True}

        if text in {tr(user_id, "btn_email_support"), tr(user_id, "btn_whatsapp_support"), tr(user_id, "btn_call_support")}:
            channel_map = {
                tr(user_id, "btn_email_support"): tr(user_id, "support_email"),
                tr(user_id, "btn_whatsapp_support"): tr(user_id, "support_whatsapp"),
                tr(user_id, "btn_call_support"): tr(user_id, "support_call"),
            }
            await send_message(chat_id, trf(user_id, "support_channel", channel=channel_map[text]))
            return {"ok": True}

        if text == "/send":
            total_admins = count_admins()
            if total_admins == 0:
                add_bot_admin(str(user_id), created_by=str(user_id))
                await send_message(chat_id, tr(user_id, "first_admin_assigned"))
                return {"ok": True}

            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_send"))
                return {"ok": True}

            await send_message(
                chat_id,
                tr(user_id, "admin_command_active"),
            )
            return {"ok": True}

        if text and text.startswith("/addadmin"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_assign_admins"))
                return {"ok": True}

            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().isdigit():
                await send_message(chat_id, tr(user_id, "usage_addadmin"))
                return {"ok": True}

            target_user_id = parts[1].strip()
            created, _ = add_bot_admin(target_user_id, created_by=str(user_id))
            if created:
                await send_message(chat_id, trf(user_id, "user_now_admin", user_id=target_user_id))
            else:
                await send_message(chat_id, trf(user_id, "user_already_admin", user_id=target_user_id))
            return {"ok": True}

        if text and (text.startswith("/status") or text == tr(user_id, "btn_check_status")):
            parts = text.split(maxsplit=1)
            status = None
            if len(parts) == 2:
                status = get_latest_status_by_phone(parts[1].strip())
            if status is None:
                status = get_latest_status_by_telegram_user(str(user_id))

            if status:
                await send_message(chat_id, trf(user_id, "status_found", status=status))
            else:
                await send_message(chat_id, tr(user_id, "status_not_found"))
            return {"ok": True}

        if text in {"/territory", tr(user_id, "btn_check_territory")}:
            await send_message(chat_id, tr(user_id, "territory_help"))
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
                await send_message(chat_id, tr(user_id, "territory_available"))
            else:
                await send_message(chat_id, tr(user_id, "territory_unavailable"))
            return {"ok": True}

        if text in {"/admin", tr(user_id, "btn_admin_management"), "/adminmenu"}:
            await show_admin_menu(chat_id, user_id)
            return {"ok": True}

        if text == tr(user_id, "btn_back_main_menu"):
            await send_message(chat_id, tr(user_id, "back_main_menu"), keyboard=start_keyboard_for_user(user_id))
            admin_sessions.pop(user_id, None)
            return {"ok": True}

        if text == tr(user_id, "btn_admin_dashboard_link"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return {"ok": True}
            dashboard_url = "/admin"
            if settings.admin_dashboard_token:
                dashboard_url = f"/admin?token={settings.admin_dashboard_token}"
            await send_message(chat_id, trf(user_id, "open_admin_dashboard", dashboard_url=dashboard_url))
            return {"ok": True}

        if text == tr(user_id, "btn_view_recent_applications"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return {"ok": True}
            apps = get_applications()[:5]
            if not apps:
                await send_message(chat_id, tr(user_id, "no_applications_yet"))
                return {"ok": True}
            await send_message(chat_id, tr(user_id, "recent_applications_preview"))
            for item in apps:
                await send_application_preview(chat_id, item, user_id)
            return {"ok": True}

        if text == tr(user_id, "btn_filter_applications"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return {"ok": True}
            admin_sessions[user_id] = {"state": "await_filter"}
            await send_message(chat_id, tr(user_id, "filter_instructions"))
            return {"ok": True}

        if text == tr(user_id, "btn_update_application_status"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return {"ok": True}
            admin_sessions[user_id] = {"state": "await_application_for_update"}
            await send_message(chat_id, tr(user_id, "send_application_id_to_update"))
            return {"ok": True}

        if text == tr(user_id, "btn_add_admin_user"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return {"ok": True}
            admin_sessions[user_id] = {"state": "await_add_admin"}
            await send_message(chat_id, tr(user_id, "send_telegram_user_id_for_admin"))
            return {"ok": True}

        if text == "/register":
            await send_message(
                chat_id,
                tr(user_id, "register_choose_type"),
                keyboard=[[tr(user_id, "btn_register_sales")], [tr(user_id, "btn_register_installer")], [tr(user_id, "btn_register_both")]],
            )
            return {"ok": True}

        applicant_type_by_button = {
            tr(user_id, "btn_register_sales"): "sales_only",
            tr(user_id, "btn_register_installer"): "installer_only",
            tr(user_id, "btn_register_both"): "sales_installer",
        }
        if text in applicant_type_by_button:
            await start_registration(chat_id, user_id, applicant_type_by_button[text])
            return {"ok": True}

        if user_id in admin_sessions:
            handled = await process_admin_input(chat_id, user_id, text)
            if handled:
                return {"ok": True}

        if registration_in_progress(user_id):
            sessions.setdefault(user_id, {})["registration_active"] = True
            await process_registration_input(chat_id, user_id, text, message)
            return {"ok": True}

        await send_message(chat_id, tr(user_id, "start_prompt"))
    except Exception:
        logger.exception("Failed to handle telegram webhook update.")
        return {"ok": True}
    return {"ok": True}


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook() -> dict:
    payload = request.get_json(silent=True) or {}
    update = Update.de_json(payload, create_telegram_bot())
    if not update:
        return {"ok": True}
    return asyncio.run(_telegram_webhook(update))

from app.web_module import WebModule

web_module = WebModule(onboarding_callback=send_post_approval_onboarding)
app.register_blueprint(web_module.blueprint)
