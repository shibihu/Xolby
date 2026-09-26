"""Server-side persistence for TikTok OAuth sessions and encrypted credentials.

This module is owned exclusively by the Render web backend. The Discord bot that
runs on Termux never imports it and never stores TikTok credentials locally.

Storage is selected through ``DATABASE_URL``:

* ``postgresql://...`` / ``postgres://...`` -> PostgreSQL (production, Render)
* ``sqlite:///path`` / a plain path          -> SQLite (local development only)

Persisting production OAuth state/tokens in Render's ephemeral filesystem is not
supported: deployments must point ``DATABASE_URL`` at a real database.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: OAuth state/session lifetime in seconds (requirement: short lived, 10 minutes).
STATE_TTL_SECONDS = 600

DEFAULT_SQLITE_PATH = Path("xolby_oauth.db")


@dataclass(frozen=True)
class OAuthSession:
    """A short-lived, single-use OAuth authorization session."""

    state: str
    discord_user_id: int
    created_at: int
    expires_at: int
    consumed: bool


@dataclass(frozen=True)
class TikTokCredential:
    """Encrypted TikTok credentials associated with a Discord user.

    ``access_token`` and ``refresh_token`` are always ciphertext; plaintext
    TikTok tokens are never persisted.
    """

    discord_user_id: int
    tiktok_open_id: str
    display_name: str
    access_token: str
    refresh_token: Optional[str]
    expires_at: int
    refresh_expires_at: Optional[int]
    created_at: int
    updated_at: int


def _sqlite_path_from_url(url: str) -> str:
    """Extract a filesystem path from a sqlite:// style URL."""
    for prefix in ("sqlite:///", "sqlite://", "sqlite:"):
        if url.startswith(prefix):
            remainder = url[len(prefix):]
            return remainder or str(DEFAULT_SQLITE_PATH)
    return url


def _detect_backend(database_url: Optional[str]) -> tuple[str, str]:
    """Return ``(backend, target)`` for the configured database URL."""
    url = (database_url or "").strip()

    if url.startswith("postgres://"):
        # Render/Heroku style URL; psycopg expects the postgresql:// scheme.
        return "postgres", "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgres", url
    if url:
        return "sqlite", _sqlite_path_from_url(url)
    return "sqlite", str(DEFAULT_SQLITE_PATH)


