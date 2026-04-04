from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, Response, render_template, request

from app.config import settings
from app.scoring import score_application
from app.services import (
    default_agent_tag,
    list_territories_for_map,
    save_application,
    send_admin_telegram_alert,
    send_notification_email,
    suggest_nearest_territories,
    territory_is_available,
)
from app.web.auth import mini_app_session


def register_mini_app_routes(blueprint: Blueprint) -> None:
    @blueprint.get("/mini-app")
    def mini_app() -> Response:
        return Response(
            render_template(
                "mini_app.html",
                mini_app_name=settings.mini_app_name,
                mini_app_primary_color=settings.mini_app_primary_color,
            ),
            mimetype="text/html",
        )

    @blueprint.post("/api/mini-app/register")
    def mini_app_register() -> dict:
        session = mini_app_session(required=True)
        payload = request.get_json(silent=True) or {}
        required = ["full_name", "phone", "region", "zone", "woreda", "kebele", "village", "preferred_territory"]
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
            "telegram_user_id": str(session["telegram_user_id"]),
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

    @blueprint.get("/api/territories/map")
    def territories_map() -> dict:
        items = list_territories_for_map(
            region=request.args.get("region"),
            zone=request.args.get("zone"),
            woreda=request.args.get("woreda"),
        )
        return {"ok": True, "items": items}

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
