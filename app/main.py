from __future__ import annotations

import logging
import mimetypes
import re
import secrets
import asyncio
import os
import hashlib
import hmac
import json
import threading
from collections import Counter
from multiprocessing import current_process
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from html import escape

import httpx
import csv
from io import StringIO
from io import BytesIO
from flask import Flask, Response, abort, redirect, request

from app.config import settings
from app.i18n import load_translations
from app.scoring import score_application
from app.web_module import WebModule
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
    get_bot_session,
    upsert_bot_session,
    delete_bot_session,
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
app.url_map.strict_slashes = False
application = app
logger = logging.getLogger(__name__)
app.secret_key = settings.flask_secret_key or settings.admin_dashboard_token or "change-me-in-production"
BOOT_TIMESTAMP = datetime.now(timezone.utc).isoformat()
PROCESS_PID = os.getpid()
WORKER_IDENTIFIER = f"{os.getenv('HOSTNAME', 'local')}:{PROCESS_PID}:{current_process().name}"
SESSION_CACHE: dict[int, dict] = {}
SESSION_CACHE_LOCK = threading.Lock()
_TELEGRAM_BOT = None
_TELEGRAM_BOT_LOCK = threading.Lock()


def create_telegram_bot():
    from aiogram import Bot

    global _TELEGRAM_BOT
    if _TELEGRAM_BOT is not None:
        return _TELEGRAM_BOT
    with _TELEGRAM_BOT_LOCK:
        if _TELEGRAM_BOT is None:
            _TELEGRAM_BOT = Bot(token=settings.telegram_bot_token)
    return _TELEGRAM_BOT


async def close_telegram_bot() -> None:
    global _TELEGRAM_BOT
    bot = _TELEGRAM_BOT
    if bot is None:
        return
    with _TELEGRAM_BOT_LOCK:
        bot = _TELEGRAM_BOT
        _TELEGRAM_BOT = None
    if bot is None:
        return
    try:
        await bot.session.close()
    except Exception:
        logger.debug("telegram_bot_close_failed", exc_info=True)


def session_fingerprint(session: dict) -> str:
    raw = f"{session.get('step_index', 'na')}:{session.get('answers', {}).get('applicant_type', 'na')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def log_registration_step(user_id: int, session: dict, reason: str) -> None:
    logger.info(
        "registration-step worker=%s pid=%s user_id=%s reason=%s step_index=%s fingerprint=%s",
        WORKER_IDENTIFIER,
        PROCESS_PID,
        user_id,
        reason,
        session.get("step_index"),
        session_fingerprint(session),
    )


def get_session(user_id: int | None) -> dict:
    if user_id is None:
        return {}
    with SESSION_CACHE_LOCK:
        cached = SESSION_CACHE.get(user_id)
    if cached is not None:
        return dict(cached)

    session = get_bot_session(str(user_id)) or {}
    with SESSION_CACHE_LOCK:
        if len(SESSION_CACHE) > 5000:
            SESSION_CACHE.clear()
        SESSION_CACHE[user_id] = dict(session)
    return session


def set_session(user_id: int, data: dict) -> None:
    upsert_bot_session(str(user_id), data)
    with SESSION_CACHE_LOCK:
        SESSION_CACHE[user_id] = dict(data)


def drop_registration_session(user_id: int) -> None:
    delete_bot_session(str(user_id))
    with SESSION_CACHE_LOCK:
        SESSION_CACHE.pop(user_id, None)


logger.info(
    "app-startup worker=%s pid=%s boot_timestamp=%s",
    WORKER_IDENTIFIER,
    PROCESS_PID,
    BOOT_TIMESTAMP,
)

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
    lang = get_session(user_id).get("language", "en")
    return I18N.get(lang, I18N["en"]).get(key, I18N["en"].get(key, key))


def trf(user_id: int, key: str, **kwargs) -> str:
    return tr(user_id, key).format(**kwargs)


def language_selection_pending(user_id: int) -> bool:
    return get_session(user_id).get("awaiting_language", False)


