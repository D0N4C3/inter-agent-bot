from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = "8760061567:AAHFLyHHLWcy75ngHTsqJt671TYvi32zi-Q"
    telegram_bot_username: str = "InterEthiopiaAgentBot"

    supabase_url: str = "https://onwgrdsknawpnjiegetj.supabase.co"
    supabase_key: str = "sb_publishable_zOngVTuXNSKSdO8YYpKkUA_NcMaV2e5"
    supabase_schema: str = "inter_agent_apply"
    supabase_storage_bucket: str = "inter-agent"

    smtp_host: str = "interethiopia.com"
    smtp_port: int = 587
    smtp_username: str = "agentapply@interethiopia.com"
    smtp_password: str = "Interes@2025!"
    smtp_from_email: str = "agentapply@interethiopia.com"
    notification_email: str = "agentapply@interethiopia.com"

    terms_text: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
