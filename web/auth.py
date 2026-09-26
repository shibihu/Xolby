"""Authentication for the internal Discord-bot <-> Render backend API.

The bot authenticates with a shared secret configured as ``XOLBY_WEB_API_KEY``
and sent as ``Authorization: Bearer <key>``. The key is never placed in OAuth
URLs, Discord messages, or logs.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException, status

log = logging.getLogger(__name__)

WEB_API_KEY_ENV = "XOLBY_WEB_API_KEY"


def _expected_api_key() -> str:
    return (os.getenv(WEB_API_KEY_ENV) or "").strip()


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency that validates the internal bearer API key."""
    expected = _expected_api_key()
    if not expected:
        log.error("%s is not configured; rejecting internal API request.", WEB_API_KEY_ENV)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API authentication is not configured on the server.",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = authorization[len("bearer "):].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("Rejected internal API request with invalid credentials.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
