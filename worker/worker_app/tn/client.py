"""One TherapyNotes user session over plain HTTP. Chrome is never involved here."""

import hashlib
import logging
import re
import time
import unicodedata
import uuid
from urllib.parse import urlparse

import httpx

from worker_app.core.config import settings
from worker_app.tn.clearance import CloudflareBlocked, clearance

log = logging.getLogger(__name__)

LOGIN_PATH = "/app/session/processlogin.aspx"
RELOGIN_LIMIT = 3
RELOGIN_WINDOW_SECONDS = 600

INVALID_PRACTICE_CODE = "invalid_practice_code"
INVALID_CREDENTIALS = "invalid_credentials"
TOO_MANY_ATTEMPTS = "too_many_attempts"
TWO_FACTOR_REQUIRED = "two_factor_required"
LOGIN_FAILED = "login_failed"
ERROR_CODES = {1: INVALID_PRACTICE_CODE, 2: INVALID_CREDENTIALS}


class TherapyNotesError(Exception):
    pass


class SessionExpired(Exception):
    pass


class ValidationFailed(Exception):
    """TherapyNotes refused a save; messages are TN's own form errors."""

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages) or "TherapyNotes rejected the change")
        self.messages = messages


class LoginFailed(Exception):
    """TherapyNotes rejected the login; message is TN's own text when available."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def password_hash(password: str) -> str:
    # Same as TN.Common.Security.PasswordHasher in the login page.
    return hashlib.sha512(unicodedata.normalize("NFC", password).encode()).hexdigest().upper()


def cloudflare_blocked(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    return bool(response.headers.get("cf-mitigated")) or "Unusual Activity" in response.text


def find(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


class TnSession:
    def __init__(
        self,
        session_id: str,
        practice_code: str,
        username: str,
        password_hash: str,
        practice_name: str = "",
        user_id: int | None = None,
        tnrac: str = "",
        tnv: str = "",
        created_at: float | None = None,
        last_used: float | None = None,
    ):
        self.session_id = session_id
        self.practice_code = practice_code
        self.username = username
        self.password_hash = password_hash
        self.practice_name = practice_name
        self.user_id = user_id
        self.tnrac = tnrac
        self.tnv = tnv
        self.created_at = created_at or time.time()
        self.last_used = last_used or time.time()
        self.relogins: list[float] = []
        self.client: httpx.AsyncClient | None = None
        self.on_login = None
        self.cf_bm = ""

    @property
    def base(self) -> str:
        return settings.therapynotes_base_url.rstrip("/")

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def host(self) -> str:
        return urlparse(self.base).hostname or ""

    def set_cookies(self, values: dict) -> None:
        # Pin the domain so TN's own Set-Cookie replaces ours instead of adding a duplicate.
        # Cloudflare sets its cookies on the parent domain (Domain=therapynotes.com).
        cf_domain = "." + self.host.removeprefix("www.")
        for name, value in values.items():
            domain = cf_domain if name.startswith(("__cf", "cf_")) else self.host
            self.client.cookies.set(name, value, domain=domain)

    async def open_client(self, cookies: dict | None = None) -> None:
        await self.close()
        self.client = httpx.AsyncClient(
            base_url=self.base,
            headers={"User-Agent": clearance.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
        )
        self.set_cookies({
            **clearance.cookies,
            "cookie-detection": "1",
            "timezone-offset": str(-time.timezone // 60),
            "practicecode": self.practice_code,
            **(cookies or {}),
        })

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

    def apply_clearance(self) -> None:
        self.client.headers["User-Agent"] = clearance.user_agent
        self.set_cookies(clearance.cookies)

    def note_cf_bm(self) -> None:
        cookies = [c for c in self.client.cookies.jar if c.name == "__cf_bm"]
        if not cookies:
            return
        cookie = max(cookies, key=lambda c: c.expires or 0)
        if cookie.value != self.cf_bm:
            self.cf_bm = cookie.value
            expires = time.strftime("%H:%M:%S UTC", time.gmtime(cookie.expires)) if cookie.expires else "session"
            log.info("Cloudflare __cf_bm for %s: %s... (expires %s)", self.username, cookie.value[:12], expires)

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Raw request; on a Cloudflare block refresh the clearance with Chrome and retry once."""
        response = await self.client.request(method, path, **kwargs)
        self.note_cf_bm()
        if not cloudflare_blocked(response):
            return response
        log.warning("Cloudflare blocked %s for %s, refreshing clearance", path, self.username)
        await clearance.refresh()
        self.apply_clearance()
        response = await self.client.request(method, path, **kwargs)
        if cloudflare_blocked(response):
            raise TherapyNotesError("Cloudflare is blocking TherapyNotes requests from this machine")
        return response

    # ---- login -------------------------------------------------------------

    async def login(self) -> None:
        await self.open_client()
        if not self.tnv:
            page = await self.request("GET", f"/app/login/{self.practice_code}/")
            self.tnv = find(r'siteVersion\s*=\s*"([^"]+)"', page.text)

        practice = await self.login_post({"practiceCode": self.practice_code, "doCookieCheck": "true"})
        self.check_login(practice)
        self.practice_name = practice.get("PracticeName") or self.practice_name

        user = await self.login_post({
            "username": self.username,
            "password": self.password_hash,
            "e-username": "",
            "agreetos": "false",
            "docookiecheck": "true",
            "twofactorreentryskipfornow": "false",
        })
        self.check_login(user)
        self.user_id = user.get("UserID") or self.user_id

        await self.load_tokens()
        log.info("TN login ok: %s/%s", self.practice_code, self.username)
        if self.on_login:
            self.on_login()

    async def login_post(self, form: dict) -> dict:
        response = await self.request(
            "POST",
            f"{LOGIN_PATH}?msg=1",
            data={**form, "correlationid": str(uuid.uuid4()), "tnv": self.tnv},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base,
                "Referer": f"{self.base}/app/login/{self.practice_code}/",
            },
        )
        try:
            return response.json()
        except ValueError as exc:
            raise TherapyNotesError(f"TherapyNotes login returned {response.status_code} without JSON") from exc

    @staticmethod
    def check_login(data: dict) -> None:
        if data.get("IsSuccessful"):
            return
        if data.get("NeedsTwoFactor") or data.get("TwoFactorRequired"):
            raise LoginFailed(TWO_FACTOR_REQUIRED, "This account requires two-factor authentication")
        if data.get("ShowNoCookiesErrorPage"):
            raise TherapyNotesError("TherapyNotes rejected the login cookies")
        message = data.get("ErrorMessage") or "TherapyNotes rejected the login"
        code = ERROR_CODES.get(data.get("ErrorID"), LOGIN_FAILED)
        if "try again" in message.lower():
            code = TOO_MANY_ATTEMPTS
        raise LoginFailed(code, message)

    async def load_tokens(self) -> None:
        response = await self.request("GET", "/app/", follow_redirects=True)
        if "/login" in str(response.url).lower():
            raise SessionExpired("TherapyNotes session is not logged in")
        html = response.text
        self.tnrac = find(r'id="TNRAC"[^>]*value="([^"]*)"', html) or find(r'value="([^"]*)"[^>]*id="TNRAC"', html)
        self.tnv = find(r'siteVersion\s*=\s*"([^"]+)"', html) or self.tnv
        if not self.tnrac:
            raise TherapyNotesError("TNRAC token missing from the TherapyNotes app page")

    async def relogin(self) -> None:
        now = time.time()
        self.relogins = [t for t in self.relogins if now - t < RELOGIN_WINDOW_SECONDS]
        if len(self.relogins) >= RELOGIN_LIMIT:
            raise SessionExpired("TherapyNotes keeps dropping this session; login again")
        self.relogins.append(now)
        log.info("TN session expired for %s, logging in again", self.username)
        await self.login()

    # ---- app calls ---------------------------------------------------------

    async def send(self, path: str, form: dict, referer: str) -> httpx.Response:
        response = await self.request(
            "POST",
            path,
            data={
                **{k: "null" if v is None else str(v) for k, v in form.items()},
                "correlationid": str(uuid.uuid4()),
                "tnrac": self.tnrac,
                "tnv": self.tnv,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base,
                "Referer": f"{self.base}{referer}",
            },
        )
        name = path.rsplit("/", 1)[-1].split(".")[0]
        log.info("TN %s status: %s", name, response.status_code)
        if response.status_code == 401 or "Error | TherapyNotes" in response.text[:400]:
            raise SessionExpired(f"TherapyNotes returned {response.status_code} for {path}")
        if response.status_code != 200:
            raise TherapyNotesError(f"TherapyNotes returned {response.status_code} for {path}")
        return response

    async def call(self, path: str, form: dict, referer: str) -> httpx.Response:
        try:
            return await self.send(path, form, referer)
        except SessionExpired:
            await self.relogin()
            return await self.send(path, form, referer)

    async def post(self, path: str, form: dict, referer: str = "/app/"):
        response = await self.call(path, form, referer)
        try:
            return response.json()
        except ValueError as exc:
            raise TherapyNotesError(f"TherapyNotes returned non-JSON for {path}") from exc

    async def ping(self) -> bool:
        try:
            await self.call("/app/session/sessionping.aspx?msg=16", {}, "/app/")
            return True
        except (TherapyNotesError, SessionExpired, LoginFailed, CloudflareBlocked, httpx.HTTPError) as exc:
            log.warning("Session ping failed for %s: %s", self.username, exc)
            return False

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "practice_code": self.practice_code,
            "practice_name": self.practice_name,
            "username": self.username,
            "password_hash": self.password_hash,
            "user_id": self.user_id,
            "tnrac": self.tnrac,
            "tnv": self.tnv,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "cookies": {c.name: c.value for c in self.client.cookies.jar} if self.client else {},
        }

    @classmethod
    async def from_dict(cls, data: dict) -> "TnSession":
        cookies = data.pop("cookies", {})
        session = cls(**data)
        await session.open_client(cookies)
        return session
