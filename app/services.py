import smtplib
import sqlite3
import json
import threading
from math import asin, cos, radians, sin, sqrt
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

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

VALID_TERRITORY_AVAILABILITY = {
    "open",
    "assigned",
    "blocked",
}

VALID_UI_LANGUAGES = {"en", "am", "om", "ti"}
_BOT_SESSION_MEMORY_STORE: dict[str, dict] = {}
_BOT_SESSION_LOCK = threading.Lock()
_BOT_SESSION_DB_READY = False


def get_supabase() -> Client:
    client = create_client(
        settings.supabase_url,
        settings.supabase_key,
        options=ClientOptions(schema=settings.supabase_schema),
    )
    return client


def _bot_session_backend() -> str:
    return (settings.bot_session_backend or "memory").strip().lower()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _session_expiry_iso() -> str:
    expiry = _utc_now() + timedelta(minutes=max(settings.bot_session_ttl_minutes, 1))
    return expiry.isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _is_expired(expires_at: str | None) -> bool:
    expiry = _parse_iso_datetime(expires_at)
    if not expiry:
        return False
    return expiry <= _utc_now()


def _sqlite_session_db_path() -> Path:
    return Path(settings.bot_session_sqlite_path)


def _ensure_sqlite_session_db() -> None:
    global _BOT_SESSION_DB_READY
    if _BOT_SESSION_DB_READY:
        return
    db_path = _sqlite_session_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_sessions_local (
                telegram_user_id TEXT PRIMARY KEY,
                session_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_sessions_local_expires ON bot_sessions_local(expires_at)")
        conn.commit()
    _BOT_SESSION_DB_READY = True


def list_app_settings() -> list[dict]:
    client = get_supabase()
    result = client.table("app_settings").select("*").order("setting_key").execute()
    return result.data or []


def get_app_setting(setting_key: str, default: str | None = None) -> str | None:
    client = get_supabase()
    result = (
        client.table("app_settings")
        .select("setting_value")
        .eq("setting_key", setting_key)
        .limit(1)
        .execute()
    )
    if result.data:
        value = result.data[0].get("setting_value")
        return str(value) if value is not None else default
    return default


def upsert_app_setting(setting_key: str, setting_value: str, updated_by: str | None = None) -> dict:
    client = get_supabase()
    existing = (
        client.table("app_settings")
        .select("setting_id")
        .eq("setting_key", setting_key)
        .limit(1)
        .execute()
    )
    payload: dict = {
        "setting_key": setting_key.strip(),
        "setting_value": setting_value.strip(),
        "updated_by": updated_by,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing.data:
        setting_id = existing.data[0]["setting_id"]
        result = client.table("app_settings").update(payload).eq("setting_id", setting_id).execute()
    else:
        result = client.table("app_settings").insert(payload).execute()
    return result.data[0]


def get_training_links(language: str | None = None) -> dict[str, str]:
    normalized_lang = (language or "en").strip().lower()
    if normalized_lang not in {"en", "am", "om", "ti"}:
        normalized_lang = "en"

    def resolve_link(base_setting_key: str, fallback: str) -> str:
        localized_key = f"{base_setting_key}_{normalized_lang}"
        return (
            get_app_setting(localized_key)
            or get_app_setting(base_setting_key, fallback)
            or fallback
        )

    return {
        "pdf": resolve_link("training_pdf_url", settings.training_pdf_url),
        "video": resolve_link("training_video_url", settings.training_video_url),
        "sales_playbook": resolve_link("sales_playbook_url", settings.sales_playbook_url),
    }


def list_woreda_regions(region: str | None = None, zone: str | None = None) -> list[dict]:
    client = get_supabase()
    query = client.table("woreda_regions").select("sl_no,woreda,zone,region,latitude,longitude").order("sl_no")
    if region:
        query = query.eq("region", region)
    if zone:
        query = query.eq("zone", zone)
    return query.limit(5000).execute().data or []


def list_location_options() -> dict[str, list]:
    rows = list_woreda_regions()
    regions = sorted({(row.get("region") or "").strip() for row in rows if row.get("region")})
    zones = sorted({(row.get("zone") or "").strip() for row in rows if row.get("zone")})
    woredas = sorted({(row.get("woreda") or "").strip() for row in rows if row.get("woreda")})
    return {
        "regions": regions,
        "zones": zones,
        "woredas": woredas,
        "rows": rows,
    }


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


def get_bot_session(telegram_user_id: str) -> dict | None:
    backend = _bot_session_backend()
    if backend == "memory":
        with _BOT_SESSION_LOCK:
            row = _BOT_SESSION_MEMORY_STORE.get(telegram_user_id)
            if not row:
                return None
            if _is_expired(row.get("expires_at")):
                _BOT_SESSION_MEMORY_STORE.pop(telegram_user_id, None)
                return None
            return dict(row.get("session_data") or {})

    if backend == "sqlite":
        _ensure_sqlite_session_db()
        with sqlite3.connect(_sqlite_session_db_path()) as conn:
            cursor = conn.execute(
                "SELECT session_json, expires_at FROM bot_sessions_local WHERE telegram_user_id = ? LIMIT 1",
                (telegram_user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            session_json, expires_at = row
            if _is_expired(expires_at):
                conn.execute("DELETE FROM bot_sessions_local WHERE telegram_user_id = ?", (telegram_user_id,))
                conn.commit()
                return None
            return json.loads(session_json)

    client = get_supabase()
    result = (
        client.table("bot_sessions")
        .select("session_data")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0].get("session_data") or {}
    return None


def upsert_bot_session(telegram_user_id: str, session_data: dict) -> dict:
    backend = _bot_session_backend()
    base_payload = {
        "telegram_user_id": telegram_user_id,
        "session_data": session_data,
        "updated_at": _utc_now().isoformat(),
    }
    if backend == "memory":
        payload = {**base_payload, "expires_at": _session_expiry_iso()}
        with _BOT_SESSION_LOCK:
            _BOT_SESSION_MEMORY_STORE[telegram_user_id] = payload
        return payload

    if backend == "sqlite":
        payload = {**base_payload, "expires_at": _session_expiry_iso()}
        _ensure_sqlite_session_db()
        with sqlite3.connect(_sqlite_session_db_path()) as conn:
            conn.execute(
                """
                INSERT INTO bot_sessions_local (telegram_user_id, session_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    session_json=excluded.session_json,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    telegram_user_id,
                    json.dumps(session_data),
                    payload["expires_at"],
                    payload["updated_at"],
                ),
            )
            conn.execute("DELETE FROM bot_sessions_local WHERE expires_at <= ?", (_utc_now().isoformat(),))
            conn.commit()
        return payload

    client = get_supabase()
    result = client.table("bot_sessions").upsert(base_payload, on_conflict="telegram_user_id").execute()
    return (result.data or [base_payload])[0]


def delete_bot_session(telegram_user_id: str) -> None:
    backend = _bot_session_backend()
    if backend == "memory":
        with _BOT_SESSION_LOCK:
            _BOT_SESSION_MEMORY_STORE.pop(telegram_user_id, None)
        return

    if backend == "sqlite":
        _ensure_sqlite_session_db()
        with sqlite3.connect(_sqlite_session_db_path()) as conn:
            conn.execute("DELETE FROM bot_sessions_local WHERE telegram_user_id = ?", (telegram_user_id,))
            conn.commit()
        return

    client = get_supabase()
    client.table("bot_sessions").delete().eq("telegram_user_id", telegram_user_id).execute()


def list_open_territories(region: str | None = None, zone: str | None = None, woreda: str | None = None) -> list[dict]:
    client = get_supabase()
    query = client.table("territories").select("region,zone,woreda,village").eq("is_locked", False)
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
    occupied_only: bool = False,
) -> list[dict]:
    client = get_supabase()
    query = client.table("territories").select(
        "territory_id,region,zone,woreda,village,latitude,longitude,is_locked,availability_status,assigned_application_id"
    )
    if occupied_only:
        query = query.or_("is_locked.eq.true,assigned_application_id.not.is.null")
    if region:
        query = query.eq("region", region)
    if zone:
        query = query.eq("zone", zone)
    if woreda:
        query = query.eq("woreda", woreda)
    rows = query.limit(500).execute().data or []
    woreda_region_rows = list_woreda_regions(region=region, zone=zone)
    woreda_coordinates = {
        (
            (item.get("region") or "").strip().lower(),
            (item.get("zone") or "").strip().lower(),
            (item.get("woreda") or "").strip().lower(),
        ): (item.get("latitude"), item.get("longitude"))
        for item in woreda_region_rows
    }
    assigned_ids = [row.get("assigned_application_id") for row in rows if row.get("assigned_application_id")]
    assigned_lookup: dict[str, dict] = {}
    if assigned_ids:
        applications = (
            client.table("agent_applications")
            .select("application_id,full_name,status")
            .in_("application_id", assigned_ids)
            .execute()
            .data
            or []
        )
        assigned_lookup = {item["application_id"]: item for item in applications}

    approved_agents = (
        client.table("agent_applications")
        .select("full_name,region,zone,woreda,preferred_territory,status")
        .eq("status", "Approved")
        .limit(1000)
        .execute()
        .data
        or []
    )

    for row in rows:
        key = (
            (row.get("region") or "").strip().lower(),
            (row.get("zone") or "").strip().lower(),
            (row.get("woreda") or "").strip().lower(),
        )
        woreda_lat_lon = woreda_coordinates.get(key)
        if woreda_lat_lon and woreda_lat_lon[0] is not None and woreda_lat_lon[1] is not None:
            row["latitude"] = woreda_lat_lon[0]
            row["longitude"] = woreda_lat_lon[1]

        assigned_id = row.get("assigned_application_id")
        assigned_app = assigned_lookup.get(assigned_id) if assigned_id else None
        fallback_app = None
        if not assigned_app:
            fallback_matches = [
                app
                for app in approved_agents
                if app.get("region") == row.get("region")
                and app.get("zone") == row.get("zone")
                and app.get("woreda") == row.get("woreda")
                and (
                    (app.get("preferred_territory") and app.get("preferred_territory") == row.get("village"))
                    or not app.get("preferred_territory")
                )
            ]
            if fallback_matches:
                fallback_app = fallback_matches[0]
        display_app = assigned_app or fallback_app
        row["has_agent"] = bool(display_app)
        row["assigned_agent_name"] = display_app.get("full_name") if display_app else None
    return rows


def list_territories_admin(
    region: str | None = None,
    zone: str | None = None,
    woreda: str | None = None,
    include_locked: bool = True,
    limit: int = 300,
) -> list[dict]:
    client = get_supabase()
    query = client.table("territories").select("*").order("region").order("zone").order("woreda").order("village")
    if region:
        query = query.eq("region", region)
    if zone:
        query = query.eq("zone", zone)
    if woreda:
        query = query.eq("woreda", woreda)
    if not include_locked:
        query = query.eq("is_locked", False)
    return query.limit(max(1, min(limit, 2000))).execute().data or []


def create_territory(
    region: str,
    zone: str,
    woreda: str,
    kebele: str,
    village: str,
    latitude: float | None = None,
    longitude: float | None = None,
    availability_status: str = "open",
    is_locked: bool = False,
) -> dict:
    if availability_status not in VALID_TERRITORY_AVAILABILITY:
        raise ValueError("Invalid territory availability_status")
    client = get_supabase()
    payload = {
        "region": region.strip(),
        "zone": zone.strip(),
        "woreda": woreda.strip(),
        "village": village.strip(),
        "latitude": latitude,
        "longitude": longitude,
        "availability_status": availability_status,
        "is_locked": bool(is_locked),
    }
    result = client.table("territories").insert(payload).execute()
    return result.data[0]


def update_territory(territory_id: str, updates: dict) -> dict:
    safe_updates = {}
    for key in ("region", "zone", "woreda", "village"):
        if key in updates and updates[key] is not None:
            safe_updates[key] = str(updates[key]).strip()
    for key in ("latitude", "longitude", "assigned_application_id"):
        if key in updates:
            safe_updates[key] = updates[key]
    if "is_locked" in updates:
        safe_updates["is_locked"] = bool(updates["is_locked"])
    if "availability_status" in updates and updates["availability_status"] is not None:
        status = str(updates["availability_status"]).strip()
        if status not in VALID_TERRITORY_AVAILABILITY:
            raise ValueError("Invalid territory availability_status")
        safe_updates["availability_status"] = status
    if not safe_updates:
        raise ValueError("No valid territory updates")
    client = get_supabase()
    result = client.table("territories").update(safe_updates).eq("territory_id", territory_id).execute()
    if not result.data:
        raise ValueError("Territory not found")
    return result.data[0]


def delete_territory(territory_id: str) -> None:
    client = get_supabase()
    client.table("territories").delete().eq("territory_id", territory_id).execute()


def list_bot_admins(limit: int = 250) -> list[dict]:
    client = get_supabase()
    result = client.table("bot_admins").select("*").order("created_at", desc=True).limit(max(1, min(limit, 1000))).execute()
    return result.data or []


def remove_bot_admin(telegram_user_id: str) -> None:
    client = get_supabase()
    client.table("bot_admins").delete().eq("telegram_user_id", telegram_user_id).execute()


def list_performance_events(application_id: str | None = None, limit: int = 300) -> list[dict]:
    client = get_supabase()
    query = client.table("agent_performance_events").select("*").order("occurred_at", desc=True)
    if application_id:
        query = query.eq("application_id", application_id)
    result = query.limit(max(1, min(limit, 2000))).execute()
    return result.data or []


def delete_performance_event(event_id: str) -> None:
    client = get_supabase()
    client.table("agent_performance_events").delete().eq("event_id", event_id).execute()


def list_training_progress(application_id: str | None = None, limit: int = 300) -> list[dict]:
    client = get_supabase()
    query = client.table("agent_training_progress").select("*").order("updated_at", desc=True)
    if application_id:
        query = query.eq("application_id", application_id)
    result = query.limit(max(1, min(limit, 2000))).execute()
    return result.data or []


def delete_training_progress(progress_id: str) -> None:
    client = get_supabase()
    client.table("agent_training_progress").delete().eq("progress_id", progress_id).execute()


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
        .eq("village", village)
        .limit(1)
        .execute()
    )

    territory_payload = {
        "region": region,
        "zone": zone,
        "woreda": woreda,
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
            "work_type": application.get("work_type"),
            "has_shop": application.get("has_shop"),
            "business_name": application.get("business_name"),
            "business_type": application.get("business_type"),
            "business_years": application.get("business_years"),
            "business_customers_weekly": application.get("business_customers_weekly"),
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


def get_training_modules_for_agent(status: str | None, applicant_type: str | None) -> list[dict]:
    normalized_type = (applicant_type or "").strip()
    if (status or "").lower() != "approved":
        return []

    common = [
        {
            "module_key": "company_intro",
            "title": "Inter Ethiopia Orientation",
            "description": "How our agent program works, standards, and support channels.",
            "audience": "all",
        },
        {
            "module_key": "code_of_conduct",
            "title": "Code of Conduct & Safety",
            "description": "Safety basics, customer etiquette, and reporting procedures.",
            "audience": "all",
        },
    ]
    sales = [
        {
            "module_key": "sales_foundation",
            "title": "Sales Fundamentals",
            "description": "Lead qualification, objection handling, and closing workflow.",
            "audience": "sales",
        }
    ]
    installer = [
        {
            "module_key": "installer_foundation",
            "title": "Installation Fundamentals",
            "description": "Site checklist, installation flow, and quality control.",
            "audience": "installer",
        }
    ]
    if normalized_type == "sales_only":
        return common + sales
    if normalized_type == "installer_only":
        return common + installer
    return common + sales + installer


def update_agent_profile(telegram_user_id: str, updates: dict) -> dict:
    application = get_application_by_telegram_user(telegram_user_id)
    if not application:
        raise ValueError("Application not found")
    allowed_fields = {
        "full_name",
        "phone",
        "work_type",
        "internal_remarks",
        "region",
        "zone",
        "woreda",
        "preferred_territory",
    }
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
    def sanitize(items: list[dict] | None) -> list[dict]:
        safe_rows: list[dict] = []
        for row in items or []:
            safe = dict(row)
            safe.pop("phone", None)
            safe_rows.append(safe)
        return safe_rows

    return {
        "top_sales_agents": sanitize(sales),
        "top_installers": sanitize(installers),
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
Business Name: {application.get('business_name')}
Business Type: {application.get('business_type')}
Business Age (Years): {application.get('business_years')}
Average Weekly Customers: {application.get('business_customers_weekly')}
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
