"""Server-side TikTok operations for the Xolby Render backend.

Everything in this module runs on Render only. It owns:

* building the official TikTok authorization URL,
* exchanging/refreshing tokens (via :class:`services.tiktok.TikTokAPIClient`),
* decrypting tokens *in memory* to call the official Display API v2,
* revoking tokens on disconnect.

The Discord bot never imports this module and never sees plaintext tokens.
"""

from __future__ import annotations

import datetime
import logging
import os
import urllib.parse
from typing import Any, Optional

from services.tiktok import (
    OFFICIAL_SCOPES,
    TIKTOK_AUTH_URL,
    TikTokAPIClient,
    TikTokAPIError,
    TikTokSecurityError,
    TikTokTokenExpiredError,
    decrypt_token,
    encrypt_token,
    is_encryption_available,
)
from web import oauth_store as oauth_store_module
from web.oauth_store import OAuthStore, TikTokCredential

log = logging.getLogger(__name__)


def mask_secret(value: str) -> str:
    """Mask a secret for safe diagnostics/logging."""
    if not value or len(value) < 8:
        return "********"
    return value[:4] + "********" + value[-4:]


def is_tiktok_configured() -> tuple[bool, str]:
    """Return ``(configured, reason)`` for the TikTok app credentials."""
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


def get_config_diagnostics() -> dict[str, str]:
    """Safe, masked configuration summary (never exposes secrets)."""
    client_key = (os.getenv("TIKTOK_CLIENT_KEY") or "").strip()
    client_secret = (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.getenv("TIKTOK_REDIRECT_URI") or "").strip()
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    web_api_key = (os.getenv("XOLBY_WEB_API_KEY") or "").strip()

    return {
        "TIKTOK_CLIENT_KEY": (
            mask_secret(client_key) if client_key and "PUT_YOUR" not in client_key else "unconfigured"
        ),
        "TIKTOK_CLIENT_SECRET": "configured" if client_secret and "PUT_YOUR" not in client_secret else "unconfigured",
        "TIKTOK_REDIRECT_URI": redirect_uri if redirect_uri else "unconfigured",
        "ENCRYPTION_AVAILABLE": str(is_encryption_available()),
        "DATABASE_URL": "configured" if database_url else "unconfigured (ephemeral sqlite)",
        "XOLBY_WEB_API_KEY": "configured" if web_api_key else "unconfigured",
    }


def build_authorization_url(state: str) -> str:
    """Build the official TikTok OAuth authorization URL for a state token."""
    params = {
        "client_key": (os.getenv("TIKTOK_CLIENT_KEY") or "").strip(),
        "scope": OFFICIAL_SCOPES,
        "response_type": "code",
        "redirect_uri": (os.getenv("TIKTOK_REDIRECT_URI") or "").strip(),
        "state": state,
    }
    return f"{TIKTOK_AUTH_URL}?{urllib.parse.urlencode(params)}"


def account_summary(credential: Optional[TikTokCredential]) -> Optional[dict[str, Any]]:
    """Return the non-sensitive account fields for a credential record."""
    if credential is None:
        return None
    return {
        "discord_user_id": credential.discord_user_id,
        "tiktok_open_id": credential.tiktok_open_id,
        "display_name": credential.display_name,
        "expires_at": credential.expires_at,
        "refresh_expires_at": credential.refresh_expires_at,
        "created_at": credential.created_at,
        "updated_at": credential.updated_at,
    }