def fallback_match_reason(user_id: int, text: str | None) -> str:
    if not text:
        return "unknown_input"

    if text in LANGUAGE_LABELS:
        return "known_language_label"

    registration_buttons = {
        tr(user_id, "btn_register_sales"),
        tr(user_id, "btn_register_installer"),
        tr(user_id, "btn_register_both"),
    }
    if text in registration_buttons:
        return "known_registration_button"

    known_commands_and_buttons = {
        "/start",
        "/language",
        tr(user_id, "btn_change_language"),
        "/help",
        "/contact",
        tr(user_id, "btn_contact_support"),
        tr(user_id, "btn_email_support"),
        tr(user_id, "btn_whatsapp_support"),
        tr(user_id, "btn_call_support"),
        "/send",
        "/status",
        tr(user_id, "btn_check_status"),
        "/admin",
        "/adminmenu",
        tr(user_id, "btn_back_main_menu"),
        tr(user_id, "btn_admin_dashboard_link"),
        tr(user_id, "btn_view_recent_applications"),
        tr(user_id, "btn_filter_applications"),
        tr(user_id, "btn_update_application_status"),
        tr(user_id, "btn_add_admin_user"),
        "/register",
    }
    if text in known_commands_and_buttons or text.startswith(("/addadmin", "/status ", "/territory ")):
        return "known_command"

    return "unknown_input"


def registration_in_progress(user_id: int) -> bool:
    session = get_session(user_id)
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

admin_sessions: dict[int, dict] = {}

VALID_PERFORMANCE_LEVELS = {"High", "Medium", "Low"}
COMMAND_WHILE_REGISTRATION_ACTIVE_COUNTER: Counter[str] = Counter()


def log_non_registration_route(user_id: int, text: str | None, route: str, in_reg: bool) -> None:
    logger.info("telegram.route route=%s in_reg=%s text=%r user_id=%s", route, in_reg, text, user_id)
    if not in_reg:
        return

    COMMAND_WHILE_REGISTRATION_ACTIVE_COUNTER[route] += 1
    logger.info(
        "metric.command_while_registration_active += 1 route=%s count=%s",
        route,
        COMMAND_WHILE_REGISTRATION_ACTIVE_COUNTER[route],
    )
    top_route, top_count = COMMAND_WHILE_REGISTRATION_ACTIVE_COUNTER.most_common(1)[0]
    logger.info(
        "metric.command_while_registration_active.top_route route=%s count=%s",
        top_route,
        top_count,
    )


def localized_values(key: str) -> set[str]:
    return {I18N.get(lang, {}).get(key, key) for lang in SUPPORTED_LANGUAGES}


def start_keyboard_for_user(user_id: int) -> list[list[str | dict[str, str]]]:
    keyboard = [
        [{"text": tr(user_id, "btn_open_mini_app"), "web_app": "https://agent.interethiopia.com/mini-app"}],
        [tr(user_id, "btn_register_sales")],
        [tr(user_id, "btn_register_installer")],
        [tr(user_id, "btn_register_both")],
        [tr(user_id, "btn_check_status")],
        [tr(user_id, "btn_change_language")],
    ]
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


async def send_message(chat_id: int, text: str, keyboard: list[list[str | dict[str, str]]] | None = None) -> None:
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

    reply_markup = None
    if keyboard:
        def make_button(item: str | dict[str, str]) -> KeyboardButton:
            if isinstance(item, str):
                return KeyboardButton(text=item)
            if "web_app" in item:
                return KeyboardButton(text=item["text"], web_app=WebAppInfo(url=item["web_app"]))
            return KeyboardButton(text=item["text"])

        reply_markup = ReplyKeyboardMarkup(
            keyboard=[[make_button(label) for label in row] for row in keyboard],
            resize_keyboard=True,
            one_time_keyboard=False,
        )
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
    lang = get_session(user_id).get("language", "en")
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
    session = get_session(user_id)
    if not session:
        await send_message(chat_id, tr(user_id, "start_prompt"))
        return
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
    session = get_session(user_id)
    if not session:
        await send_message(chat_id, tr(user_id, "start_prompt"))
        return
    answers = session["answers"]

    territory_valid = territory_is_available(
        answers["preferred_territory"],
        region=answers.get("region"),
        zone=answers.get("zone"),
        woreda=answers.get("woreda"),
    )
    if not territory_valid:
        session["step_index"] = next(i for i, (k, _) in enumerate(QUESTION_FLOW) if k == "preferred_territory")
        set_session(user_id, session)
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
    drop_registration_session(user_id)


