"""Authentication dependency and current user resolution.

This module provides standard JWT Bearer dependency integration for FastAPI endpoints.
Until the full registration/auth flow is merged, it extracts the Bearer token and
supports:
1. Valid JWT tokens containing {"user_id": <int>} or {"sub": <int>}.
2. Fallback header `X-User-Id` or query/mock token for testing and local development.
3. Once the registration module is added, decode_jwt can be updated with real secrets.
"""

import json
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.services.users import UnknownUserError, require_user

# Optional HTTPBearer with auto_error=False to allow seamless developer/fallback headers if needed
security = HTTPBearer(auto_error=False)


def get_current_user_id(
    auth_creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
    x_user_id: Annotated[
        int | None, Header(description="Mock User ID header for development")
    ] = None,
) -> int:
    """Extract authenticated user_id from Bearer token or development header."""
    if auth_creds and auth_creds.credentials:
        token = auth_creds.credentials.strip()
        # 1. Attempt basic base64/JWT payload parse if formatted as header.payload.signature
        if "." in token:
            parts = token.split(".")
            if len(parts) >= 2:
                import base64

                payload_b64 = parts[1]
                # Pad base64 if needed
                payload_b64 += "=" * (-len(payload_b64) % 4)
                try:
                    payload_bytes = base64.urlsafe_b64decode(payload_b64)
                    payload = json.loads(payload_bytes.decode("utf-8"))
                    uid = payload.get("user_id") or payload.get("sub")
                    if uid is not None and str(uid).isdigit():
                        return int(uid)
                except Exception:
                    pass

        # 2. Allow raw user_id in Bearer token (e.g. Bearer 1) for lightweight test suites
        if token.isdigit():
            return int(token)

    # 3. Fallback header for testing/dev environments
    if x_user_id is not None and x_user_id >= 1:
        return x_user_id

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Resolve active database User model from authenticated user_id."""
    try:
        return require_user(db, user_id)
    except UnknownUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user does not exist",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