_SCHEMA_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS tiktok_oauth_sessions (
        state TEXT PRIMARY KEY,
        discord_user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0,
        completed_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tiktok_credentials (
        discord_user_id INTEGER PRIMARY KEY,
        tiktok_open_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        expires_at INTEGER NOT NULL,
        refresh_expires_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tiktok_oauth_sessions_user ON tiktok_oauth_sessions(discord_user_id)",
]

_SCHEMA_POSTGRES = [
    """
    CREATE TABLE IF NOT EXISTS tiktok_oauth_sessions (
        state TEXT PRIMARY KEY,
        discord_user_id BIGINT NOT NULL,
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0,
        completed_at BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tiktok_credentials (
        discord_user_id BIGINT PRIMARY KEY,
        tiktok_open_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        expires_at BIGINT NOT NULL,
        refresh_expires_at BIGINT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tiktok_oauth_sessions_user ON tiktok_oauth_sessions(discord_user_id)",
]


class OAuthStore:
    """Persistent store for OAuth sessions and encrypted TikTok credentials."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        if database_url is None:
            database_url = os.getenv("DATABASE_URL", "")
        self.database_url = database_url
        self.backend, self.target = _detect_backend(database_url)
        self._init_db()

    @property
    def is_ephemeral(self) -> bool:
        """True when persistence lives on the local (ephemeral) filesystem."""
        return self.backend == "sqlite"

    # ------------------------------------------------------------------
    # Low level helpers
    # ------------------------------------------------------------------
    def _execute(
        self, sql: str, params: tuple = (), *, fetch: bool = False
    ) -> tuple[list[Any], int]:
        """Run a single statement and return ``(rows, rowcount)``.

        Statements use ``%s`` placeholders and are translated for SQLite. The
        ``tiktok_oauth_sessions`` / ``tiktok_credentials`` schema sticks to the
        common subset that both SQLite and PostgreSQL understand.
        """
        if self.backend == "postgres":
            import psycopg  # imported lazily so SQLite-only environments work
            from psycopg.rows import dict_row

            with psycopg.connect(self.target, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall() if fetch else []
                    return rows, cur.rowcount

        query = sql.replace("%s", "?")
        conn = sqlite3.connect(self.target, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall() if fetch else []
            rowcount = cur.rowcount
            conn.commit()
            return rows, rowcount
        finally:
            conn.close()

    def _init_db(self) -> None:
        schema = _SCHEMA_POSTGRES if self.backend == "postgres" else _SCHEMA_SQLITE
        for statement in schema:
            self._execute(statement)
        if self.is_ephemeral:
            log.warning(
                "OAuthStore is using local SQLite (%s). Set DATABASE_URL to a "
                "persistent database in production: Render's filesystem is ephemeral.",
                self.target,
            )
        else:
            log.info("OAuthStore initialised on %s backend.", self.backend)

    # ------------------------------------------------------------------
    # OAuth sessions / state
    # ------------------------------------------------------------------
    def create_session(
        self, discord_user_id: int, ttl_seconds: int = STATE_TTL_SECONDS
    ) -> OAuthSession:
        """Create a cryptographically random, single-use OAuth session."""
        try:
            user_id = int(discord_user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("discord_user_id must be an integer.") from exc
        if user_id <= 0:
            raise ValueError("discord_user_id must be a positive Discord snowflake.")

        now = int(time.time())
        expires_at = now + int(ttl_seconds)
        state = secrets.token_urlsafe(32)

        self._execute(
            "INSERT INTO tiktok_oauth_sessions "
            "(state, discord_user_id, created_at, expires_at, consumed, completed_at) "
            "VALUES (%s, %s, %s, %s, 0, NULL)",
            (state, user_id, now, expires_at),
        )
        self._purge_expired(now)

        return OAuthSession(
            state=state,
            discord_user_id=user_id,
            created_at=now,
            expires_at=expires_at,
            consumed=False,
        )

    def consume_session(self, state: str) -> Optional[OAuthSession]:
        """Validate and atomically consume a state token.

        Returns ``None`` when the state is unknown, already consumed, or expired.
        The atomic ``UPDATE ... WHERE consumed = 0`` guarantees single use.
        """
        if not state:
            return None

        now = int(time.time())
        _, rowcount = self._execute(
            "UPDATE tiktok_oauth_sessions SET consumed = 1, completed_at = %s "
            "WHERE state = %s AND consumed = 0 AND expires_at > %s",
            (now, state, now),
        )
        if rowcount != 1:
            return None

        rows, _ = self._execute(
            "SELECT state, discord_user_id, created_at, expires_at, consumed "
            "FROM tiktok_oauth_sessions WHERE state = %s",
            (state,),
            fetch=True,
        )
        if not rows:
            return None

        row = rows[0]
        return OAuthSession(
            state=row["state"],
            discord_user_id=int(row["discord_user_id"]),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
            consumed=bool(row["consumed"]),
        )

    def _purge_expired(self, now: int) -> None:
        self._execute("DELETE FROM tiktok_oauth_sessions WHERE expires_at <= %s", (now,))

    def delete_sessions_for_user(self, discord_user_id: int) -> None:
        self._execute(
            "DELETE FROM tiktok_oauth_sessions WHERE discord_user_id = %s",
            (int(discord_user_id),),
        )

    # ------------------------------------------------------------------
    # Encrypted TikTok credentials
    # ------------------------------------------------------------------
    def save_credentials(
        self,
        *,
        discord_user_id: int,
        tiktok_open_id: str,
        display_name: str,
        access_token: str,
        refresh_token: Optional[str],
        expires_at: int,
        refresh_expires_at: Optional[int] = None,
    ) -> TikTokCredential:
        """Insert or update encrypted credentials for a Discord user."""
        if not access_token:
            raise ValueError("access_token ciphertext is required.")
        if not discord_user_id or int(discord_user_id) <= 0:
            raise ValueError("discord_user_id must be a positive Discord snowflake.")

        now = int(time.time())
        user_id = int(discord_user_id)

        self._execute(
            "INSERT INTO tiktok_credentials "
            "(discord_user_id, tiktok_open_id, display_name, access_token, refresh_token, "
            " expires_at, refresh_expires_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (discord_user_id) DO UPDATE SET "
            "  tiktok_open_id = EXCLUDED.tiktok_open_id, "
            "  display_name = EXCLUDED.display_name, "
            "  access_token = EXCLUDED.access_token, "
            "  refresh_token = EXCLUDED.refresh_token, "
            "  expires_at = EXCLUDED.expires_at, "
            "  refresh_expires_at = EXCLUDED.refresh_expires_at, "
            "  updated_at = EXCLUDED.updated_at",
            (
                user_id,
                tiktok_open_id or "",
                display_name or "",
                access_token,
                refresh_token,
                int(expires_at),
                int(refresh_expires_at) if refresh_expires_at else None,
                now,
                now,
            ),
        )

        credential = self.get_credentials(user_id)
        if credential is None:  # pragma: no cover - defensive
            raise RuntimeError("Failed to persist TikTok credentials.")
        return credential

    def get_credentials(self, discord_user_id: int) -> Optional[TikTokCredential]:
        rows, _ = self._execute(
            "SELECT discord_user_id, tiktok_open_id, display_name, access_token, "
            "       refresh_token, expires_at, refresh_expires_at, created_at, updated_at "
            "FROM tiktok_credentials WHERE discord_user_id = %s",
            (int(discord_user_id),),
            fetch=True,
        )
        if not rows:
            return None
        return self._row_to_credential(rows[0])

    @staticmethod
    def _row_to_credential(row: Any) -> TikTokCredential:
        return TikTokCredential(
            discord_user_id=int(row["discord_user_id"]),
            tiktok_open_id=row["tiktok_open_id"] or "",
            display_name=row["display_name"] or "",
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=int(row["expires_at"]),
            refresh_expires_at=(
                int(row["refresh_expires_at"]) if row["refresh_expires_at"] else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def delete_credentials(self, discord_user_id: int) -> bool:
        _, rowcount = self._execute(
            "DELETE FROM tiktok_credentials WHERE discord_user_id = %s",
            (int(discord_user_id),),
        )
        return rowcount > 0

    def delete_all_for_user(self, discord_user_id: int) -> bool:
        """Delete stored credentials *and* any OAuth sessions for a user."""
        removed = self.delete_credentials(discord_user_id)
        self.delete_sessions_for_user(discord_user_id)
        return removed


#: Module level singleton used by the FastAPI backend.
oauth_store = OAuthStore()
