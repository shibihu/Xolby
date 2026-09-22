"""SQLite database helper for persistent storage of warnings, reminders, channel lock states, and live trackers.

Thread-safe database operations using sqlite3 for local persistence.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

log = logging.getLogger(__name__)

DB_PATH = Path("bot_data.db")


@dataclass(frozen=True)
class WarningRecord:
    id: int
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    timestamp: str


@dataclass(frozen=True)
class ReminderRecord:
    id: int
    guild_id: int
    channel_id: int
    user_id: int
    message: str
    remind_at: str
    completed: bool


@dataclass(frozen=True)
class LiveTrackerRecord:
    id: int
    guild_id: int
    channel_id: int
    message_id: int
    enabled: bool
    created_at: str
    updated_at: str


class Database:
    """Thread-safe SQLite database manager."""

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create necessary tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_locks (
                    channel_id INTEGER PRIMARY KEY,
                    previous_send_messages INTEGER
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS popular_game_live_trackers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL UNIQUE,
                    message_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        log.info("Initialized database at %s", self.db_path)

    # ---------------------------------------------------------------------------
    # Warnings
    # ---------------------------------------------------------------------------
    def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> WarningRecord:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO warnings (guild_id, user_id, moderator_id, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, moderator_id, reason, now_iso),
            )
            conn.commit()
            record_id = cursor.lastrowid

        return WarningRecord(
            id=record_id,
            guild_id=guild_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason=reason,
            timestamp=now_iso,
        )

    def get_warnings(self, guild_id: int, user_id: int) -> list[WarningRecord]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, guild_id, user_id, moderator_id, reason, timestamp
                FROM warnings
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC
                """,
                (guild_id, user_id),
            )
            rows = cursor.fetchall()

        return [
            WarningRecord(
                id=row["id"],
                guild_id=row["guild_id"],
                user_id=row["user_id"],
                moderator_id=row["moderator_id"],
                reason=row["reason"],
                timestamp=row["timestamp"],
            )
            for row in rows
        ]

    # ---------------------------------------------------------------------------
    # Reminders
    # ---------------------------------------------------------------------------
    def add_reminder(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        message: str,
        remind_at: datetime.datetime,
    ) -> ReminderRecord:
        remind_at_iso = remind_at.astimezone(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO reminders (guild_id, channel_id, user_id, message, remind_at, completed)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (guild_id, channel_id, user_id, message, remind_at_iso),
            )
            conn.commit()
            record_id = cursor.lastrowid

        return ReminderRecord(
            id=record_id,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            message=message,
            remind_at=remind_at_iso,
            completed=False,
        )

    def get_due_reminders(self) -> list[ReminderRecord]:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, guild_id, channel_id, user_id, message, remind_at, completed
                FROM reminders
                WHERE completed = 0 AND remind_at <= ?
                """,
                (now_iso,),
            )
            rows = cursor.fetchall()

        return [
            ReminderRecord(
                id=row["id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                user_id=row["user_id"],
                message=row["message"],
                remind_at=row["remind_at"],
                completed=bool(row["completed"]),
            )
            for row in rows
        ]

    def mark_reminder_completed(self, reminder_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE reminders SET completed = 1 WHERE id = ?", (reminder_id,)
            )
            conn.commit()

    # ---------------------------------------------------------------------------
    # Channel Locks
    # ---------------------------------------------------------------------------
    def save_channel_lock(self, channel_id: int, previous_send_messages: Optional[bool]) -> None:
        val = None if previous_send_messages is None else (1 if previous_send_messages else 0)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO channel_locks (channel_id, previous_send_messages)
                VALUES (?, ?)
                """,
                (channel_id, val),
            )
            conn.commit()

    def get_and_clear_channel_lock(self, channel_id: int) -> tuple[bool, Optional[bool]]:
        """Returns (has_saved_state, previous_send_messages). Removes entry upon retrieval."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT previous_send_messages FROM channel_locks WHERE channel_id = ?",
                (channel_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return False, None

            val = row["previous_send_messages"]
            prev_bool = None if val is None else bool(val)

            cursor.execute("DELETE FROM channel_locks WHERE channel_id = ?", (channel_id,))
            conn.commit()
            return True, prev_bool

    # ---------------------------------------------------------------------------
    # Live Trackers
    # ---------------------------------------------------------------------------
    def add_or_update_live_tracker(self, guild_id: int, channel_id: int, message_id: int) -> LiveTrackerRecord:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO popular_game_live_trackers (guild_id, channel_id, message_id, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    message_id = excluded.message_id,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (guild_id, channel_id, message_id, now_iso, now_iso),
            )
            conn.commit()
            cursor.execute(
                "SELECT id, guild_id, channel_id, message_id, enabled, created_at, updated_at FROM popular_game_live_trackers WHERE channel_id = ?",
                (channel_id,),
            )
            row = cursor.fetchone()

        return LiveTrackerRecord(
            id=row["id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_active_live_trackers(self) -> list[LiveTrackerRecord]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, guild_id, channel_id, message_id, enabled, created_at, updated_at
                FROM popular_game_live_trackers
                WHERE enabled = 1
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()

        return [
            LiveTrackerRecord(
                id=row["id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def remove_live_tracker(self, channel_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM popular_game_live_trackers WHERE channel_id = ?",
                (channel_id,),
            )
            conn.commit()

    def deactivate_live_tracker(self, channel_id: int) -> None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE popular_game_live_trackers SET enabled = 0, updated_at = ? WHERE channel_id = ?",
                (now_iso, channel_id),
            )
            conn.commit()


db = Database()
