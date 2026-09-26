"""TikTok OAuth backend for the Xolby Render web service.

This is the *single* owner of TikTok OAuth: it creates and validates OAuth
state, exchanges authorization codes, encrypts tokens, and persists encrypted
credentials. The Discord bot on Termux only talks to the internal JSON API and
never handles TikTok secrets or tokens.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services.tiktok import (
    TikTokAPIError,
    TikTokSecurityError,
    TikTokTokenExpiredError,
    encrypt_token,
    is_encryption_available,
)
from web import oauth_store as oauth_store_module
from web.auth import require_api_key
from web.oauth_store import STATE_TTL_SECONDS
from web.tiktok_service import (
    build_authorization_url,
    fetch_account,
    fetch_videos,
    get_config_diagnostics,
    is_tiktok_configured,
    mask_secret,
    revoke_and_delete,
)

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

# Re-exported so callers (e.g. web.app startup diagnostics) can keep importing
# them from this module.
__all__ = [
    "router",
    "get_config_diagnostics",
    "mask_secret",
    "is_tiktok_configured",
]


class OAuthStartRequest(BaseModel):
    discord_user_id: int = Field(..., gt=0, description="Discord user snowflake")


class DisconnectRequest(BaseModel):
    discord_user_id: int = Field(..., gt=0, description="Discord user snowflake")


def _json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": detail})


def _error_page(request: Request, title: str, message: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "title": title,
            "message": message,
            "current_year": datetime.datetime.now(datetime.timezone.utc).year,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Internal bot <-> backend API (authenticated with XOLBY_WEB_API_KEY)
# ---------------------------------------------------------------------------
@router.post("/api/tiktok/oauth/start", dependencies=[Depends(require_api_key)])
async def api_oauth_start(payload: OAuthStartRequest) -> JSONResponse:
    """Create a short-lived OAuth session bound to a Discord user and return
    the TikTok authorization URL for that user to open."""
    configured, reason = is_tiktok_configured()
    if not configured:
        log.error("TikTok OAuth start refused: %s", reason)
        return _json_error(503, "TikTok integration is not configured on the server.")

    if not is_encryption_available():
        log.error("TikTok OAuth start refused: encryption backend unavailable.")
        return _json_error(503, "Secure token storage is unavailable on the server.")

    try:
        session = oauth_store_module.oauth_store.create_session(payload.discord_user_id)
    except ValueError:
        return _json_error(400, "Invalid Discord user id.")

    authorization_url = build_authorization_url(session.state)
    log.info(
        "Created TikTok OAuth session for Discord user %s (expires in %ss)",
        payload.discord_user_id,
        STATE_TTL_SECONDS,
    )
    return JSONResponse(
        status_code=200,
        content={
            "authorization_url": authorization_url,
            "expires_at": session.expires_at,
            "expires_in": STATE_TTL_SECONDS,
        },
    )


@router.get("/api/tiktok/account/{discord_user_id}", dependencies=[Depends(require_api_key)])
async def api_tiktok_account(discord_user_id: int) -> JSONResponse:
    """Return non-sensitive connected account metadata (never returns tokens)."""
    if discord_user_id <= 0:
        return _json_error(400, "Invalid Discord user id.")
    result = await fetch_account(discord_user_id)
    return JSONResponse(status_code=200, content=result)


@router.get("/api/tiktok/videos/{discord_user_id}", dependencies=[Depends(require_api_key)])
async def api_tiktok_videos(discord_user_id: int) -> JSONResponse:
    """Return recent video metrics for a Discord user's connected account."""
    if discord_user_id <= 0:
        return _json_error(400, "Invalid Discord user id.")
    result = await fetch_videos(discord_user_id)
    return JSONResponse(status_code=200, content=result)


@router.post("/api/tiktok/disconnect", dependencies=[Depends(require_api_key)])
async def api_tiktok_disconnect(payload: DisconnectRequest) -> JSONResponse:
    """Revoke tokens server-side and delete stored credentials/sessions."""
    result = await revoke_and_delete(payload.discord_user_id)
    return JSONResponse(status_code=200, content=result)


