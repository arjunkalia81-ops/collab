from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    therapynotes_login_url: str = "https://www.therapynotes.com/app/login/"
    therapynotes_base_url: str = "https://www.therapynotes.com"

    # Chrome is only opened to pass Cloudflare; profile path is relative to worker/
    therapynotes_headless: bool = False
    browser_profile: str = "browser_profile"
    # Optional: connect to an already-running local Chrome (chrome.exe --remote-debugging-port=9222)
    chrome_cdp_url: str = ""
    # Optional: full path to chrome.exe; empty uses Playwright channel="chrome"
    chrome_executable: str = ""
    clearance_min_interval_seconds: int = 60
    # true: Chrome stays open on the login page (watch it over VNC) and is reused for Cloudflare
    keep_browser_open: bool = False

    tn_session_ping_seconds: int = 120
    tn_idle_minutes: int = 60
    tn_max_sessions: int = 50

    worker_secret: str = ""
    worker_host: str = "127.0.0.1"
    worker_port: int = 8002

    model_config = SettingsConfigDict(
        env_file=str(_ENV),
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
