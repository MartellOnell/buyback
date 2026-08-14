from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "local"
    COMMON_API_KEY: str = "changeme"

    DATABASE_URL: str = ""

    POSTGRES_USER: str = "buyback"
    POSTGRES_PASSWORD: str = "buyback"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "buyback"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.DATABASE_URL:
            object.__setattr__(
                self,
                "DATABASE_URL",
                f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}",
            )

    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://opencode.ai/zen/go/v1"
    LLM_MODEL: str = "deepseek-v4-flash"
    LLM_MAX_RETRIES: int = 5
    LLM_RETRY_BASE_DELAY: float = 2.0

    ORCHESTRATOR_INTERVAL_DAYS: int = 14
    STATUS_POLLER_INTERVAL_MINUTES: int = 60

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""


settings = Settings()
