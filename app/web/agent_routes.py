from __future__ import annotations

from flask import Blueprint, abort, request

from app.config import settings
from app.services import (
    VALID_PERFORMANCE_EVENT_TYPES,
    create_performance_event,
    get_agent_dashboard,
    get_application,
    get_training_links,
    get_training_modules_for_agent,
    get_rankings,
    update_agent_profile,
    upsert_training_progress,
)
from app.web.auth import mini_app_session


def register_agent_routes(blueprint: Blueprint) -> None:
    @blueprint.get("/api/agent/dashboard/<telegram_user_id>")
    def agent_dashboard_api(telegram_user_id: str) -> dict:
        session = mini_app_session(required=True)
        if str(session["telegram_user_id"]) != str(telegram_user_id) and not session["is_admin"]:
            return {"ok": False, "error": "Forbidden"}, 403

        dashboard = get_agent_dashboard(telegram_user_id)
        if not dashboard:
            return {"ok": False, "error": "Agent not found"}, 404

        training_links = get_training_links()
        profile = dashboard.get("profile") or {}
        modules = get_training_modules_for_agent(profile.get("status"), profile.get("applicant_type"))
        for module in modules:
            module["attachments"] = {
                "pdf": training_links.get("pdf"),
                "video": training_links.get("video"),
                "playbook": training_links.get("sales_playbook"),
            }
        dashboard["training_links"] = training_links
        dashboard["training_modules"] = modules
        return {"ok": True, "dashboard": dashboard}

    @blueprint.patch("/api/agent/dashboard/<telegram_user_id>/profile")
    def agent_profile_update_api(telegram_user_id: str) -> dict:
        session = mini_app_session(required=True)
        if str(session["telegram_user_id"]) != str(telegram_user_id) and not session["is_admin"]:
            return {"ok": False, "error": "Forbidden"}, 403
        payload = request.get_json(silent=True) or {}
        updated = update_agent_profile(telegram_user_id, payload)
        return {"ok": True, "application": updated}

    @blueprint.post("/api/agent/training/<application_id>")
    def agent_training_progress_api(application_id: str) -> dict:
        session = mini_app_session(required=True)
        app_row = get_application(application_id)
        if not app_row:
            return {"ok": False, "error": "Application not found"}, 404
        if str(app_row.get("telegram_user_id")) != str(session["telegram_user_id"]) and not session["is_admin"]:
            return {"ok": False, "error": "Forbidden"}, 403

        payload = request.get_json(silent=True) or {}
        module_key = str(payload.get("module_key") or "").strip()
        if not module_key:
            return {"ok": False, "error": "module_key is required"}, 400

        completed = bool(payload.get("completed", False))
        result = upsert_training_progress(application_id, module_key, completed)
        return {"ok": True, "training_progress": result}

    @blueprint.post("/api/performance/events")
    def performance_event_api() -> dict:
        session = mini_app_session(required=False)
        token = request.args.get("token") or request.headers.get("x-admin-token")
        token_ok = bool(settings.admin_dashboard_token and token == settings.admin_dashboard_token)
        if not ((session and session.get("is_admin")) or token_ok):
            abort(401, description="Unauthorized")

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

    @blueprint.get("/api/rankings")
    def rankings_api() -> dict:
        return {"ok": True, "rankings": get_rankings()}