async def process_registration_input(chat_id: int, user_id: int, text: str | None, message_obj) -> None:
    session = get_session(user_id)
    if not session:
        await send_message(chat_id, tr(user_id, "start_prompt"))
        return
    field, _ = QUESTION_FLOW[session["step_index"]]

    if field in {"id_front", "id_back", "profile_photo"}:
        if field == "profile_photo" and text and text.strip().lower() in {"skip", "ዝለል"}:
            session["answers"]["profile_photo_url"] = None
            session["step_index"] += 1
            set_session(user_id, session)
            log_registration_step(user_id, session, reason="profile_photo_skipped")
            await ask_next(chat_id, user_id)
            return

        doc = message_obj.document
        photos = message_obj.photo or []
        if not doc and not photos:
            await send_message(chat_id, tr(user_id, "file_required"))
            return

        file_id = doc.file_id if doc else photos[-1].file_id
        file_ext = "jpg"
        if doc and doc.file_name and "." in doc.file_name:
            file_ext = doc.file_name.split(".")[-1]

        tg_file = await create_telegram_bot().get_file(file_id)
        file_size = int(getattr(tg_file, "file_size", 0) or 0)
        max_size = settings.max_upload_size_mb * 1024 * 1024
        if file_size > max_size:
            await send_message(chat_id, trf(user_id, "file_too_large", size=settings.max_upload_size_mb))
            return
        buffer = BytesIO()
        await create_telegram_bot().download(tg_file, destination=buffer)
        file_bytes = buffer.getvalue()

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
        set_session(user_id, session)
        log_registration_step(user_id, session, reason=f"{field}_captured")
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
            drop_registration_session(user_id)
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
    set_session(user_id, session)
    log_registration_step(user_id, session, reason=f"{field}_captured")
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
    lang = get_session(user_id).get("language", "en")

    session = {
        "registration_active": True,
        "step_index": 0,
        "answers": {"applicant_type": applicant_type},
        "language": lang,
        "awaiting_language": False,
    }
    set_session(user_id, session)
    log_registration_step(user_id, session, reason="registration_started")
    await send_message(chat_id, tr(user_id, "registration_started"))
    await ask_next(chat_id, user_id)


@app.route("/health", methods=["GET"])
def health() -> dict:
    return {"status": "ok"}


@app.route("/", methods=["GET"])
def root() -> Response:
    return redirect("/health", code=302)


