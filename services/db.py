"""SQLite database helper for persistent storage of warnings and reminders.

Thread-safe database operations using sqlite3 for local persistence.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

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


class Database:
    """Thread-safe SQLite database manager."""

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

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


db = Database()
