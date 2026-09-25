import datetime
import logging
import os
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from services.db import db
from services.tiktok import (
    OFFICIAL_SCOPES,
    TIKTOK_AUTH_URL,
    TikTokAPIClient,
    TikTokSecurityError,
    encrypt_token,
    is_encryption_available,
)
from services.tiktok_oauth import (
    _STATE_STORE,
    create_oauth_state,
    verify_and_consume_state,
)

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


def is_tiktok_configured() -> tuple[bool, str]:
    client_key = (os.getenv("TIKTOK_CLIENT_KEY") or "").strip()
    client_secret = (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.getenv("TIKTOK_REDIRECT_URI") or "").strip()

    if not client_key or "PUT_YOUR" in client_key:
        return False, "TIKTOK_CLIENT_KEY is missing or unconfigured."
    if not client_secret or "PUT_YOUR" in client_secret:
        return False, "TIKTOK_CLIENT_SECRET is missing or unconfigured."
    if not redirect_uri:
        return False, "TIKTOK_REDIRECT_URI is missing or unconfigured."

    return True, "Configured"


def mask_secret(value: str) -> str:
    if not value or len(value) < 8:
        return "********"
    return value[:4] + "********" + value[-4:]


def get_config_diagnostics() -> dict[str, str]:
    ck = (os.getenv("TIKTOK_CLIENT_KEY") or "").strip()
    cs = (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()
    ru = (os.getenv("TIKTOK_REDIRECT_URI") or "").strip()

    return {
        "TIKTOK_CLIENT_KEY": mask_secret(ck) if ck and "PUT_YOUR" not in ck else "unconfigured",
        "TIKTOK_CLIENT_SECRET": "configured" if cs and "PUT_YOUR" not in cs else "unconfigured",
        "TIKTOK_REDIRECT_URI": ru if ru else "unconfigured",
        "ENCRYPTION_AVAILABLE": str(is_encryption_available()),
    }


@router.get("/oauth/tiktok", response_class=HTMLResponse)
async def start_tiktok_oauth(
    request: Request,
    state: Optional[str] = Query(None),
):
    configured, reason = is_tiktok_configured()
    if not configured:
        log.error("TikTok OAuth start failed: %s", reason)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Configuration Error",
                "message": "TikTok Integration is not fully configured on the server. Please check environment variables.",
                "current_year": datetime.datetime.now(datetime.timezone.utc).year,
            },
            status_code=500,
        )

    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI", "").strip()

    if state and state in _STATE_STORE:
        state_token = state
    else:
        state_token = create_oauth_state(discord_user_id=0)

    params = {
        "client_key": client_key,
        "scope": OFFICIAL_SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state_token,
    }
    auth_url = f"{TIKTOK_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=auth_url, status_code=307)


@router.get("/oauth/tiktok/callback", response_class=HTMLResponse)
@router.get("/tiktok/callback", response_class=HTMLResponse)
async def tiktok_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    current_year = datetime.datetime.now(datetime.timezone.utc).year

    if not is_encryption_available():
        log.error("TikTok OAuth callback error: Encryption backend unavailable.")
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Security Error",
                "message": "Secure token storage (`cryptography`) is unavailable in this server environment. OAuth connection disabled.",
                "current_year": current_year,
            },
            status_code=500,
        )

    if error:
        log.warning("TikTok OAuth callback received error: %s - %s", error, error_description)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Authorization Canceled",
                "message": error_description or "TikTok authorization was canceled or denied.",
                "current_year": current_year,
            },
            status_code=400,
        )

    if not state or not code:
        log.warning("TikTok OAuth callback missing required parameters.")
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Invalid Request",
                "message": "Missing required callback parameters (code or state).",
                "current_year": current_year,
            },
            status_code=400,
        )

    discord_user_id = verify_and_consume_state(state)
    if discord_user_id is None:
        log.warning("TikTok OAuth callback rejected invalid or expired state token.")
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Session Expired",
                "message": "Your OAuth session timed out or was invalid. Please run `/tiktokconnect` in Discord to start a new connection.",
                "current_year": current_year,
            },
            status_code=400,
        )

    api_client = TikTokAPIClient()
    try:
        tokens = await api_client.exchange_code(code)
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        open_id = tokens.get("open_id") or tokens.get("open_id", "unknown_openid")
        expires_in = tokens.get("expires_in", 86400)
        refresh_expires_in = tokens.get("refresh_expires_in", 31536000)

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        expires_at = now_ts + expires_in
        refresh_expires_at = now_ts + refresh_expires_in if refresh_expires_in else None

        display_name = ""
        try:
            user_info = await api_client.get_user_info(access_token)
            display_name = user_info.get("display_name", "")
        except Exception as exc:
            log.warning("Could not fetch TikTok user info in callback: %s", type(exc).__name__)

        enc_access = encrypt_token(access_token)
        enc_refresh = encrypt_token(refresh_token) if refresh_token else None

        if discord_user_id > 0:
            db.save_tiktok_account(
                discord_user_id=discord_user_id,
                tiktok_open_id=open_id,
                display_name=display_name,
                access_token=enc_access,
                refresh_token=enc_refresh,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
            )

            try:
                videos = await api_client.get_user_videos(access_token, max_count=1)
                if videos:
                    v = videos[0]
                    v_id = str(v.get("id", ""))
                    v_title = v.get("title") or v.get("video_description") or "Untitled Video"
                    db.add_tiktok_snapshot(
                        discord_user_id=discord_user_id,
                        tiktok_open_id=open_id,
                        video_id=v_id,
                        video_title=v_title,
                        view_count=int(v.get("view_count", 0)),
                        like_count=int(v.get("like_count", 0)),
                        comment_count=int(v.get("comment_count", 0)),
                        share_count=int(v.get("share_count", 0)),
                        favorite_count=int(v.get("favorite_count", 0)),
                    )
            except Exception as exc:
                log.warning("Failed to record initial video snapshot in callback: %s", type(exc).__name__)

        log.info("Successfully completed TikTok OAuth token exchange for OpenID %s", mask_secret(open_id))
        return templates.TemplateResponse(
            request=request,
            name="success.html",
            context={
                "display_name": display_name,
                "current_year": current_year,
            },
            status_code=200,
        )

    except TikTokSecurityError as sec_err:
        log.error("TikTok OAuth security error during callback: %s", sec_err)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Security Error",
                "message": "Failed to encrypt tokens securely. Account credentials were not stored.",
                "current_year": current_year,
            },
            status_code=500,
        )
    except Exception as exc:
        log.exception("Unexpected error during TikTok OAuth code exchange")
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "Connection Error",
                "message": "An error occurred while communicating with TikTok. Please try again.",
                "current_year": current_year,
            },
            status_code=500,
        )
    finally:
        await api_client.close()
