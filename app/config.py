from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_bot_username: str

    supabase_url: str
    supabase_key: str
    supabase_schema: str = "inter_agent_apply"
    supabase_storage_bucket: str = "inter-agent"

    smtp_host: str
    smtp_port: int = 587
    smtp_username: str
    smtp_password: str
    smtp_from_email: str
    notification_email: str

    admin_telegram_chat_id: str | None = None
    admin_dashboard_token: str | None = None
    flask_secret_key: str | None = None

    terms_text: str = (
        "I confirm that the information I provided is correct. "
        "I agree that Inter Ethiopia Solutions may review my application and contact me "
        "regarding agent opportunities."
    )
    expected_review_timeline: str = "Our team typically responds within 3-5 business days."
    max_upload_size_mb: int = 8
    training_pdf_url: str = "https://example.com/training/solar-installation-guide.pdf"
    training_video_url: str = "https://example.com/training/solar-installation-video"
    sales_playbook_url: str = "https://example.com/training/sales-playbook.pdf"
    mini_app_name: str = "Inter Agent Platform"
    mini_app_primary_color: str = "#165dff"
    google_maps_sdk_key: str | None = None
    territory_suggestion_limit: int = 5
    bot_session_backend: str = "memory"
    bot_session_ttl_minutes: int = 90
    bot_session_sqlite_path: str = "data/bot_sessions.sqlite3"

    @field_validator(
        "telegram_bot_token",
        "telegram_bot_username",
        "supabase_url",
        "supabase_key",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from_email",
        "notification_email",
        mode="before",
    )
    @classmethod
    def clean_string_value(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().strip('"').strip("'")


settings = Settings()
