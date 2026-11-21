"""Login, token refresh, session ping and logout. The TherapyNotes session itself lives in the worker."""

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import require_session_id
from app.auth.jwt_tokens import create_access_token, create_token_pair, decode_token
from app.core.config import settings
from app.worker_client import worker_close, worker_login, worker_session_ping

router = APIRouter(tags=["Auth"])


class LoginBody(BaseModel):
    practice_code: str = Field(min_length=1, description="TherapyNotes practice code")
    username: str = Field(min_length=1, description="TherapyNotes username")
    password: SecretStr = Field(min_length=1, description="TherapyNotes password")


class RefreshBody(BaseModel):
    refresh_token: str = Field(description="`refresh_token` from login")


@router.post(
    "/login",
    summary="Log in",
    description=(
        "Signs in to TherapyNotes with the practice code, username and password. "
        "Returns a short-lived `access_token` for the other APIs and a `refresh_token` to renew it."
    ),
)
async def login_endpoint(body: LoginBody):
    result = await worker_login(
        body.practice_code.strip(),
        body.username.strip(),
        body.password.get_secret_value(),
    )
    return {**create_token_pair(result["session_id"]), "practice_name": result.get("practice_name")}


@router.post(
    "/refresh",
    summary="Get a new access token",
    description="Swaps the refresh token for a new access token. Returns 401 if the TherapyNotes session has ended; log in again.",
)
async def refresh_endpoint(body: RefreshBody):
    try:
        payload = decode_token(body.refresh_token, "refresh")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid refresh token: {exc}") from exc

    session_id = payload["sub"]
    # Worker answers 401 if it no longer holds this TN session.
    await worker_session_ping(session_id)
    return {
        "access_token": create_access_token(session_id),
        "token_type": "bearer",
        "expires_in": settings.jwt_access_minutes * 60,
    }


@router.post(
    "/session-ping",
    summary="Keep the session alive",
    description="Pings TherapyNotes now so the session doesn't time out. The server also pings on its own.",
)
async def session_ping_endpoint(session_id: str = Depends(require_session_id)):
    return await worker_session_ping(session_id)


@router.post("/logout", summary="Log out", description="Closes your TherapyNotes session.")
async def logout_endpoint(session_id: str = Depends(require_session_id)):
    await worker_close(session_id)
    return {"ok": True, "message": "TherapyNotes session closed"}


@router.post(
    "/logout/force",
    summary="Log out everyone",
    description="Closes every TherapyNotes session on the server. For testing; it needs no token.",
)
async def logout_force_endpoint():
    await worker_close()
    return {"ok": True, "message": "All TherapyNotes sessions closed"}
