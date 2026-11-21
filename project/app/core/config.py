from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    jwt_secret: str
    jwt_access_minutes: int = 5
    jwt_refresh_days: int = 7

    worker_url: str = "http://127.0.0.1:8002"
    worker_secret: str = ""

    # Comma-separated frontend origins, e.g. "http://localhost:3000,https://app.example.com"
    cors_origins: str = "http://localhost:3000,http://localhost:5173,http://192.168.11.27:3000"

    host: str = "0.0.0.0"
    port: int = 8001
    reload: bool = False
    model_config = SettingsConfigDict(
        env_file=str(_ENV),
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
