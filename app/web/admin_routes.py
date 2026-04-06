from __future__ import annotations

import csv
from io import StringIO
import uuid

from flask import Blueprint, Response, redirect, render_template, request

from app.services import (
    VALID_AGENT_TAGS,
    VALID_PERFORMANCE_EVENT_TYPES,
    VALID_STATUSES,
    VALID_TERRITORY_AVAILABILITY,
    add_bot_admin,
    delete_performance_event,
    delete_territory,
    delete_training_progress,
    get_application,
    get_applications,
    list_bot_admins,
    list_location_options,
    list_performance_events,
    list_territories_admin,
    list_training_progress,
    remove_bot_admin,
    update_territory,
    update_application_status,
    upsert_training_progress,
    create_performance_event,
    create_territory,
    get_training_links,
    list_app_settings,
    upsert_app_setting,
    upload_telegram_file,
)
from app.web.auth import is_admin_authenticated, login_admin, logout_admin, require_admin
from app.web.constants import EXPORT_FIELDNAMES, VALID_PERFORMANCE_LEVELS
from app.web.helpers import is_image_url


def register_admin_routes(blueprint: Blueprint, onboarding_callback) -> None:
    @blueprint.get("/admin/login")
    def admin_login_page() -> Response:
        if is_admin_authenticated():
            return redirect("/admin", code=302)
        return Response(render_template("admin_login.html", error_message=""), mimetype="text/html")

    @blueprint.post("/admin/login")
    def admin_login_submit() -> Response:
        email = str(request.form.get("email") or "").strip()
        password = str(request.form.get("password") or "")
        if login_admin(email=email, password=password):
            return redirect("/admin", code=303)
        return Response(
            render_template("admin_login.html", error_message="Invalid credentials. Login only; registration is disabled."),
            mimetype="text/html",
            status=401,
        )

    @blueprint.post("/admin/logout")
    def admin_logout() -> Response:
        logout_admin()
        return redirect("/admin/login", code=303)

    @blueprint.get("/admin")
    def admin_dashboard() -> Response:
        if not is_admin_authenticated():
            return redirect("/admin/login", code=302)
        region = request.args.get("region")
        applicant_type = request.args.get("applicant_type")
        status = request.args.get("status")
        apps = get_applications(region=region, applicant_type=applicant_type, status=status)
        territories = list_territories_admin(region=region or None, zone=request.args.get("zone"), woreda=request.args.get("woreda"))
        admins = list_bot_admins()
        performance_events = list_performance_events(application_id=request.args.get("application_id"))
        training_progress = list_training_progress(application_id=request.args.get("application_id"))
        app_settings = list_app_settings()
        training_links = get_training_links()

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
                uploads.append({"label": label, "url": url, "is_image": is_image_url(url)})
            rows.append({"app": app_row, "uploads": uploads})

        location_options = list_location_options()
        known_regions = location_options.get("regions", [])
        known_zones = location_options.get("zones", [])
        known_woredas = location_options.get("woredas", [])
        location_rows = location_options.get("rows", [])

        return Response(
            render_template(
                "admin_dashboard.html",
                apps=rows,
                region=region or "",
                applicant_type=applicant_type or "",
                status=status or "",
                valid_statuses=sorted(VALID_STATUSES),
                valid_agent_tags=sorted(VALID_AGENT_TAGS),
                valid_performance_levels=sorted(VALID_PERFORMANCE_LEVELS),
                valid_performance_event_types=sorted(VALID_PERFORMANCE_EVENT_TYPES),
                valid_territory_availability=sorted(VALID_TERRITORY_AVAILABILITY),
                territories=territories,
                admins=admins,
                performance_events=performance_events,
                training_progress=training_progress,
                app_settings=app_settings,
                training_links=training_links,
                mini_app_default_language=next((item.get("setting_value") for item in app_settings if item.get("setting_key") == "default_mini_app_language"), "en"),
                known_regions=known_regions,
                known_zones=known_zones,
                known_woredas=known_woredas,
                location_rows=location_rows,
            ),
            mimetype="text/html",
        )

    @blueprint.post("/admin/settings")
    def admin_upsert_settings():
        require_admin()
        form = request.form
        updated_by = str(form.get("updated_by") or "").strip() or None
        for key in ("training_pdf_url", "training_video_url", "sales_playbook_url", "default_mini_app_language"):
            value = str(form.get(key) or "").strip()
            if value:
                upsert_app_setting(setting_key=key, setting_value=value, updated_by=updated_by)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/settings/training-materials/upload")
    def admin_upload_training_material():
        require_admin()
        material_key = str(request.form.get("material_key") or "").strip()
        file = request.files.get("file")
        if file and material_key in {"training_pdf_url", "training_video_url", "sales_playbook_url"}:
            content_type = (file.content_type or "application/octet-stream").lower()
            extension = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "bin"
            storage_name = f"{material_key}-{uuid.uuid4().hex}.{extension}"
            url = upload_telegram_file(
                file_bytes=file.read(),
                folder="training_materials",
                filename=storage_name,
                content_type=content_type,
                upsert=False,
            )
            upsert_app_setting(setting_key=material_key, setting_value=url)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/applications/<application_id>/status")
    def admin_update_status(application_id: str):
        require_admin()
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
            onboarding_callback(updated)

        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/territories")
    def admin_create_territory():
        require_admin()
        form = request.form
        create_territory(
            region=str(form.get("region") or ""),
            zone=str(form.get("zone") or ""),
            woreda=str(form.get("woreda") or ""),
            kebele=str(form.get("kebele") or ""),
            village=str(form.get("village") or ""),
            latitude=float(form.get("latitude")) if form.get("latitude") else None,
            longitude=float(form.get("longitude")) if form.get("longitude") else None,
            availability_status=str(form.get("availability_status") or "open"),
            is_locked=bool(form.get("is_locked")),
        )
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/territories/<territory_id>")
    def admin_update_territory(territory_id: str):
        require_admin()
        form = request.form
        update_territory(
            territory_id,
            {
                "availability_status": str(form.get("availability_status") or ""),
                "is_locked": bool(form.get("is_locked")),
                "assigned_application_id": str(form.get("assigned_application_id") or "").strip() or None,
            },
        )
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/territories/<territory_id>/delete")
    def admin_delete_territory(territory_id: str):
        require_admin()
        delete_territory(territory_id)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/bot-admins")
    def admin_add_bot_admin():
        require_admin()
        telegram_user_id = str(request.form.get("telegram_user_id") or "").strip()
        created_by = str(request.form.get("created_by") or "").strip() or None
        if telegram_user_id:
            add_bot_admin(telegram_user_id=telegram_user_id, created_by=created_by)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/bot-admins/<telegram_user_id>/delete")
    def admin_delete_bot_admin(telegram_user_id: str):
        require_admin()
        remove_bot_admin(telegram_user_id)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/performance-events")
    def admin_create_performance_event():
        require_admin()
        form = request.form
        create_performance_event(
            application_id=str(form.get("application_id") or "").strip(),
            event_type=str(form.get("event_type") or "").strip(),
            event_value=float(form.get("event_value") or 0),
            metadata={},
            occurred_at=str(form.get("occurred_at") or "").strip() or None,
        )
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/performance-events/<event_id>/delete")
    def admin_delete_performance_event(event_id: str):
        require_admin()
        delete_performance_event(event_id)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/training-progress")
    def admin_upsert_training_progress():
        require_admin()
        form = request.form
        upsert_training_progress(
            application_id=str(form.get("application_id") or "").strip(),
            module_key=str(form.get("module_key") or "").strip(),
            completed=bool(form.get("completed")),
        )
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.post("/admin/training-progress/<progress_id>/delete")
    def admin_delete_training_progress(progress_id: str):
        require_admin()
        delete_training_progress(progress_id)
        token = request.args.get("token")
        return redirect(f"/admin?token={token}" if token else "/admin", code=303)

    @blueprint.get("/admin/export.csv")
    @blueprint.get("/admin/export.xlsx")
    def admin_export() -> Response:
        require_admin()
        apps = get_applications(
            region=request.args.get("region"),
            applicant_type=request.args.get("applicant_type"),
            status=request.args.get("status"),
        )
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDNAMES)
        writer.writeheader()
        for row in apps:
            writer.writerow({key: row.get(key) for key in EXPORT_FIELDNAMES})

        content = output.getvalue()
        is_excel = request.path.endswith(".xlsx")
        mimetype = "application/vnd.ms-excel" if is_excel else "text/csv"
        filename = "agent_lifecycle_export.xlsx" if is_excel else "agent_lifecycle_export.csv"
        return Response(
            content,
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