async def get_valid_access_token(
    discord_user_id: int,
    store: Optional[OAuthStore] = None,
    api_client: Optional[TikTokAPIClient] = None,
) -> tuple[Optional[str], Optional[TikTokCredential]]:
    """Return ``(plaintext_access_token, credential)`` for a Discord user.

    Decryption happens here on Render only. Tokens are refreshed automatically
    and re-encrypted before being persisted. The plaintext token is never logged
    or returned over the bot API.
    """
    store = store or oauth_store_module.oauth_store
    credential = store.get_credentials(discord_user_id)
    if not credential:
        return None, None

    if not is_encryption_available():
        log.error("Encryption backend unavailable; cannot decrypt TikTok token.")
        return None, credential

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        access_token = decrypt_token(credential.access_token)
        refresh_token = decrypt_token(credential.refresh_token) if credential.refresh_token else ""
    except TikTokSecurityError:
        log.error("Failed to decrypt TikTok credentials for user %s.", discord_user_id)
        return None, credential

    if credential.expires_at > now_ts + 60 and access_token:
        return access_token, credential

    if refresh_token and (not credential.refresh_expires_at or credential.refresh_expires_at > now_ts):
        client = api_client or TikTokAPIClient()
        try:
            refreshed = await client.refresh_access_token(refresh_token)
            new_access = refreshed.get("access_token")
            new_refresh = refreshed.get("refresh_token") or refresh_token
            expires_in = int(refreshed.get("expires_in", 86400))
            refresh_expires_in = int(refreshed.get("refresh_expires_in", 31536000))

            if new_access:
                updated = store.save_credentials(
                    discord_user_id=discord_user_id,
                    tiktok_open_id=credential.tiktok_open_id,
                    display_name=credential.display_name,
                    access_token=encrypt_token(new_access),
                    refresh_token=encrypt_token(new_refresh),
                    expires_at=now_ts + expires_in,
                    refresh_expires_at=now_ts + refresh_expires_in,
                )
                return new_access, updated
        except TikTokTokenExpiredError:
            log.warning("TikTok refresh token expired for Discord user %s.", discord_user_id)
        except TikTokSecurityError:
            log.error("Failed to encrypt refreshed TikTok token for user %s.", discord_user_id)
        except Exception as exc:  # noqa: BLE001 - never leak token material
            log.error("Error refreshing TikTok token for user %s: %s", discord_user_id, type(exc).__name__)
        finally:
            if api_client is None:
                await client.close()

    return None, credential


async def fetch_account(discord_user_id: int, store: Optional[OAuthStore] = None) -> dict[str, Any]:
    """Return connected/account metadata for a Discord user (no tokens)."""
    store = store or oauth_store_module.oauth_store
    credential = store.get_credentials(discord_user_id)
    if not credential:
        return {"connected": False, "account": None}
    return {"connected": True, "account": account_summary(credential)}


async def fetch_videos(
    discord_user_id: int,
    store: Optional[OAuthStore] = None,
    api_client: Optional[TikTokAPIClient] = None,
) -> dict[str, Any]:
    """Fetch the user's recent TikTok videos (decrypted server-side only)."""
    store = store or oauth_store_module.oauth_store
    credential = store.get_credentials(discord_user_id)
    if not credential:
        return {"status": "not_connected", "videos": [], "account": None}

    access_token, credential = await get_valid_access_token(discord_user_id, store, api_client)
    if not access_token:
        return {
            "status": "reconnect_required",
            "videos": [],
            "account": account_summary(credential),
        }

    client = api_client or TikTokAPIClient()
    try:
        videos = await client.get_user_videos(access_token, max_count=10)
        return {
            "status": "ok",
            "videos": videos,
            "account": account_summary(credential),
        }
    except TikTokTokenExpiredError:
        return {
            "status": "reconnect_required",
            "videos": [],
            "account": account_summary(credential),
        }
    except TikTokAPIError as exc:
        log.warning("TikTok API error while fetching videos: %s", type(exc).__name__)
        return {
            "status": "error",
            "videos": [],
            "account": account_summary(credential),
            "message": "TikTok API request failed.",
        }
    finally:
        if api_client is None:
            await client.close()


async def revoke_and_delete(
    discord_user_id: int,
    store: Optional[OAuthStore] = None,
    api_client: Optional[TikTokAPIClient] = None,
) -> dict[str, Any]:
    """Revoke TikTok tokens server-side and delete stored credentials/sessions."""
    store = store or oauth_store_module.oauth_store
    credential = store.get_credentials(discord_user_id)
    if not credential:
        store.delete_all_for_user(discord_user_id)
        return {"disconnected": False, "revoked": False, "message": "No connected TikTok account."}

    revoked = False
    if is_encryption_available():
        client = api_client or TikTokAPIClient()
        try:
            access_token = decrypt_token(credential.access_token)
            revoked = await client.revoke_token(access_token)
        except TikTokSecurityError:
            log.error("Failed to decrypt token during disconnect for user %s.", discord_user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Token revocation failed during disconnect: %s", type(exc).__name__)
        finally:
            if api_client is None:
                await client.close()

    store.delete_all_for_user(discord_user_id)
    return {"disconnected": True, "revoked": revoked}
