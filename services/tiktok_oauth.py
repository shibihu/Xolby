"""TikTok OAuth flow management and HTTP callback redirect server."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import secrets
import time
import urllib.parse
from typing import Optional
from aiohttp import web

from services.db import db
from services.tiktok import (
    OFFICIAL_SCOPES,
    TIKTOK_AUTH_URL,
    TikTokAPIClient,
    TikTokSecurityError,
    encrypt_token,
    is_encryption_available,
)

log = logging.getLogger(__name__)

# State storage: state_token -> {"discord_user_id": int, "created_at": float}
_STATE_STORE: dict[str, dict] = {}
STATE_TTL_SECONDS = 600.0  # 10 minutes


def create_oauth_state(discord_user_id: int) -> str:
    """Generate a secure random state token bound to a Discord user ID."""
    _cleanup_expired_states()
    state = secrets.token_urlsafe(32)
    _STATE_STORE[state] = {
        "discord_user_id": discord_user_id,
        "created_at": time.time(),
    }
    return state


def verify_and_consume_state(state: str) -> Optional[int]:
    """Validate state token and return associated discord_user_id, consuming token."""
    _cleanup_expired_states()
    data = _STATE_STORE.pop(state, None)
    if not data:
        return None
    if time.time() - data["created_at"] > STATE_TTL_SECONDS:
        return None
    return data["discord_user_id"]


def _cleanup_expired_states() -> None:
    now = time.time()
    expired = [
        k for k, v in _STATE_STORE.items() if now - v["created_at"] > STATE_TTL_SECONDS
    ]
    for k in expired:
        _STATE_STORE.pop(k, None)


def build_authorization_url(state: str) -> str:
    """Generate the official TikTok OAuth authorization URL."""
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "")
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI", "")

    params = {
        "client_key": client_key,
        "scope": OFFICIAL_SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{TIKTOK_AUTH_URL}?{urllib.parse.urlencode(params)}"


class TikTokOAuthServer:
    """Lightweight web server to handle official TikTok OAuth callback redirects."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        api_client: Optional[TikTokAPIClient] = None,
    ) -> None:
        self.host = host or os.getenv("TIKTOK_CALLBACK_HOST", "0.0.0.0")
        self.port = port or int(os.getenv("TIKTOK_CALLBACK_PORT", "8080"))
        self.api_client = api_client or TikTokAPIClient()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def handle_callback(self, request: web.Request) -> web.Response:
        if not is_encryption_available():
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Security Error</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #f5e0dc; text-align: center; padding: 50px;">
                <h2 style="color: #f38ba8;">⚠️ Secure Token Storage Unavailable</h2>
                <p>TikTok connection cannot be completed because secure token encryption is unavailable in this environment.</p>
                <p>No access or refresh tokens were saved.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=500)

        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")
        error_desc = request.query.get("error_description")

        if error:
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Authorization Canceled</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #f5e0dc; text-align: center; padding: 50px;">
                <h2 style="color: #f38ba8;">Authorization Canceled</h2>
                <p>{error_desc or 'TikTok account connection was canceled or denied.'}</p>
                <p>You can close this window and try again in Discord with <code>/tiktokconnect</code>.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=400)

        if not state or not code:
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Invalid Request</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #f5e0dc; text-align: center; padding: 50px;">
                <h2 style="color: #f38ba8;">Invalid Callback Parameters</h2>
                <p>Missing state or authorization code.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=400)

        discord_user_id = verify_and_consume_state(state)
        if not discord_user_id:
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Session Expired</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #f5e0dc; text-align: center; padding: 50px;">
                <h2 style="color: #fab387;">OAuth Session Expired or Invalid</h2>
                <p>Your authorization session timed out or state token was invalid.</p>
                <p>Please run <code>/tiktokconnect</code> in Discord to start a new connection.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=400)

        try:
            tokens = await self.api_client.exchange_code(code)
            access_token = tokens["access_token"]
            refresh_token = tokens.get("refresh_token")
            open_id = tokens.get("open_id") or tokens.get("open_id", "unknown_openid")
            expires_in = tokens.get("expires_in", 86400)
            refresh_expires_in = tokens.get("refresh_expires_in", 31536000)

            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            expires_at = now_ts + expires_in
            refresh_expires_at = now_ts + refresh_expires_in if refresh_expires_in else None

            # Get user info
            display_name = ""
            try:
                user_info = await self.api_client.get_user_info(access_token)
                display_name = user_info.get("display_name", "")
            except Exception as exc:
                log.warning("Could not fetch display name: %s", type(exc).__name__)

            # Encrypt tokens before storing (raises TikTokSecurityError if encryption fails)
            enc_access = encrypt_token(access_token)
            enc_refresh = encrypt_token(refresh_token) if refresh_token else None

            # Store in DB
            account = db.save_tiktok_account(
                discord_user_id=discord_user_id,
                tiktok_open_id=open_id,
                display_name=display_name,
                access_token=enc_access,
                refresh_token=enc_refresh,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
            )

            # Record initial video snapshot if video exists
            try:
                videos = await self.api_client.get_user_videos(access_token, max_count=1)
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
            except Exception as e:
                log.warning("Initial snapshot store failed: %s", type(e).__name__)

            name_str = f" (@{display_name})" if display_name else ""
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>TikTok Account Connected</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #a6e3a1; text-align: center; padding: 50px;">
                <h1 style="color: #a6e3a1;">✅ TikTok Connected Successfully!</h1>
                <p style="color: #cdd6f4; font-size: 1.2em;">Account connected{name_str}</p>
                <p style="color: #bac2de;">You can close this tab and return to Discord.</p>
                <p style="color: #bac2de;">Use <code>/tiktokstats</code> or <code>/tiktoklive</code> in Discord to view your analytics!</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=200)

        except TikTokSecurityError as sec_err:
            log.error("TikTok OAuth security error: %s", sec_err)
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Security Error</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #f5e0dc; text-align: center; padding: 50px;">
                <h2 style="color: #f38ba8;">⚠️ Secure Storage Error</h2>
                <p>Secure token encryption failed. No credentials were stored.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=500)
        except Exception as exc:
            log.exception("Error during OAuth callback token exchange")
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Connection Error</title></head>
            <body style="font-family: system-ui, sans-serif; background: #1e1e2e; color: #f5e0dc; text-align: center; padding: 50px;">
                <h2 style="color: #f38ba8;">❌ Connection Failed</h2>
                <p>An error occurred while exchanging tokens with TikTok.</p>
                <p>Please try running <code>/tiktokconnect</code> again in Discord.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html", status=500)

    async def start(self) -> None:
        """Start the callback HTTP server."""
        app = web.Application()
        app.router.add_get("/tiktok/callback", self.handle_callback)
        app.router.add_get("/", self.handle_callback)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        log.info("[TIKTOK OAUTH] Callback server listening at http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the callback server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        await self.api_client.close()
        log.info("[TIKTOK OAUTH] Callback server stopped.")
