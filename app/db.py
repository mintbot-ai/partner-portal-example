"""SQLite helpers — single events table, plus a tiny idempotency check.

Schema:

    events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id   TEXT    UNIQUE NOT NULL,   -- X-Mintbot-Event-Id
        event_type TEXT    NOT NULL,          -- X-Mintbot-Event-Type
        payload    TEXT    NOT NULL,          -- raw JSON body
        received_at TEXT   NOT NULL           -- ISO8601 UTC
    )

The UNIQUE constraint on event_id gives us idempotent ingest: MintOffice
retries (e.g. when our /webhooks/mintoffice returned 500 mid-process)
won't create duplicate rows.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import settings


def _connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT UNIQUE NOT NULL,
                event_type  TEXT NOT NULL,
                payload     TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS events_received_at_idx "
            "ON events(received_at DESC)"
        )


def store_event(event_id: str, event_type: str, payload: str) -> bool:
    """Insert a webhook event. Returns True on first insert, False if the
    event_id already exists (idempotent replay)."""
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO events (event_id, event_type, payload, received_at) "
                "VALUES (?, ?, ?, ?)",
                (event_id, event_type, payload, received_at),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def list_events(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, event_id, event_type, payload, received_at "
            "FROM events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
