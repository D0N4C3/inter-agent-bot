import smtplib
import uuid
from math import asin, cos, radians, sin, sqrt
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx
from postgrest.exceptions import APIError
from supabase import Client, create_client
from supabase.lib.client_options import ClientOptions

from app.config import settings


VALID_STATUSES = {
    "Submitted",
    "Under Review",
    "Approved",
    "Rejected",
    "More Info Required",
}

VALID_AGENT_TAGS = {
    "Sales Agent",
    "Installer Agent",
    "Hybrid",
}

VALID_PERFORMANCE_EVENT_TYPES = {
    "sale_closed",
    "installer_job_completed",
    "training_completed",
}


def get_supabase() -> Client:
    client = create_client(
        settings.supabase_url,
        settings.supabase_key,
        options=ClientOptions(schema=settings.supabase_schema),
    )
    return client


def upload_telegram_file(
    file_bytes: bytes,
    folder: str,
    filename: str,
    content_type: str,
    upsert: bool = False,
) -> str:
    client = get_supabase()
    safe_folder = folder.strip("/").replace("..", "")
    safe_filename = filename.split("/")[-1].replace("..", "")
    path = f"{safe_folder}/{safe_filename}"
    client.storage.from_(settings.supabase_storage_bucket).upload(
        path=path,
        file=file_bytes,
        file_options={
            "upsert": "true" if upsert else "false",
            "content-type": content_type,
        },
    )
    return client.storage.from_(settings.supabase_storage_bucket).get_public_url(path)


def save_application(record: dict) -> dict:
    client = get_supabase()
    result = client.table("agent_applications").insert(record).execute()
    return result.data[0]


def default_agent_tag(applicant_type: str) -> str:
    mapping = {
        "sales_only": "Sales Agent",
        "installer_only": "Installer Agent",
        "sales_installer": "Hybrid",
    }
    return mapping.get(applicant_type, "Hybrid")


