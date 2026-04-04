from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from flask import abort, request

from app.config import settings
from app.services import is_bot_admin


def require_admin() -> None:
    expected = settings.admin_dashboard_token
    if not expected:
        return
    provided = request.args.get("token") or request.headers.get("x-admin-token")
    if provided != expected:
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


def mini_app_session(required: bool = True) -> dict | None:
    init_data = request.headers.get("x-telegram-init-data") or request.args.get("tg_init_data")
    session = verify_telegram_init_data(init_data)
    if required and not session:
        abort(401, description="Telegram mini app authentication failed")
    return session
