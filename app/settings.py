from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_PRE_PING: bool = True

    CLEANUP_INTERVAL_SECONDS: int = 60

    MESSAGE_TTL_SECONDS: int = 60
    CHANNEL_IDLE_TIMEOUT_SECONDS: int = 300
    WS_AUTH_TIMEOUT_SECONDS: int = 5
    CHANNEL_CLEANUP_INTERVAL_SECONDS: int = 15


settings = Settings()
