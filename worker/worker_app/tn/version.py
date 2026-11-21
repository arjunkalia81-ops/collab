"""Conflict check before a save, and verification after it."""

import hashlib
import json


class Conflict(Exception):
    def __init__(self, what: str, current: dict):
        super().__init__(f"{what} was changed in TherapyNotes after you loaded it. Reload it and make your change again")
        self.current = current


class NotVerified(Exception):
    def __init__(self, what: str, fields: list[str], current: dict):
        super().__init__(
            f"{what} was saved, but TherapyNotes shows a different value for {', '.join(fields)}. Check it in TherapyNotes"
        )
        self.fields = fields
        self.current = current


def version(data) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def check_version(expected: str, current: dict, what: str) -> None:
    if expected != current["version"]:
        raise Conflict(what, current)


def same(a, b) -> bool:
    def norm(value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return float(value)
        return value
    return norm(a) == norm(b)


def verify(what: str, wanted: dict, saved: dict, current: dict) -> None:
    """Re-read check: every field in `wanted` must read back the same from TherapyNotes."""
    fields = [key for key, value in wanted.items() if not same(value, saved.get(key))]
    if fields:
        raise NotVerified(what, fields, current)
