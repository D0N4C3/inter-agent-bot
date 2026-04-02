from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
    )
    telegram_bot_username: str = "InterEthiopiaAgentBot"

    supabase_url: str = Field(
        default="",
        validation_alias=AliasChoices("SUPABASE_URL", "SUPABASE_PROJECT_URL"),
    )
    supabase_key: str = Field(
        default="",
        validation_alias=AliasChoices("SUPABASE_KEY", "SUPABASE_API_KEY", "SUPABASE_ANON_KEY"),
    )
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

    @field_validator("telegram_bot_token", "supabase_url", "supabase_key", mode="before")
    @classmethod
    def clean_env_value(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip().strip('"').strip("'")
        return value


settings = Settings()
