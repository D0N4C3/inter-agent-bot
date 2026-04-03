import smtplib
import uuid
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
    }

    if lookup.data:
        territory_id = lookup.data[0]["territory_id"]
        client.table("territories").update(territory_payload).eq("territory_id", territory_id).execute()
    else:
        client.table("territories").insert(territory_payload).execute()


def update_application_status(application_id: str, status: str, admin_notes: str | None = None, territory_village: str | None = None) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError("Invalid status")

    application = get_application(application_id)
    if not application:
        raise ValueError("Application not found")

    updates: dict = {"status": status}
    if admin_notes is not None:
        updates["admin_notes"] = admin_notes

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

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    httpx.post(url, json={"chat_id": settings.admin_telegram_chat_id, "text": text}, timeout=20)