# ---------------------------------------------------------------------------
# Public OAuth entry + callback (TikTok redirects here)
# ---------------------------------------------------------------------------
@router.get("/oauth/tiktok", response_class=HTMLResponse)
async def oauth_entry(request: Request) -> HTMLResponse:
    """Explain how to start the flow.

    The OAuth state is bound to a Discord user id by the internal API, so this
    endpoint intentionally cannot start a session on its own.
    """
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "title": "Connect from Discord",
            "message": (
                "Please run /tiktokconnect inside Discord to start a secure TikTok "
                "connection. This page cannot create an authorization session."
            ),
            "current_year": datetime.datetime.now(datetime.timezone.utc).year,
        },
        status_code=200,
    )


@router.get("/oauth/tiktok/callback", response_class=HTMLResponse)
async def tiktok_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
) -> HTMLResponse:
    """Handle the TikTok redirect: validate state, exchange code, store tokens."""
    if error:
        log.warning("TikTok OAuth callback received error: %s - %s", error, error_description)
        return _error_page(
            request,
            "Authorization Canceled",
            error_description or "TikTok authorization was canceled or denied.",
            400,
        )

    if not state or not code:
        log.warning("TikTok OAuth callback missing required parameters.")
        return _error_page(
            request,
            "Invalid Request",
            "Missing required callback parameters (code or state).",
            400,
        )

    if not is_encryption_available():
        log.error("TikTok OAuth callback refused: encryption backend unavailable.")
        return _error_page(
            request,
            "Security Error",
            "Secure token storage is unavailable on the server. No tokens were saved.",
            500,
        )

    session = oauth_store_module.oauth_store.consume_session(state)
    if session is None:
        log.warning("TikTok OAuth callback rejected invalid/expired/consumed state.")
        return _error_page(
            request,
            "Session Expired",
            "Your OAuth session expired, was already used, or was invalid. "
            "Run /tiktokconnect in Discord to start a new connection.",
            400,
        )

    from services.tiktok import TikTokAPIClient  # local import keeps module deps light

    api_client = TikTokAPIClient()
    try:
        tokens = await api_client.exchange_code(code)
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        open_id = tokens.get("open_id") or "unknown_openid"
        expires_in = int(tokens.get("expires_in", 86400))
        refresh_expires_in = tokens.get("refresh_expires_in")

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        expires_at = now_ts + expires_in
        refresh_expires_at = now_ts + int(refresh_expires_in) if refresh_expires_in else None

        display_name = ""
        try:
            user_info = await api_client.get_user_info(access_token)
            display_name = user_info.get("display_name", "") or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch TikTok user info in callback: %s", type(exc).__name__)

        # Encrypt before touching storage. Raises if encryption is unavailable.
        enc_access = encrypt_token(access_token)
        enc_refresh = encrypt_token(refresh_token) if refresh_token else None

        oauth_store_module.oauth_store.save_credentials(
            discord_user_id=session.discord_user_id,
            tiktok_open_id=open_id,
            display_name=display_name,
            access_token=enc_access,
            refresh_token=enc_refresh,
            expires_at=expires_at,
            refresh_expires_at=refresh_expires_at,
        )

        log.info(
            "Completed TikTok OAuth for Discord user %s (open_id %s)",
            session.discord_user_id,
            mask_secret(open_id),
        )
        return templates.TemplateResponse(
            request=request,
            name="success.html",
            context={
                "display_name": display_name,
                "current_year": datetime.datetime.now(datetime.timezone.utc).year,
            },
            status_code=200,
        )

    except TikTokSecurityError:
        log.error("TikTok OAuth callback failed to encrypt tokens.")
        return _error_page(
            request,
            "Security Error",
            "Failed to encrypt tokens securely. No credentials were stored.",
            500,
        )
    except TikTokTokenExpiredError:
        return _error_page(
            request,
            "Connection Error",
            "TikTok rejected the authorization code (it may have expired). Please try again.",
            400,
        )
    except TikTokAPIError:
        log.error("TikTok token exchange failed.")
        return _error_page(
            request,
            "Connection Error",
            "An error occurred while communicating with TikTok. Please try again.",
            500,
        )
    except Exception:
        log.exception("Unexpected error during TikTok OAuth code exchange")
        return _error_page(
            request,
            "Connection Error",
            "An unexpected error occurred. Please try again from Discord.",
            500,
        )
    finally:
        await api_client.close()
