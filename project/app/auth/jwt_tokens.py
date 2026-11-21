"""Our API JWTs (not TherapyNotes cookies)."""

from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings


def create_access_token(session_id: str) -> str:
    payload = {
        "sub": session_id,
        "type": "access",
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=settings.jwt_access_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(session_id: str) -> str:
    payload = {
        "sub": session_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc)
        + timedelta(days=settings.jwt_refresh_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_token_pair(session_id: str) -> dict:
    return {
        "access_token": create_access_token(session_id),
        "refresh_token": create_refresh_token(session_id),
        "token_type": "bearer",
        "expires_in": settings.jwt_access_minutes * 60,
    }


def decode_token(token: str, expected_type: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    if not payload.get("sub"):
        raise jwt.InvalidTokenError("missing session id")
    return payload
