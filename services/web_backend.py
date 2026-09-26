"""HTTP client for the Xolby Render web backend (Discord bot side).

The bot running on Termux uses this client to start TikTok OAuth sessions and
to read TikTok analytics. It never handles the TikTok client secret, plaintext
tokens, or token encryption - all of that lives on Render.

Configuration (Termux only)::

    XOLBY_WEB_BASE_URL=https://xolby.onrender.com
    XOLBY_WEB_API_KEY=<shared internal API key>
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0


class WebBackendError(Exception):
    """Base error for Xolby web backend communication."""


class WebBackendUnavailable(WebBackendError):
    """Raised when the Render backend cannot be reached (network/5xx/timeout)."""


class WebBackendAuthError(WebBackendError):
    """Raised when the backend rejects the bot's API credentials."""


@dataclass(frozen=True)
class TikTokAccountInfo:
    """Non-sensitive TikTok account metadata returned by the backend."""

    discord_user_id: int
    tiktok_open_id: str
    display_name: str


class XolbyWebBackend:
    """Small async client for the authenticated internal bot <-> backend API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._base_url = (base_url if base_url is not None else os.getenv("XOLBY_WEB_BASE_URL", "")).strip().rstrip("/")
        self._api_key = (api_key if api_key is not None else os.getenv("XOLBY_WEB_API_KEY", "")).strip()
        self._session = session

    @property
    def base_url(self) -> str:
        return self._base_url

    def is_configured(self) -> bool:
        """True when both the base URL and API key are present."""
        return bool(self._base_url and self._api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self, method: str, path: str, payload: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        if not self.is_configured():
            raise WebBackendUnavailable(
                "Xolby web backend is not configured (XOLBY_WEB_BASE_URL / XOLBY_WEB_API_KEY)."
            )

        session = await self._get_session()
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with session.request(method, url, json=payload, headers=headers) as resp:
                if resp.status in (401, 403):
                    raise WebBackendAuthError("Xolby web backend rejected the bot API key.")
                if resp.status >= 500:
                    raise WebBackendUnavailable(f"Xolby web backend error (HTTP {resp.status}).")
                if resp.status >= 400:
                    raise WebBackendError(f"Xolby web backend request failed (HTTP {resp.status}).")
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else {"data": data}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Xolby web backend request failed: %s", type(exc).__name__)
            raise WebBackendUnavailable(
                f"Could not reach the Xolby web backend ({type(exc).__name__})."
            ) from exc

    # ------------------------------------------------------------------
    # TikTok operations
    # ------------------------------------------------------------------
    async def start_tiktok_oauth(self, discord_user_id: int) -> dict[str, Any]:
        """Create a server-side OAuth session and return its authorization URL."""
        return await self._request(
            "POST", "/api/tiktok/oauth/start", {"discord_user_id": int(discord_user_id)}
        )

    async def get_account(self, discord_user_id: int) -> Optional[TikTokAccountInfo]:
        """Return connected account metadata, or None when not connected."""
        data = await self._request("GET", f"/api/tiktok/account/{int(discord_user_id)}")
        if not data.get("connected") or not data.get("account"):
            return None
        return self._account_from_payload(data["account"])

    async def get_analytics(self, discord_user_id: int) -> dict[str, Any]:
        """Return ``{status, videos, account, timestamp}`` for a Discord user."""
        data = await self._request("GET", f"/api/tiktok/videos/{int(discord_user_id)}")
        status = data.get("status") or "error"
        videos = data.get("videos") or []
        account = self._account_from_payload(data.get("account"))
        result: dict[str, Any] = {
            "status": status,
            "videos": videos,
            "account": account,
            "timestamp": time.time(),
        }
        if data.get("message"):
            result["message"] = data["message"]
        return result

    async def disconnect(self, discord_user_id: int) -> dict[str, Any]:
        """Ask the backend to revoke tokens and delete stored credentials."""
        return await self._request(
            "POST", "/api/tiktok/disconnect", {"discord_user_id": int(discord_user_id)}
        )

    @staticmethod
    def _account_from_payload(payload: Optional[dict[str, Any]]) -> Optional[TikTokAccountInfo]:
        if not payload:
            return None
        try:
            return TikTokAccountInfo(
                discord_user_id=int(payload.get("discord_user_id") or 0),
                tiktok_open_id=str(payload.get("tiktok_open_id") or ""),
                display_name=str(payload.get("display_name") or ""),
            )
        except (TypeError, ValueError):
            return None


#: Module level singleton used by the bot.
web_backend = XolbyWebBackend()
