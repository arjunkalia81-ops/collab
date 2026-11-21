"""Cloudflare clearance shared by every HTTP session: cf_* cookies + the Chrome User-Agent they were issued to."""

import asyncio
import json
import logging
import time
from pathlib import Path

from worker_app.core.config import settings
from worker_app.playwright.browser import close_browser, open_browser

FILE = Path(__file__).resolve().parents[2] / "clearance.json"
COOKIE_WAIT_MS = 30_000
# Used until Chrome has been opened once; Cloudflare only needs it to match the cf_clearance it issued.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)

log = logging.getLogger(__name__)


class CloudflareBlocked(Exception):
    """Chrome could not get past the Cloudflare challenge."""


def is_cloudflare(url: str, title: str = "") -> bool:
    u = url.lower()
    t = title.lower()
    return "cf_chl" in u or "cdn-cgi/challenge" in u or "just a moment" in t or "attention required" in t


async def wait_until_past_cloudflare(page, timeout_ms: int = 90_000) -> bool:
    """Returns True if Chrome had to pass a challenge, False if the page loaded straight away."""
    elapsed = 0
    while elapsed < timeout_ms:
        try:
            title = await page.title()
        except Exception:
            title = ""
        if not is_cloudflare(page.url, title):
            return elapsed > 0
        log.info("Cloudflare challenge, waiting: %s", page.url[:80])
        await page.wait_for_timeout(2000)
        elapsed += 2000
    raise CloudflareBlocked("Cloudflare challenge did not clear in Chrome; retry later")


async def cf_cookies(context) -> dict[str, str]:
    cookies = await context.cookies(settings.therapynotes_base_url)
    return {c["name"]: c["value"] for c in cookies if c["name"].startswith(("cf_", "__cf"))}


class Clearance:
    def __init__(self):
        self.cookies: dict[str, str] = {}
        self.user_agent = DEFAULT_USER_AGENT
        self.created_at = 0.0
        self.lock = asyncio.Lock()
        self.browser = None

    async def open_page(self):
        if not settings.keep_browser_open:
            return await open_browser()
        if self.browser and not self.browser[2].is_closed():
            return self.browser
        await self.close()
        self.browser = await open_browser()
        return self.browser

    async def release(self, playwright, context) -> None:
        if not settings.keep_browser_open:
            await close_browser(playwright, context)

    async def show(self) -> None:
        async with self.lock:
            _, _, page = await self.open_page()
            await page.goto(settings.therapynotes_login_url, wait_until="domcontentloaded")

    async def close(self) -> None:
        if self.browser:
            playwright, context, _ = self.browser
            self.browser = None
            await close_browser(playwright, context)

    @property
    def ok(self) -> bool:
        return bool(self.cookies.get("cf_clearance") and self.user_agent)

    def load(self) -> None:
        if not FILE.exists():
            return
        data = json.loads(FILE.read_text(encoding="utf-8"))
        self.cookies = data.get("cookies") or {}
        self.user_agent = data.get("user_agent") or DEFAULT_USER_AGENT
        self.created_at = data.get("created_at") or 0.0

    def save(self) -> None:
        FILE.write_text(
            json.dumps({"cookies": self.cookies, "user_agent": self.user_agent, "created_at": self.created_at}, indent=2),
            encoding="utf-8",
        )

    async def refresh(self, force: bool = False, wait_ms: int = 90_000) -> None:
        """Let Chrome pass Cloudflare and keep its cookies + UA; Chrome closes unless KEEP_BROWSER_OPEN."""
        async with self.lock:
            recent = time.time() - self.created_at < settings.clearance_min_interval_seconds
            if self.ok and recent and not force:
                return
            log.info("Refreshing Cloudflare clearance with Chrome")
            playwright, context, page = await self.open_page()
            try:
                await page.goto(settings.therapynotes_login_url, wait_until="domcontentloaded")
                challenged = await wait_until_past_cloudflare(page, wait_ms)
                cookies = await cf_cookies(context)
                waited = 0
                while challenged and "cf_clearance" not in cookies and waited < COOKIE_WAIT_MS:
                    await page.wait_for_timeout(1000)
                    waited += 1000
                    cookies = await cf_cookies(context)
                if challenged and "cf_clearance" not in cookies:
                    raise CloudflareBlocked("Chrome loaded TherapyNotes but Cloudflare never set cf_clearance")
                self.cookies = cookies
                self.user_agent = await page.evaluate("navigator.userAgent")
                self.created_at = time.time()
            finally:
                await self.release(playwright, context)
            self.save()
            log.info("Cloudflare clearance saved")


clearance = Clearance()
