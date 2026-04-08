from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from urllib.parse import unquote

from flask import abort, request, session

from app.config import settings
from app.services import get_supabase, is_bot_admin

ADMIN_SESSION_KEY = "admin_auth"


def _session_is_valid(payload: dict | None) -> bool:
    if not payload:
        return False
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    now = datetime.now(timezone.utc).timestamp()
    return expires_at > now


def is_admin_authenticated() -> bool:
    expected = settings.admin_dashboard_token
    provided = request.args.get("token") or request.headers.get("x-admin-token")
    if expected and provided == expected:
        return True
    return _session_is_valid(session.get(ADMIN_SESSION_KEY))


def login_admin(email: str, password: str) -> bool:
    try:
        client = get_supabase()
        auth_response = client.auth.sign_in_with_password({"email": email, "password": password})
        auth_session = getattr(auth_response, "session", None)
        auth_user = getattr(auth_response, "user", None)
        access_token = getattr(auth_session, "access_token", None)
        expires_at = getattr(auth_session, "expires_at", None)
        user_email = getattr(auth_user, "email", email)
        if not access_token or not expires_at:
            return False
        session[ADMIN_SESSION_KEY] = {
            "email": user_email,
            "access_token": access_token,
            "expires_at": float(expires_at),
        }
        session.permanent = True
        return True
    except Exception:
        return False


def logout_admin() -> None:
    session.pop(ADMIN_SESSION_KEY, None)


def require_admin() -> None:
    if not is_admin_authenticated():
        abort(401, description="Unauthorized")


def verify_telegram_init_data(init_data: str | None) -> dict | None:
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        provided_hash = pairs.pop("hash", None)
        if not provided_hash:
            return None
        data_check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", settings.telegram_bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, provided_hash):
            return None

        user_raw = pairs.get("user")
        if not user_raw:
            return None
        user = json.loads(user_raw)
        telegram_user_id = str(user.get("id") or "").strip()
        if not telegram_user_id:
            return None
        return {"telegram_user_id": telegram_user_id, "user": user, "is_admin": is_bot_admin(telegram_user_id)}
    except Exception:
        return None




def _fallback_telegram_user_id() -> str | None:
    candidate = (
        request.headers.get("x-telegram-user-id")
        or request.args.get("telegram_user_id")
        or request.args.get("uid")
    )
    if candidate:
        telegram_user_id = str(candidate).strip()
        if telegram_user_id.isdigit():
            return telegram_user_id

    start_param_candidate = (
        request.headers.get("x-telegram-start-param")
        or request.args.get("tgWebAppStartParam")
        or request.args.get("startapp")
        or request.args.get("start_param")
    )
    if not start_param_candidate:
        return None

    start_param = str(start_param_candidate).strip()
    if not start_param:
        return None

    if start_param.isdigit():
        return start_param

    for prefix in ("uid_", "uid:", "user_", "user:"):
        if start_param.startswith(prefix):
            suffix = start_param[len(prefix) :].strip()
            if suffix.isdigit():
                return suffix

    return None

def mini_app_session(required: bool = True) -> dict | None:
    init_data_candidates = [
        request.headers.get("x-telegram-init-data"),
        request.args.get("tg_init_data"),
        request.args.get("tgWebAppData"),
    ]
    session = None
    for candidate in init_data_candidates:
        if not candidate:
            continue
        session = verify_telegram_init_data(candidate)
        if session:
            break
        # Some entry points pass tgWebAppData URL-encoded.
        decoded_once = unquote(candidate)
        if decoded_once != candidate:
            session = verify_telegram_init_data(decoded_once)
            if session:
                break

    if not session:
        fallback_user_id = _fallback_telegram_user_id()
        if fallback_user_id:
            session = {
                "telegram_user_id": fallback_user_id,
                "user": {"id": int(fallback_user_id)},
                "is_admin": is_bot_admin(fallback_user_id),
            }

    if required and not session:
        abort(401, description="Telegram mini app authentication failed")
    return session
