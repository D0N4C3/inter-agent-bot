from __future__ import annotations

import csv
from io import StringIO

from flask import Blueprint, Response, redirect, render_template, request

from app.services import (
    VALID_AGENT_TAGS,
    VALID_STATUSES,
    get_application,
    get_applications,
    update_application_status,
)
from app.web.auth import require_admin
from app.web.constants import EXPORT_FIELDNAMES, VALID_PERFORMANCE_LEVELS
from app.web.helpers import is_image_url


def register_admin_routes(blueprint: Blueprint, onboarding_callback) -> None:
    @blueprint.get("/admin")
    def admin_dashboard() -> Response:
        require_admin()
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
                uploads.append({"label": label, "url": url, "is_image": is_image_url(url)})
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
