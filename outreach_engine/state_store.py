from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Optional


logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self._db_path)
        try:
            connection.row_factory = sqlite3.Row
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_emails (
                    creator_name TEXT NOT NULL,
                    lead_email TEXT NOT NULL,
                    feed_url TEXT,
                    lead_link TEXT,
                    run_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    message_id TEXT,
                    UNIQUE(creator_name, lead_email)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_message TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS follow_ups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_name TEXT NOT NULL,
                    lead_email TEXT NOT NULL,
                    original_subject TEXT,
                    original_body TEXT,
                    original_sent_at TEXT NOT NULL,
                    follow_up_due_at TEXT NOT NULL,
                    follow_up_sent_at TEXT,
                    replied INTEGER NOT NULL DEFAULT 0,
                    sender_account_json TEXT,
                    run_id TEXT,
                    UNIQUE(creator_name, lead_email)
                )
                """
            )

    def set_json(self, key: str, payload: Dict[str, Any]) -> None:
        serialized = json.dumps(payload, ensure_ascii=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kv_store(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, serialized),
            )

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row["value"])

    def mark_sent(
        self,
        creator_name: str,
        lead_email: str,
        feed_url: str,
        lead_link: str,
        run_id: str,
        message_id: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_emails(
                    creator_name, lead_email, feed_url, lead_link, run_id, sent_at, message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    creator_name,
                    lead_email,
                    feed_url,
                    lead_link,
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                    message_id,
                ),
            )

    def already_sent(self, creator_name: str, lead_email: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_emails WHERE creator_name = ? AND lead_email = ?",
                (creator_name, lead_email),
            ).fetchone()
        return row is not None

    def start_run(self, run_id: str, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, status, started_at, completed_at, last_message)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    last_message = excluded.last_message
                """,
                (run_id, "running", now, message),
            )

    def update_run(self, run_id: str, status: str, message: str, completed: bool = False) -> None:
        completed_at = datetime.now(timezone.utc).isoformat() if completed else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    last_message = ?,
                    completed_at = COALESCE(?, completed_at)
                WHERE run_id = ?
                """,
                (status, message, completed_at, run_id),
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "last_message": row["last_message"],
        }

    # ── Follow-up tracking ──────────────────────────────────────────────

    def mark_follow_up_pending(
        self,
        creator_name: str,
        lead_email: str,
        original_subject: str,
        original_body: str,
        follow_up_days: int,
        sender_account_json: Optional[str],
        run_id: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        due = now + timedelta(days=follow_up_days)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO follow_ups(
                    creator_name, lead_email, original_subject, original_body,
                    original_sent_at, follow_up_due_at, sender_account_json, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(creator_name, lead_email) DO UPDATE SET
                    original_subject = excluded.original_subject,
                    original_body = excluded.original_body,
                    original_sent_at = excluded.original_sent_at,
                    follow_up_due_at = excluded.follow_up_due_at,
                    follow_up_sent_at = NULL,
                    replied = 0,
                    sender_account_json = excluded.sender_account_json,
                    run_id = excluded.run_id
                """,
                (
                    creator_name,
                    lead_email,
                    original_subject,
                    original_body,
                    now.isoformat(),
                    due.isoformat(),
                    sender_account_json,
                    run_id,
                ),
            )
        logger.info("Follow-up scheduled: %s -> %s (due %s)", creator_name, lead_email, due.isoformat())

    def get_due_follow_ups(self) -> list:
        """Return follow-ups that are due and not yet sent and not replied."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM follow_ups
                WHERE follow_up_due_at <= ?
                  AND follow_up_sent_at IS NULL
                  AND replied = 0
                """,
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_follow_up_emails(self) -> set:
        """Return all lead_email addresses with pending (unsent, not-replied) follow-ups."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT lead_email FROM follow_ups "
                "WHERE follow_up_sent_at IS NULL AND replied = 0"
            ).fetchall()
        return {row["lead_email"] for row in rows}

    def mark_replied(self, lead_email: str) -> int:
        """Mark all pending follow-ups for a lead email as replied. Returns rows affected."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE follow_ups SET replied = 1 WHERE lead_email = ? AND follow_up_sent_at IS NULL",
                (lead_email,),
            )
        count = cursor.rowcount
        if count:
            logger.info("Marked %d follow-up(s) as replied for %s", count, lead_email)
        return count

    def mark_follow_up_sent(self, follow_up_id: int, message_id: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE follow_ups SET follow_up_sent_at = ? WHERE id = ?",
                (now, follow_up_id),
            )
        logger.info("Follow-up #%d marked as sent", follow_up_id)
