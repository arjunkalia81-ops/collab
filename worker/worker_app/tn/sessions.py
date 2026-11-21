"""Session registry: in-memory TnSessions, mirrored to sessions.json so a worker restart keeps users logged in."""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from worker_app.core.config import settings
from worker_app.tn.client import TnSession, password_hash

FILE = Path(__file__).resolve().parents[2] / "sessions.json"
SWEEP_SECONDS = 60

log = logging.getLogger(__name__)


class SessionLimit(Exception):
    pass


class Registry:
    def __init__(self):
        self.sessions: dict[str, TnSession] = {}
        self.pings: dict[str, asyncio.Task] = {}
        self.sweeper: asyncio.Task | None = None

    def get(self, session_id: str) -> TnSession | None:
        return self.sessions.get(session_id)

    def find(self, practice_code: str, username: str) -> TnSession | None:
        for session in self.sessions.values():
            if session.practice_code.lower() == practice_code.lower() and session.username.lower() == username.lower():
                return session
        return None

    async def login(self, practice_code: str, username: str, password: str) -> TnSession:
        digest = password_hash(password)
        existing = self.find(practice_code, username)
        if existing and existing.password_hash == digest and await existing.ping():
            existing.touch()
            return existing

        if len(self.sessions) - (1 if existing else 0) >= settings.tn_max_sessions:
            raise SessionLimit(f"Worker already holds {settings.tn_max_sessions} sessions")

        session = TnSession(str(uuid.uuid4()), practice_code, username, digest)
        session.on_login = self.save
        await session.login()
        if existing:
            await self.drop(existing.session_id)
        self.add(session)
        self.save()
        return session

    def add(self, session: TnSession) -> None:
        self.sessions[session.session_id] = session
        self.pings[session.session_id] = asyncio.create_task(self.ping_loop(session))

    async def drop(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if not session:
            return
        await cancel(self.pings.pop(session_id, None))
        await session.close()
        self.save()

    async def drop_all(self) -> None:
        for session_id in list(self.sessions):
            await self.drop(session_id)

    # ---- background ----------------------------------------------------------

    async def ping_loop(self, session: TnSession) -> None:
        interval = max(30, settings.tn_session_ping_seconds)
        while True:
            await asyncio.sleep(interval)
            if not await session.ping():
                log.warning("Keep-alive failed for %s; next call will re-login", session.username)

    async def sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            cutoff = time.time() - settings.tn_idle_minutes * 60
            for session in list(self.sessions.values()):
                if session.last_used < cutoff:
                    log.info("Dropping idle session for %s", session.username)
                    await self.drop(session.session_id)

    # ---- persistence -------------------------------------------------------

    def save(self) -> None:
        FILE.write_text(
            json.dumps([s.to_dict() for s in self.sessions.values()], indent=2),
            encoding="utf-8",
        )

    async def start(self) -> None:
        if FILE.exists():
            for data in json.loads(FILE.read_text(encoding="utf-8") or "[]"):
                session = await TnSession.from_dict(data)
                session.on_login = self.save
                self.add(session)
            log.info("Restored %s session(s) from %s", len(self.sessions), FILE.name)
        self.sweeper = asyncio.create_task(self.sweep_loop())

    async def stop(self) -> None:
        await cancel(self.sweeper)
        for task in self.pings.values():
            await cancel(task)
        self.save()
        for session in self.sessions.values():
            await session.close()


async def cancel(task: asyncio.Task | None) -> None:
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


registry = Registry()
