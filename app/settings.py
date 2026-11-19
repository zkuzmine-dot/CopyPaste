from pathlib import Path

from pydantic import Field, SecretStr
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
    WS_MAX_MESSAGE_BYTES: int = 1_048_576
    WS_RATE_LIMIT_PER_SECOND: float = 2
    WS_RATE_LIMIT_BURST: int = 10

    FILES_DIR: Path = Path("/data/files")
    # Без значения по умолчанию: подписывать ссылки общеизвестным ключом хуже, чем не стартовать
    FILES_SIGNING_SECRET: SecretStr = Field(min_length=32)
    FILE_MAX_SIZE_BYTES: int = 52_428_800
    NOTE_MAX_FILES: int = 10
    FILES_MAX_TOTAL_BYTES: int = 5_368_709_120
    CHANNEL_FILE_TTL_SECONDS: int = 300
    BURN_NOTE_FILES_WINDOW_SECONDS: int = 600
    PENDING_UPLOAD_TTL_SECONDS: int = 3600
    FILE_URL_TTL_SECONDS: int = 3600
    UPLOAD_RATE_LIMIT_PER_MINUTE: float = 10
    UPLOAD_RATE_LIMIT_BURST: int = 10
    FILES_GC_INTERVAL_SECONDS: int = 60
    FILES_DISK_SWEEP_INTERVAL_SECONDS: int = 3600

    REDIS_URL: str = "redis://redis:6379/0"
    NOTE_PASSWORD_MIN_LENGTH: int = 4
    NOTE_PASSWORD_MAX_LENGTH: int = 128
    KDF_TIME_COST: int = 3
    KDF_MEMORY_KIB: int = 65_536
    KDF_PARALLELISM: int = 4
    KDF_MAX_CONCURRENT: int = 4
    UNLOCK_IP_MAX_FAILURES: int = 10
    UNLOCK_IP_WINDOW_SECONDS: int = 900
    UNLOCK_IP_BAN_SECONDS: int = 900
    UNLOCK_NOTE_MAX_FAILURES: int = 5
    UNLOCK_NOTE_WINDOW_SECONDS: int = 60
    UNLOCK_NOTE_LOCK_SECONDS: int = 60
    PROTECTED_FILE_URL_TTL_SECONDS: int = 600
    FILE_KEY_CACHE_SECONDS: int = 600
    FILE_ENCRYPTION_CHUNK_BYTES: int = 65_536


settings = Settings()
