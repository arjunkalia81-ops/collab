"""FastAPI auth helper — Bearer access JWT → session_id."""

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.jwt_tokens import decode_token

bearer = HTTPBearer(auto_error=False, description="Paste the `access_token` from `POST /api/v1/login`.")


def require_session_id(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        payload = decode_token(creds.credentials, "access")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    return payload["sub"]
