"""TikTok service providing token encryption, engagement metrics calculation,
and official TikTok API integration.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import logging
import os
from typing import Any, Optional
import aiohttp

log = logging.getLogger(__name__)

# Safely import cryptography if available in target environment
IS_ENCRYPTION_AVAILABLE = False
Fernet = None
InvalidToken = Exception

try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    Fernet = _Fernet
    InvalidToken = _InvalidToken
    IS_ENCRYPTION_AVAILABLE = True
except (ImportError, Exception) as _crypto_err:
    log.warning(
        "Cryptography module failed to load (%s: %s). TikTok token encryption will be unavailable.",
        type(_crypto_err).__name__,
        _crypto_err,
    )

# Official TikTok API Endpoints
TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"

OFFICIAL_SCOPES = "user.info.basic,video.list"

# Studio-only metrics explicitly listed as unavailable through the official API
UNAVAILABLE_STUDIO_METRICS = [
    "Stayed to watch",
    "Average watch time",
    "Total play time",
    "Audience retention",
    "Watched full video",
]


# ---------------------------------------------------------------------------
# TikTok API Custom Exceptions
# ---------------------------------------------------------------------------
class TikTokAPIError(Exception):
    """General error communicating with TikTok API."""
    pass


class TikTokTokenExpiredError(TikTokAPIError):
    """User authorization token expired and needs reconnection."""
    pass


class TikTokRateLimitError(TikTokAPIError):
    """TikTok API rate limit reached."""
    pass


class TikTokSecurityError(TikTokAPIError):
    """Raised when secure token encryption is unavailable or encryption/decryption fails."""
    pass


# ---------------------------------------------------------------------------
# Token Encryption Utilities
# ---------------------------------------------------------------------------
def is_encryption_available() -> bool:
    """Check if secure token encryption backend is available."""
    return IS_ENCRYPTION_AVAILABLE and Fernet is not None


def _get_fernet() -> Any:
    """Derive a deterministic 32-byte Fernet key from environment secrets."""
    if not is_encryption_available():
        raise TikTokSecurityError("Secure token encryption is unavailable in this environment.")

    secret = (
        os.getenv("TIKTOK_TOKEN_ENCRYPTION_KEY")
        or os.getenv("DISCORD_TOKEN")
        or "default_xolby_tiktok_secret_key"
    )
    salt = b"xolby_tiktok_salt_v1"
    key_bytes = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000)
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a sensitive token string at rest.

    Raises TikTokSecurityError if secure encryption is unavailable.
    NEVER falls back to returning plaintext tokens.
    """
    if not plaintext:
        return ""
    if not is_encryption_available():
        raise TikTokSecurityError("Secure token encryption is unavailable in this environment.")

    try:
        f = _get_fernet()
        return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    except Exception as e:
        log.error("Failed to encrypt token: %s", type(e).__name__)
        raise TikTokSecurityError("Token encryption failed.") from e


def decrypt_token(ciphertext: str) -> str:
    """Decrypt an encrypted token string at rest.

    Raises TikTokSecurityError if secure encryption is unavailable.
    NEVER falls back to returning unencrypted or invalid tokens.
    """
    if not ciphertext:
        return ""
    if not is_encryption_available():
        raise TikTokSecurityError("Secure token encryption is unavailable in this environment.")

    try:
        f = _get_fernet()
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception) as e:
        log.error("Failed to decrypt token: %s", type(e).__name__)
        raise TikTokSecurityError("Token decryption failed.") from e


# ---------------------------------------------------------------------------
# Engagement Rate Calculation
# ---------------------------------------------------------------------------
def calculate_like_rate(views: int, likes: int) -> float:
    """Calculate like rate percentage safely."""
    if views <= 0:
        return 0.0
    val = (likes / views) * 100.0
    return round(val, 2)


def calculate_comment_rate(views: int, comments: int) -> float:
    """Calculate comment rate percentage safely."""
    if views <= 0:
        return 0.0
    val = (comments / views) * 100.0
    return round(val, 2)


def calculate_share_rate(views: int, shares: int) -> float:
    """Calculate share rate percentage safely."""
    if views <= 0:
        return 0.0
    val = (shares / views) * 100.0
    return round(val, 2)


