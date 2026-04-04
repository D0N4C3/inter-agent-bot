from __future__ import annotations

import hashlib
import hmac
import json
import csv
from datetime import datetime, timezone
from io import StringIO
from urllib.parse import parse_qsl

from flask import Blueprint, Response, abort, redirect, render_template, request

from app.config import settings
from app.scoring import score_application
from app.services import (
    VALID_AGENT_TAGS,
    VALID_PERFORMANCE_EVENT_TYPES,
    VALID_STATUSES,
    create_performance_event,
    default_agent_tag,
    get_agent_dashboard,
    get_application,
    get_applications,
    get_rankings,
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
)

VALID_PERFORMANCE_LEVELS = {"High", "Medium", "Low"}


class WebModule:
    def __init__(self, onboarding_callback):
        self.onboarding_callback = onboarding_callback
        self.blueprint = Blueprint("web_module", __name__)
        self._register_routes()

    def _register_routes(self) -> None:
        self.blueprint.add_url_rule("/admin", view_func=self.admin_dashboard, methods=["GET"])
        self.blueprint.add_url_rule(
            "/admin/applications/<application_id>/status",
            view_func=self.admin_update_status,
            methods=["POST"],
        )
        self.blueprint.add_url_rule("/admin/export.csv", view_func=self.admin_export, methods=["GET"])
        self.blueprint.add_url_rule("/admin/export.xlsx", view_func=self.admin_export, methods=["GET"])
        self.blueprint.add_url_rule("/mini-app", view_func=self.mini_app, methods=["GET"])
        self.blueprint.add_url_rule("/api/mini-app/register", view_func=self.mini_app_register, methods=["POST"])
        self.blueprint.add_url_rule("/api/territories/map", view_func=self.territories_map, methods=["GET"])
        self.blueprint.add_url_rule("/api/territories/nearest", view_func=self.nearest_territories, methods=["POST"])
        self.blueprint.add_url_rule(
            "/api/agent/dashboard/<telegram_user_id>", view_func=self.agent_dashboard_api, methods=["GET"]
        )
        self.blueprint.add_url_rule(
            "/api/agent/dashboard/<telegram_user_id>/profile",
            view_func=self.agent_profile_update_api,
            methods=["PATCH"],
        )
        self.blueprint.add_url_rule(
            "/api/agent/training/<application_id>", view_func=self.agent_training_progress_api, methods=["POST"]
        )
        self.blueprint.add_url_rule("/api/performance/events", view_func=self.performance_event_api, methods=["POST"])
        self.blueprint.add_url_rule("/api/rankings", view_func=self.rankings_api, methods=["GET"])
        self.blueprint.add_url_rule("/api/mini-app/session", view_func=self.mini_app_session_api, methods=["GET"])

    def _require_admin(self) -> None:
        expected = settings.admin_dashboard_token
        if not expected:
            return
        provided = request.args.get("token") or request.headers.get("x-admin-token")
        if provided != expected:
            abort(401, description="Unauthorized")

    def _verify_telegram_init_data(self, init_data: str | None) -> dict | None:
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

    def _mini_app_session(self, required: bool = True) -> dict | None:
        init_data = request.headers.get("x-telegram-init-data") or request.args.get("tg_init_data")
        session = self._verify_telegram_init_data(init_data)
        if required and not session:
            abort(401, description="Telegram mini app authentication failed")
        return session

    @staticmethod
    def _is_image_url(url: str | None) -> bool:
        if not url:
            return False
        return url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))

    def admin_dashboard(self) -> Response:
        self._require_admin()
        region = request.args.get("region")
        applicant_type = request.args.get("applicant_type")
        status = request.args.get("status")
        token = request.args.get("token", "")
        apps = get_applications(region=region, applicant_type=applicant_type, status=status)

        rows = []
        for app_row in apps:
            uploads = []
            for label, url in (
                ("Front ID", app_row.get("id_file_front_url")),
                ("Back ID", app_row.get("id_file_back_url")),
                ("Profile", app_row.get("profile_photo_url")),
            ):
                if not url:
                    continue
                uploads.append({"label": label, "url": url, "is_image": self._is_image_url(url)})
            rows.append({"app": app_row, "uploads": uploads})

        return Response(
            render_template(
                "admin_dashboard.html",
                apps=rows,
                region=region or "",
                applicant_type=applicant_type or "",
                status=status or "",
                token=token,
                valid_statuses=sorted(VALID_STATUSES),
                valid_agent_tags=sorted(VALID_AGENT_TAGS),
                valid_performance_levels=sorted(VALID_PERFORMANCE_LEVELS),
            ),
            mimetype="text/html",
        )

    def admin_update_status(self, application_id: str):
        self._require_admin()
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
            self.onboarding_callback(updated)
        token = request.args.get("token")
        redirect_url = "/admin"
        if token:
            redirect_url = f"/admin?token={token}"
        return redirect(redirect_url, code=303)

    def admin_export(self) -> Response:
        self._require_admin()
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

    def mini_app(self) -> Response:
        return Response(
            render_template(
                "mini_app.html",
                mini_app_name=settings.mini_app_name,
                mini_app_primary_color=settings.mini_app_primary_color,
            ),
            mimetype="text/html",
        )

    def mini_app_register(self) -> dict:
        session = self._mini_app_session(required=True)
        payload = request.get_json(silent=True) or {}
        required = [
            "full_name", "phone", "region", "zone", "woreda", "kebele", "village", "preferred_territory",
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

    def territories_map(self) -> dict:
        items = list_territories_for_map(
            region=request.args.get("region"),
            zone=request.args.get("zone"),
            woreda=request.args.get("woreda"),
        )
        return {"ok": True, "items": items}

    def nearest_territories(self) -> dict:
        payload = request.get_json(silent=True) or {}
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if latitude is None or longitude is None:
            return {"ok": False, "error": "latitude and longitude are required"}, 400
        items = suggest_nearest_territories(float(latitude), float(longitude), settings.territory_suggestion_limit)
        return {"ok": True, "items": items}

    def agent_dashboard_api(self, telegram_user_id: str) -> dict:
        session = self._mini_app_session(required=True)
        if str(session["telegram_user_id"]) != str(telegram_user_id) and not session["is_admin"]:
            return {"ok": False, "error": "Forbidden"}, 403
        dashboard = get_agent_dashboard(telegram_user_id)
        if not dashboard:
            return {"ok": False, "error": "Agent not found"}, 404
        dashboard["training_links"] = {
            "pdf": settings.training_pdf_url,
            "video": settings.training_video_url,
            "sales_playbook": settings.sales_playbook_url,
        }
        return {"ok": True, "dashboard": dashboard}

    def agent_profile_update_api(self, telegram_user_id: str) -> dict:
        session = self._mini_app_session(required=True)
        if str(session["telegram_user_id"]) != str(telegram_user_id) and not session["is_admin"]:
            return {"ok": False, "error": "Forbidden"}, 403
        payload = request.get_json(silent=True) or {}
        updated = update_agent_profile(telegram_user_id, payload)
        return {"ok": True, "application": updated}

    def agent_training_progress_api(self, application_id: str) -> dict:
        session = self._mini_app_session(required=True)
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

    def performance_event_api(self) -> dict:
        session = self._mini_app_session(required=False)
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

    @staticmethod
    def rankings_api() -> dict:
        return {"ok": True, "rankings": get_rankings()}

    def mini_app_session_api(self) -> dict:
        session = self._mini_app_session(required=True)
        return {"ok": True, "session": session}
