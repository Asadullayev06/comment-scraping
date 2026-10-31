"""Application configuration loaded from environment variables."""

from typing import List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core Security & Access
    ADMIN_TELEGRAM_IDS: List[int] = Field(default_factory=list)
    SESSION_ENCRYPTION_KEY: str = Field(default="v6S2q5P2sZzL3gU0G_3u8B3_mG0gq7L8YkL7n0X4R-Y=")

    # Telegram Bot (Control & Alert Plane - Aiogram 3)
    TELEGRAM_BOT_TOKEN: str = Field(default="")

    # Telegram MTProto Userbot (Monitoring & History Scanner - Telethon)
    TELEGRAM_API_ID: int = Field(default=0)
    TELEGRAM_API_HASH: str = Field(default="")
    TELEGRAM_SESSION_NAME: str = Field(default="monitor_userbot")
    TELEGRAM_SESSION_STRING: Optional[str] = Field(default=None)

    # DeepSeek LLM Configuration
    DEEPSEEK_API_KEY: str = Field(default="")
    DEEPSEEK_BASE_URL: str = Field(default="https://api.deepseek.com")
    DEEPSEEK_MODEL: str = Field(default="deepseek-chat")
    LLM_BATCH_SIZE: int = Field(default=40)
    LLM_BATCH_FLUSH_SECONDS: float = Field(default=4.0)
    LLM_ALERT_SEVERITY_THRESHOLD: int = Field(default=7)

    # YouTube Official API (Optional)
    YOUTUBE_API_KEY: Optional[str] = Field(default=None)

    # X (Twitter) Official API v2 Bearer Token (Optional alternative to burner cookies)
    X_BEARER_TOKEN: Optional[str] = Field(default=None)

    # Database & Redis
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://operatives:strongpassword@postgres:5432/operatives_db"
    )
    REDIS_URL: str = Field(default="redis://redis:6379/0")

    # Platform Dynamic Query Hashes / Doc IDs (Configurable without redeployment)
    X_TWEET_DETAIL_QUERY_ID: str = Field(default="V94IR8nUKvgjwjd8uKnogg")
    FB_COMET_DOC_ID: str = Field(default="5959580457497184")

    # Scraper & Worker Settings
    SCRAPER_CONCURRENCY: int = Field(default=5)
    SCRAPER_HEADLESS: bool = Field(default=True)

    # Internal Hermes Bridge API Settings
    OPS_API_HOST: str = Field(default="0.0.0.0")
    OPS_API_PORT: int = Field(default=18890)
    OPS_API_KEY: Optional[str] = Field(default=None)

    @field_validator("ADMIN_TELEGRAM_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, (list, tuple, set)):
            return [int(x) for x in v]
        if isinstance(v, str):
            clean = v.strip().strip("[]()")
            if not clean:
                return []
            return [int(x.strip().strip("'\"")) for x in clean.split(",") if x.strip()]
        try:
            return [int(v)]
        except Exception:
            return []

    @field_validator("TELEGRAM_API_ID", mode="before")
    @classmethod
    def parse_api_id(cls, v):
        if v is None or v == "" or (isinstance(v, str) and not v.strip()):
            return 0
        try:
            return int(v)
        except Exception:
            return 0

    def is_admin(self, user_id: int) -> bool:
        """Check if a Telegram user ID is authorized as an administrator."""
        if not self.ADMIN_TELEGRAM_IDS:
            return True  # If no admin configured, allow initial setup
        return user_id in self.ADMIN_TELEGRAM_IDS


settings = Settings()