async def _telegram_webhook(update) -> dict:
    trace_id = secrets.token_hex(4)
    user_id: int | None = None
    chat_id: int | None = None

    def _log_route(route: str) -> dict:
        logger.info(
            "telegram_webhook_route %s",
            json.dumps({"trace_id": trace_id, "user_id": user_id, "chat_id": chat_id, "route": route}, ensure_ascii=False, separators=(",", ":")),
        )
        return {"ok": True}

    def _payload_shape(value: object) -> object:
        if isinstance(value, dict):
            return {key: _payload_shape(nested) for key, nested in value.items()}
        if isinstance(value, list):
            if not value:
                return []
            return [_payload_shape(value[0])]
        return type(value).__name__

    try:
        message_obj = update.message or update.edited_message
        if not message_obj:
            return _log_route("no_effective_message")

        message = message_obj.model_dump(mode="python")
        if not message:
            return _log_route("empty_message")

        chat_id = message_obj.chat.id if message_obj.chat else None
        user_id = message_obj.from_user.id if message_obj.from_user else None
        text = message_obj.text
        message_type = next(
            (key for key in ("photo", "document", "voice", "video", "sticker", "location", "contact", "animation") if message.get(key)),
            "unknown",
        )
        text_value = text if text is not None else f"<{message_type}>"
        session_keys = sorted(get_session(user_id).keys()) if user_id is not None else []
        logger.info(
            "telegram_webhook_entry %s",
            json.dumps(
                {
                    "trace_id": trace_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "text": text_value,
                    "session_keys": session_keys,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        in_reg = registration_in_progress(user_id)

        if text == "/start":
            log_non_registration_route(user_id, text, "/start", in_reg)
            previous_language = get_session(user_id).get("language", "en")
            drop_registration_session(user_id)
            set_session(user_id, {"language": previous_language, "awaiting_language": True, "registration_active": False})
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return _log_route("start_command")

        if text in LANGUAGE_LABELS:
            session = get_session(user_id)
            session["language"] = LANGUAGE_LABELS[text]
            session["awaiting_language"] = False
            set_session(user_id, session)
            await send_message(chat_id, tr(user_id, "welcome"), keyboard=start_keyboard_for_user(user_id))
            return _log_route("language_selected")

        if text in {"/language", tr(user_id, "btn_change_language")}:
            log_non_registration_route(user_id, text, "/language", in_reg)
            session = get_session(user_id)
            session["awaiting_language"] = True
            set_session(user_id, session)
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return _log_route("language_command")

        if language_selection_pending(user_id):
            await send_message(chat_id, tr(user_id, "choose_language"), keyboard=LANGUAGE_KEYBOARD)
            return _log_route("language_pending")

        if text in {"/help", "/contact", tr(user_id, "btn_contact_support")}:
            log_non_registration_route(user_id, text, "support", in_reg)
            await send_message(chat_id, tr(user_id, "support"), keyboard=support_keyboard(user_id))
            return _log_route("support_menu")

        if text in {tr(user_id, "btn_email_support"), tr(user_id, "btn_whatsapp_support"), tr(user_id, "btn_call_support")}:
            channel_map = {
                tr(user_id, "btn_email_support"): tr(user_id, "support_email"),
                tr(user_id, "btn_whatsapp_support"): tr(user_id, "support_whatsapp"),
                tr(user_id, "btn_call_support"): tr(user_id, "support_call"),
            }
            await send_message(chat_id, trf(user_id, "support_channel", channel=channel_map[text]))
            return _log_route("support_channel")

        if text == "/send":
            total_admins = count_admins()
            if total_admins == 0:
                add_bot_admin(str(user_id), created_by=str(user_id))
                await send_message(chat_id, tr(user_id, "first_admin_assigned"))
                return _log_route("send_first_admin")

            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_send"))
                return _log_route("send_non_admin_denied")

            await send_message(
                chat_id,
                tr(user_id, "admin_command_active"),
            )
            return _log_route("send_admin_ready")

        if text and text.startswith("/addadmin"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_assign_admins"))
                return _log_route("addadmin_non_admin_denied")

            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().isdigit():
                await send_message(chat_id, tr(user_id, "usage_addadmin"))
                return _log_route("addadmin_usage")

            target_user_id = parts[1].strip()
            created, _ = add_bot_admin(target_user_id, created_by=str(user_id))
            if created:
                await send_message(chat_id, trf(user_id, "user_now_admin", user_id=target_user_id))
            else:
                await send_message(chat_id, trf(user_id, "user_already_admin", user_id=target_user_id))
            return _log_route("addadmin_complete")

        if text and (text.startswith("/status") or text == tr(user_id, "btn_check_status")):
            log_non_registration_route(user_id, text, "status", in_reg)
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
            return _log_route("status_lookup")

        if text and text.startswith("/territory"):
            log_non_registration_route(user_id, text, "territory_removed", in_reg)
            await send_message(chat_id, "Territory availability check has moved to the Mini App Territories map.")
            return _log_route("territory_removed")

        if text in {"/admin", "/adminmenu"}:
            log_non_registration_route(user_id, text, "admin", in_reg)
            await show_admin_menu(chat_id, user_id)
            return _log_route("admin_menu")

        if text == tr(user_id, "btn_back_main_menu"):
            await send_message(chat_id, tr(user_id, "back_main_menu"), keyboard=start_keyboard_for_user(user_id))
            admin_sessions.pop(user_id, None)
            return _log_route("back_main_menu")

        if text == tr(user_id, "btn_admin_dashboard_link"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return _log_route("admin_dashboard_denied")
            dashboard_url = "/admin"
            if settings.admin_dashboard_token:
                dashboard_url = f"/admin?token={settings.admin_dashboard_token}"
            await send_message(chat_id, trf(user_id, "open_admin_dashboard", dashboard_url=dashboard_url))
            return _log_route("admin_dashboard_link")

        if text == tr(user_id, "btn_view_recent_applications"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return _log_route("view_recent_denied")
            apps = get_applications()[:5]
            if not apps:
                await send_message(chat_id, tr(user_id, "no_applications_yet"))
                return _log_route("view_recent_empty")
            await send_message(chat_id, tr(user_id, "recent_applications_preview"))
            for item in apps:
                await send_application_preview(chat_id, item, user_id)
            return _log_route("view_recent_done")

        if text == tr(user_id, "btn_filter_applications"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return _log_route("filter_applications_denied")
            admin_sessions[user_id] = {"state": "await_filter"}
            await send_message(chat_id, tr(user_id, "filter_instructions"))
            return _log_route("filter_applications_start")

        if text == tr(user_id, "btn_update_application_status"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return _log_route("update_status_denied")
            admin_sessions[user_id] = {"state": "await_application_for_update"}
            await send_message(chat_id, tr(user_id, "send_application_id_to_update"))
            return _log_route("update_status_start")

        if text == tr(user_id, "btn_add_admin_user"):
            if not is_bot_admin(str(user_id)):
                await send_message(chat_id, tr(user_id, "admin_only_features"))
                return _log_route("add_admin_user_denied")
            admin_sessions[user_id] = {"state": "await_add_admin"}
            await send_message(chat_id, tr(user_id, "send_telegram_user_id_for_admin"))
            return _log_route("add_admin_user_start")

        if text == "/register":
            await send_message(
                chat_id,
                tr(user_id, "register_choose_type"),
                keyboard=[[tr(user_id, "btn_register_sales")], [tr(user_id, "btn_register_installer")], [tr(user_id, "btn_register_both")]],
            )
            return _log_route("register_menu")

        applicant_type_by_button = {
            tr(user_id, "btn_register_sales"): "sales_only",
            tr(user_id, "btn_register_installer"): "installer_only",
            tr(user_id, "btn_register_both"): "sales_installer",
        }
        if text in applicant_type_by_button:
            await start_registration(chat_id, user_id, applicant_type_by_button[text])
            return _log_route("registration_type_selected")

        if user_id in admin_sessions:
            handled = await process_admin_input(chat_id, user_id, text)
            if handled:
                return _log_route("admin_input")

        if registration_in_progress(user_id):
            session = get_session(user_id)
            session["registration_active"] = True
            set_session(user_id, session)
            await process_registration_input(chat_id, user_id, text, message_obj)
            return _log_route("registration_input")

        session = get_session(user_id)
        awaiting_language = session.get("awaiting_language", False)
        match_reason = fallback_match_reason(user_id, text)
        matched_known_command_or_button = match_reason in {"known_command", "known_registration_button"}
        logger.info(
            "route=start_prompt_fallback registration_in_progress=%s awaiting_language=%s step_index=%s matched_known_command_or_button=%s match_reason=%s",
            registration_in_progress(user_id),
            awaiting_language,
            session.get("step_index"),
            matched_known_command_or_button,
            match_reason,
        )
        await send_message(chat_id, tr(user_id, "start_prompt"))
        return _log_route("start_prompt_fallback")
    except Exception:
        try:
            update_payload = update.model_dump(mode="python") if update else {}
        except Exception:
            update_payload = {"unserializable_update": True}
        logger.exception(
            "telegram_webhook_exception %s",
            json.dumps(
                {
                    "trace_id": trace_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "update_shape": _payload_shape(update_payload),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return _log_route("exception")
    finally:
        await close_telegram_bot()


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook() -> dict:
    from aiogram.types import Update

    payload = request.get_json(silent=True) or {}
    try:
        update = Update.model_validate(payload)
    except Exception:
        logger.exception("invalid_telegram_update_payload")
        return {"ok": True}
    return asyncio.run(_telegram_webhook(update))

web_module = WebModule(onboarding_callback=send_post_approval_onboarding)
app.register_blueprint(web_module.blueprint)
