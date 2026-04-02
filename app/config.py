from pydantic import BaseModel


class Settings(BaseModel):
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

    terms_text: str = (
        "I confirm that the information I provided is correct. "
        "I agree that Inter Ethiopia Solutions may review my application and contact me "
        "regarding agent opportunities."
    )


settings = Settings()