def calculate_total_engagement_rate(
    views: int, likes: int, comments: int, shares: int, favorites: int = 0
) -> float:
    """Calculate total engagement rate percentage safely."""
    if views <= 0:
        return 0.0
    total_eng = likes + comments + shares + favorites
    val = (total_eng / views) * 100.0
    return round(val, 2)


def calculate_engagement_metrics(
    views: int, likes: int, comments: int, shares: int, favorites: int = 0
) -> dict[str, float]:
    """Return dictionary of all calculated engagement rates."""
    return {
        "like_rate": calculate_like_rate(views, likes),
        "comment_rate": calculate_comment_rate(views, comments),
        "share_rate": calculate_share_rate(views, shares),
        "total_engagement_rate": calculate_total_engagement_rate(
            views, likes, comments, shares, favorites
        ),
    }


# ---------------------------------------------------------------------------
# TikTok Official API Client Wrapper
# ---------------------------------------------------------------------------
class TikTokAPIClient:
    """Async client for official TikTok Display API v2 endpoints."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._session = session
        self.client_key = os.getenv("TIKTOK_CLIENT_KEY", "")
        self.client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "")
        self.redirect_uri = os.getenv("TIKTOK_REDIRECT_URI", "")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15.0)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange OAuth authorization code for access and refresh tokens."""
        session = await self._get_session()
        payload = {
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            async with session.post(
                TIKTOK_TOKEN_URL, data=payload, headers=headers
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    log.error("TikTok token exchange HTTP error: status %s", resp.status)
                    err_msg = data.get("error_description") or data.get("message") or "Token exchange failed"
                    raise TikTokAPIError(f"HTTP {resp.status}: {err_msg}")

                if "access_token" not in data and "data" in data:
                    data = data["data"]

                if not data.get("access_token"):
                    err_desc = data.get("error_description") or data.get("error") or "Missing access_token"
                    raise TikTokAPIError(f"Token exchange error: {err_desc}")

                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("TikTok token exchange connection error: %s", type(exc).__name__)
            raise TikTokAPIError(f"Network error during token exchange: {type(exc).__name__}") from exc

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh an expired access token using the refresh token."""
        session = await self._get_session()
        payload = {
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            async with session.post(
                TIKTOK_TOKEN_URL, data=payload, headers=headers
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    log.error("TikTok token refresh HTTP error: status %s", resp.status)
                    raise TikTokTokenExpiredError("Refresh token invalid or expired")

                if "access_token" not in data and "data" in data:
                    data = data["data"]

                if not data.get("access_token"):
                    raise TikTokTokenExpiredError("Failed to refresh access token")

                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("TikTok token refresh connection error: %s", type(exc).__name__)
            raise TikTokAPIError(f"Network error during token refresh: {type(exc).__name__}") from exc

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        """Retrieve user info (display_name, open_id, avatar_url)."""
        session = await self._get_session()
        url = f"{TIKTOK_USER_INFO_URL}?fields=open_id,union_id,avatar_url,display_name"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with session.post(url, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 401:
                    raise TikTokTokenExpiredError("Access token expired")
                elif resp.status == 429:
                    raise TikTokRateLimitError("Rate limit exceeded")
                elif resp.status != 200:
                    raise TikTokAPIError(f"HTTP {resp.status} fetching user info")

                err_info = data.get("error", {})
                code = err_info.get("code")
                if code and code not in ("ok", 0, "0"):
                    if "token" in str(code).lower() or "auth" in str(code).lower():
                        raise TikTokTokenExpiredError("Token authorization expired")
                    raise TikTokAPIError(f"TikTok API error: {err_info.get('message', code)}")

                user_data = data.get("data", {}).get("user", {})
                return user_data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("TikTok user info connection error: %s", type(exc).__name__)
            raise TikTokAPIError(f"Network error fetching user info: {type(exc).__name__}") from exc

    async def get_user_videos(
        self, access_token: str, max_count: int = 10, retries: int = 2
    ) -> list[dict[str, Any]]:
        """Query user's recent videos with supported metrics."""
        session = await self._get_session()
        fields = (
            "id,title,video_description,cover_image_url,create_time,"
            "share_url,view_count,like_count,comment_count,share_count,favorite_count"
        )
        url = f"{TIKTOK_VIDEO_LIST_URL}?fields={fields}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"max_count": max(1, min(max_count, 20))}

        for attempt in range(retries + 1):
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 401:
                        raise TikTokTokenExpiredError("Access token expired")
                    elif resp.status == 429:
                        if attempt < retries:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise TikTokRateLimitError("Rate limit exceeded")
                    elif resp.status != 200:
                        raise TikTokAPIError(f"HTTP {resp.status} fetching videos")

                    err_info = data.get("error", {})
                    code = err_info.get("code")
                    if code and code not in ("ok", 0, "0"):
                        msg = err_info.get("message", str(code))
                        if "token" in str(code).lower() or "access_token" in str(code).lower():
                            raise TikTokTokenExpiredError("Access token invalid or expired")
                        elif "rate" in str(code).lower():
                            if attempt < retries:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            raise TikTokRateLimitError("Rate limit exceeded")
                        raise TikTokAPIError(f"TikTok API error: {msg}")

                    data_obj = data.get("data", {})
                    videos = data_obj.get("videos", [])
                    return videos
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < retries:
                    await asyncio.sleep(1)
                    continue
                log.error("TikTok get_user_videos connection error: %s", type(exc).__name__)
                raise TikTokAPIError(f"Network error fetching videos: {type(exc).__name__}") from exc

        return []

    async def revoke_token(self, token: str) -> bool:
        """Revoke official TikTok OAuth token."""
        if not token:
            return True
        session = await self._get_session()
        payload = {
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "token": token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            async with session.post(TIKTOK_REVOKE_URL, data=payload, headers=headers) as resp:
                log.info("Token revocation response status: %s", resp.status)
                return resp.status == 200
        except Exception as e:
            log.warning("Revoke token network error: %s", type(e).__name__)
            return False


async def get_valid_access_token(
    discord_user_id: int, api_client: Optional[TikTokAPIClient] = None
) -> tuple[Optional[str], Optional[Any]]:
    """Retrieve decrypted valid access token for user, refreshing automatically if expired."""
    from services.db import db

    account = db.get_tiktok_account(discord_user_id)
    if not account:
        return None, None

    if not is_encryption_available():
        log.warning("Encryption unavailable; cannot decrypt access token for user %s", discord_user_id)
        return None, account

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        decrypted_access = decrypt_token(account.access_token)
        decrypted_refresh = decrypt_token(account.refresh_token) if account.refresh_token else ""
    except TikTokSecurityError:
        log.error("Failed to decrypt tokens for user %s due to security error.", discord_user_id)
        return None, account

    # Check if access token is still valid (60s buffer)
    if account.expires_at > now_ts + 60 and decrypted_access:
        return decrypted_access, account

    # Token is expired or expiring soon, try refreshing if refresh_token available
    if decrypted_refresh and (not account.refresh_expires_at or account.refresh_expires_at > now_ts):
        client = api_client or TikTokAPIClient()
        try:
            refresh_res = await client.refresh_access_token(decrypted_refresh)
            new_access = refresh_res.get("access_token")
            new_refresh = refresh_res.get("refresh_token") or decrypted_refresh
            expires_in = refresh_res.get("expires_in", 86400)
            refresh_expires_in = refresh_res.get("refresh_expires_in", 31536000)

            if new_access:
                enc_access = encrypt_token(new_access)
                enc_refresh = encrypt_token(new_refresh)
                updated_account = db.save_tiktok_account(
                    discord_user_id=discord_user_id,
                    tiktok_open_id=account.tiktok_open_id,
                    display_name=account.display_name,
                    access_token=enc_access,
                    refresh_token=enc_refresh,
                    expires_at=now_ts + expires_in,
                    refresh_expires_at=now_ts + refresh_expires_in,
                )
                return new_access, updated_account
        except TikTokTokenExpiredError:
            log.warning("Refresh token expired for Discord user %s", discord_user_id)
        except Exception as exc:
            log.error("Error refreshing token for user %s: %s", discord_user_id, type(exc).__name__)

    return None, account