def save_application_draft(
    telegram_user_id: str,
    applicant_type: str,
    language: str,
    step_index: int,
    answers: dict,
) -> None:
    client = get_supabase()
    payload = {
        "telegram_user_id": telegram_user_id,
        "applicant_type": applicant_type,
        "language": language,
        "step_index": step_index,
        "answers": answers,
        "reminder_sent_at": None,
    }
    existing = (
        client.table("application_drafts")
        .select("draft_id")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        draft_id = existing.data[0]["draft_id"]
        client.table("application_drafts").update(payload).eq("draft_id", draft_id).execute()
    else:
        payload["draft_id"] = str(uuid.uuid4())
        client.table("application_drafts").insert(payload).execute()


def get_application_draft(telegram_user_id: str) -> dict | None:
    client = get_supabase()
    result = (
        client.table("application_drafts")
        .select("*")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def delete_application_draft(telegram_user_id: str) -> None:
    client = get_supabase()
    client.table("application_drafts").delete().eq("telegram_user_id", telegram_user_id).execute()


def get_stale_drafts(hours: int = 24) -> list[dict]:
    client = get_supabase()
    result = client.rpc("get_stale_application_drafts", {"cutoff_hours": hours}).execute()
    return result.data or []


def mark_draft_reminder_sent(telegram_user_id: str) -> None:
    client = get_supabase()
    client.table("application_drafts").update({"reminder_sent_at": datetime.now(timezone.utc).isoformat()}).eq("telegram_user_id", telegram_user_id).execute()


def get_application(application_id: str) -> dict | None:
    client = get_supabase()
    result = client.table("agent_applications").select("*").eq("application_id", application_id).limit(1).execute()
    if result.data:
        return result.data[0]
    return None


def get_applications(region: str | None = None, applicant_type: str | None = None, status: str | None = None) -> list[dict]:
    client = get_supabase()
    query = client.table("agent_applications").select("*").order("submitted_at", desc=True)
    if region:
        query = query.eq("region", region)
    if applicant_type:
        query = query.eq("applicant_type", applicant_type)
    if status:
        query = query.eq("status", status)
    return query.execute().data or []


def get_application_by_telegram_user(telegram_user_id: str) -> dict | None:
    client = get_supabase()
    result = (
        client.table("agent_applications")
        .select("*")
        .eq("telegram_user_id", telegram_user_id)
        .order("submitted_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def get_latest_status_by_telegram_user(telegram_user_id: str) -> str | None:
    client = get_supabase()
    result = (
        client.table("agent_applications")
        .select("status")
        .eq("telegram_user_id", telegram_user_id)
        .order("submitted_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["status"]
    return None


def get_latest_status_by_phone(phone: str) -> str | None:
    client = get_supabase()
    result = (
        client.table("agent_applications")
        .select("status")
        .eq("phone", phone)
        .order("submitted_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["status"]
    return None


def count_admins() -> int:
    client = get_supabase()
    result = client.table("bot_admins").select("telegram_user_id", count="exact").limit(1).execute()
    return int(result.count or 0)


def is_bot_admin(telegram_user_id: str) -> bool:
    client = get_supabase()
    result = (
        client.table("bot_admins")
        .select("telegram_user_id")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def add_bot_admin(telegram_user_id: str, created_by: str | None = None) -> tuple[bool, dict]:
    client = get_supabase()
    existing = (
        client.table("bot_admins")
        .select("*")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return False, existing.data[0]

    payload: dict = {"telegram_user_id": telegram_user_id}
    if created_by:
        payload["created_by"] = created_by
    inserted = client.table("bot_admins").insert(payload).execute()
    return True, inserted.data[0]


def list_open_territories(region: str | None = None, zone: str | None = None, woreda: str | None = None) -> list[dict]:
    client = get_supabase()
    query = client.table("territories").select("region,zone,woreda,kebele,village").eq("is_locked", False)
    if region:
        query = query.eq("region", region)
    if zone:
        query = query.eq("zone", zone)
    if woreda:
        query = query.eq("woreda", woreda)
    return query.limit(10).execute().data or []


def list_territories_for_map(
    region: str | None = None,
    zone: str | None = None,
    woreda: str | None = None,
) -> list[dict]:
    client = get_supabase()
    query = client.table("territories").select(
        "territory_id,region,zone,woreda,kebele,village,latitude,longitude,is_locked,availability_status"
    )
    if region:
        query = query.eq("region", region)
    if zone:
        query = query.eq("zone", zone)
    if woreda:
        query = query.eq("woreda", woreda)
    return query.limit(500).execute().data or []


def territory_is_available(
    preferred_territory: str,
    region: str | None = None,
    zone: str | None = None,
    woreda: str | None = None,
    kebele: str | None = None,
) -> bool:
    client = get_supabase()
    try:
        query = client.table("territories").select("is_locked").eq("village", preferred_territory)
        if region:
            query = query.eq("region", region)
        if zone:
            query = query.eq("zone", zone)
        if woreda:
            query = query.eq("woreda", woreda)
        if kebele:
            query = query.eq("kebele", kebele)

        result = query.limit(1).execute()
    except APIError:
        return True

    if not result.data:
        return True
    return not bool(result.data[0]["is_locked"])


def lock_territory_for_application(application_id: str, region: str, zone: str, woreda: str, kebele: str, village: str) -> None:
    client = get_supabase()
    lookup = (
        client.table("territories")
        .select("territory_id")
        .eq("region", region)
        .eq("zone", zone)
        .eq("woreda", woreda)
        .eq("kebele", kebele)
        .eq("village", village)
        .limit(1)
        .execute()
    )

    territory_payload = {
        "region": region,
        "zone": zone,
        "woreda": woreda,
        "kebele": kebele,
        "village": village,
        "is_locked": True,
        "assigned_application_id": application_id,
        "availability_status": "assigned",
    }

    if lookup.data:
        territory_id = lookup.data[0]["territory_id"]
        client.table("territories").update(territory_payload).eq("territory_id", territory_id).execute()
    else:
        client.table("territories").insert(territory_payload).execute()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(a))


def suggest_nearest_territories(latitude: float, longitude: float, limit: int = 5) -> list[dict]:
    territories = list_territories_for_map()
    candidates = []
    for row in territories:
        if row.get("is_locked"):
            continue
        lat = row.get("latitude")
        lon = row.get("longitude")
        if lat is None or lon is None:
            continue
        distance_km = _haversine_km(latitude, longitude, float(lat), float(lon))
        row["distance_km"] = round(distance_km, 2)
        candidates.append(row)
    candidates.sort(key=lambda item: item["distance_km"])
    return candidates[: max(limit, 1)]


def update_application_status(
    application_id: str,
    status: str,
    admin_notes: str | None = None,
    territory_village: str | None = None,
    agent_tag: str | None = None,
    performance_potential: str | None = None,
    internal_remarks: str | None = None,
) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError("Invalid status")

    application = get_application(application_id)
    if not application:
        raise ValueError("Application not found")

    updates: dict = {"status": status}
    if admin_notes is not None:
        updates["admin_notes"] = admin_notes
    if agent_tag is not None:
        if agent_tag not in VALID_AGENT_TAGS:
            raise ValueError("Invalid agent tag")
        updates["agent_tag"] = agent_tag
    if performance_potential is not None:
        updates["performance_potential"] = performance_potential
    if internal_remarks is not None:
        updates["internal_remarks"] = internal_remarks

    if status == "Approved":
        village = territory_village or application["preferred_territory"]
        if not territory_is_available(
            village,
            region=application["region"],
            zone=application["zone"],
            woreda=application["woreda"],
            kebele=application["kebele"],
        ):
            raise ValueError("Territory already locked")

        lock_territory_for_application(
            application_id,
            region=application["region"],
            zone=application["zone"],
            woreda=application["woreda"],
            kebele=application["kebele"],
            village=village,
        )
        updates["preferred_territory"] = village

    result = client = get_supabase()
    updated = client.table("agent_applications").update(updates).eq("application_id", application_id).execute()
    return updated.data[0]


def create_performance_event(
    application_id: str,
    event_type: str,
    event_value: float,
    metadata: dict | None = None,
    occurred_at: str | None = None,
) -> dict:
    if event_type not in VALID_PERFORMANCE_EVENT_TYPES:
        raise ValueError("Invalid performance event type")
    application = get_application(application_id)
    if not application:
        raise ValueError("Application not found")
    client = get_supabase()
    payload = {
        "application_id": application_id,
        "event_type": event_type,
        "event_value": event_value,
        "metadata": metadata or {},
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
    }
    result = client.table("agent_performance_events").insert(payload).execute()
    return result.data[0]


def get_agent_dashboard(telegram_user_id: str) -> dict | None:
    application = get_application_by_telegram_user(telegram_user_id)
    if not application:
        return None
    client = get_supabase()
    metrics = (
        client.table("agent_performance_events")
        .select("event_type,event_value,occurred_at")
        .eq("application_id", application["application_id"])
        .order("occurred_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )
    training = (
        client.table("agent_training_progress")
        .select("module_key,completed,completed_at")
        .eq("application_id", application["application_id"])
        .execute()
        .data
        or []
    )
    return {
        "profile": {
            "application_id": application["application_id"],
            "full_name": application["full_name"],
            "phone": application["phone"],
            "applicant_type": application["applicant_type"],
            "agent_tag": application.get("agent_tag"),
            "status": application.get("status"),
        },
        "territory": {
            "region": application["region"],
            "zone": application["zone"],
            "woreda": application["woreda"],
            "kebele": application["kebele"],
            "village": application["preferred_territory"],
        },
        "training": training,
        "performance_events": metrics,
    }


def update_agent_profile(telegram_user_id: str, updates: dict) -> dict:
    application = get_application_by_telegram_user(telegram_user_id)
    if not application:
        raise ValueError("Application not found")
    allowed_fields = {"phone", "work_type", "internal_remarks"}
    safe_updates = {key: value for key, value in updates.items() if key in allowed_fields and value is not None}
    if not safe_updates:
        return application
    client = get_supabase()
    result = (
        client.table("agent_applications")
        .update(safe_updates)
        .eq("application_id", application["application_id"])
        .execute()
    )
    return result.data[0]


def upsert_training_progress(application_id: str, module_key: str, completed: bool) -> dict:
    client = get_supabase()
    existing = (
        client.table("agent_training_progress")
        .select("progress_id")
        .eq("application_id", application_id)
        .eq("module_key", module_key)
        .limit(1)
        .execute()
    )
    payload = {
        "application_id": application_id,
        "module_key": module_key,
        "completed": completed,
        "completed_at": datetime.now(timezone.utc).isoformat() if completed else None,
    }
    if existing.data:
        progress_id = existing.data[0]["progress_id"]
        result = client.table("agent_training_progress").update(payload).eq("progress_id", progress_id).execute()
    else:
        result = client.table("agent_training_progress").insert(payload).execute()
    return result.data[0]


def get_rankings() -> dict:
    client = get_supabase()
    sales = (
        client.rpc("top_sales_agents", {"result_limit": 10}).execute().data
        if hasattr(client, "rpc")
        else []
    )
    installers = (
        client.rpc("top_installer_agents", {"result_limit": 10}).execute().data
        if hasattr(client, "rpc")
        else []
    )
    return {
        "top_sales_agents": sales or [],
        "top_installers": installers or [],
    }


def send_notification_email(application: dict) -> None:
    type_label = {
        "sales_only": "Sales Agent",
        "installer_only": "Installer Agent",
        "sales_installer": "Sales + Installer",
    }.get(application["applicant_type"], application["applicant_type"])

    subject = f"New Application – {type_label} – {application['full_name']} – {application['region']}"
    body = f"""Applicant Type: {type_label}
Full Name: {application['full_name']}
Phone: {application['phone']}
Region: {application['region']}
Zone: {application['zone']}
Woreda: {application['woreda']}
Kebele: {application['kebele']}
Town/Village: {application['village']}
Experience: {'Yes' if application['experience'] else 'No'}
Experience Years: {application['experience_years']}
Work Type: {application['work_type']}
Has Shop: {'Yes' if application['has_shop'] else 'No'}
Can Install Solar Systems: {'Yes' if application['can_install'] else 'No'}
Preferred Territory: {application['preferred_territory']}
ID Front File: {application['id_file_front_url']}
ID Back File: {application['id_file_back_url']}
Profile Photo: {application.get('profile_photo_url')}
Qualification Score: {application['qualification_score']}
Qualification Flag: {application['qualification_flag']}
Status: {application['status']}
"""

    msg = EmailMessage()
    msg["From"] = settings.smtp_from_email
    msg["To"] = settings.notification_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


def send_admin_telegram_alert(application: dict) -> None:
    if not settings.admin_telegram_chat_id:
        return

    text = (
        "🚨 New Agent Application\n"
        f"Name: {application['full_name']}\n"
        f"Type: {application['applicant_type']}\n"
        f"Region: {application['region']}\n"
        f"Territory: {application['preferred_territory']}\n"
        f"Score: {application['qualification_score']} ({application['qualification_flag']})"
    )

    base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    httpx.post(f"{base_url}/sendMessage", json={"chat_id": settings.admin_telegram_chat_id, "text": text}, timeout=20)

    uploads = [
        ("ID Front", application.get("id_file_front_url")),
        ("ID Back", application.get("id_file_back_url")),
        ("Profile Photo", application.get("profile_photo_url")),
    ]
    for label, file_url in uploads:
        if not file_url:
            continue
        cleaned = file_url.lower().split("?")[0]
        is_image = cleaned.endswith((".jpg", ".jpeg", ".png", ".webp"))
        if is_image:
            httpx.post(
                f"{base_url}/sendPhoto",
                json={"chat_id": settings.admin_telegram_chat_id, "photo": file_url, "caption": label},
                timeout=20,
            )
        else:
            httpx.post(
                f"{base_url}/sendMessage",
                json={"chat_id": settings.admin_telegram_chat_id, "text": f"{label}: {file_url}"},
                timeout=20,
            )
