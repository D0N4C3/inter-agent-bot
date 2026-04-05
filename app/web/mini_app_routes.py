from __future__ import annotations

from datetime import datetime, timezone
import uuid

from flask import Blueprint, Response, render_template, request

from app.config import settings
from app.i18n import load_mini_app_strings
from app.scoring import score_application
from app.services import (
    VALID_UI_LANGUAGES,
    default_agent_tag,
    get_app_setting,
    list_location_options,
    list_territories_for_map,
    save_application,
    send_admin_telegram_alert,
    send_notification_email,
    suggest_nearest_territories,
    upload_telegram_file,
)
from app.web.auth import mini_app_session


def register_mini_app_routes(blueprint: Blueprint) -> None:
    @blueprint.get("/mini-app")
    def mini_app() -> Response:
        default_lang = get_app_setting("default_mini_app_language", "en") or "en"
        if default_lang not in VALID_UI_LANGUAGES:
            default_lang = "en"
        return Response(
            render_template(
                "mini_app.html",
                mini_app_name=settings.mini_app_name,
                mini_app_primary_color=settings.mini_app_primary_color,
                google_maps_sdk_key=settings.google_maps_sdk_key,
                default_lang=default_lang,
                mini_app_strings=load_mini_app_strings(),
            ),
            mimetype="text/html",
        )

    @blueprint.post("/api/mini-app/upload")
    def mini_app_upload() -> dict:
        mini_app_session(required=True)
        file = request.files.get("file")
        if not file:
            return {"ok": False, "error": "file is required"}, 400

        file_bytes = file.read()
        if not file_bytes:
            return {"ok": False, "error": "File is empty"}, 400

        max_size = settings.max_upload_size_mb * 1024 * 1024
        if len(file_bytes) > max_size:
            return {"ok": False, "error": f"File too large. Max size is {settings.max_upload_size_mb}MB."}, 400

        content_type = (file.content_type or "").lower()
        allowed_types = {"image/jpeg", "image/jpg", "image/png", "image/webp", "application/pdf"}
        if content_type not in allowed_types:
            return {"ok": False, "error": "Unsupported file format. Please upload JPG, PNG, WEBP, or PDF."}, 400

        ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
        extension = ext or ("pdf" if content_type == "application/pdf" else "jpg")
        filename = f"{uuid.uuid4().hex}.{extension}"
        file_url = upload_telegram_file(
            file_bytes=file_bytes,
            folder="mini_app_uploads",
            filename=filename,
            content_type=content_type,
            upsert=False,
        )
        return {"ok": True, "url": file_url}

    @blueprint.post("/api/mini-app/register")
    def mini_app_register() -> dict:
        session = mini_app_session(required=True)
        payload = request.get_json(silent=True) or {}
        required = ["full_name", "phone", "region", "zone", "woreda", "preferred_territory"]
        missing = [field for field in required if not payload.get(field)]
        if missing:
            return {"ok": False, "error": f"Missing fields: {', '.join(missing)}"}, 400

        score = score_application(payload)
        record = {
            "telegram_user_id": str(session["telegram_user_id"]),
            "full_name": payload["full_name"],
            "phone": payload["phone"],
            "applicant_type": payload.get("applicant_type", "sales_installer"),
            "region": payload["region"],
            "zone": payload["zone"],
            "woreda": payload["woreda"],
            "kebele": payload.get("kebele") or "N/A",
            "village": payload.get("village") or payload.get("woreda"),
            "experience": bool(payload.get("experience", False)),
            "experience_years": int(payload.get("experience_years") or 0),
            "work_type": payload.get("work_type", "N/A"),
            "has_shop": bool(payload.get("has_shop", False)),
            "business_name": payload.get("business_name"),
            "business_type": payload.get("business_type"),
            "business_years": int(payload.get("business_years") or 0),
            "business_customers_weekly": int(payload.get("business_customers_weekly") or 0),
            "can_install": bool(payload.get("can_install", False)),
            "preferred_territory": payload["preferred_territory"],
            "picked_latitude": payload.get("picked_latitude"),
            "picked_longitude": payload.get("picked_longitude"),
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

    @blueprint.get("/api/territories/map")
    def territories_map() -> dict:
        occupied_only = request.args.get("occupied_only", "").lower() in {"1", "true", "yes"}
        items = list_territories_for_map(
            region=request.args.get("region"),
            zone=request.args.get("zone"),
            woreda=request.args.get("woreda"),
            occupied_only=occupied_only,
        )
        return {"ok": True, "items": items}

    @blueprint.get("/api/locations/options")
    def location_options() -> dict:
        return {"ok": True, "options": list_location_options()}

    @blueprint.post("/api/territories/nearest")
    def nearest_territories() -> dict:
        payload = request.get_json(silent=True) or {}
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if latitude is None or longitude is None:
            return {"ok": False, "error": "latitude and longitude are required"}, 400
        items = suggest_nearest_territories(float(latitude), float(longitude), settings.territory_suggestion_limit)
        return {"ok": True, "items": items}

    @blueprint.get("/api/mini-app/session")
    def mini_app_session_api() -> dict:
        session = mini_app_session(required=True)
        return {"ok": True, "session": session}
