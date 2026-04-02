import smtplib
from email.message import EmailMessage

from supabase import Client, create_client

from app.config import settings


def get_supabase() -> Client:
    client = create_client(settings.supabase_url, settings.supabase_key)
    client.schema(settings.supabase_schema)
    return client


def upload_telegram_file(
    file_bytes: bytes,
    folder: str,
    filename: str,
    content_type: str,
    upsert: bool = False,
) -> str:
    client = get_supabase()
    path = f"{folder}/{filename}"
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


def territory_is_available(preferred_territory: str) -> bool:
    client = get_supabase()
    result = (
        client.table("territories")
        .select("is_locked")
        .eq("village", preferred_territory)
        .limit(1)
        .execute()
    )
    if not result.data:
        return True
    return not bool(result.data[0]["is_locked"])


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
