"""SQLite helpers — single events table, plus idempotency + pagination.

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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS events_type_idx ON events(event_type)"
        )


def healthcheck() -> bool:
    """Cheap read+write smoke test for /healthz. Returns True on success."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


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


def list_events(
    *,
    limit: int = 100,
    offset: int = 0,
    event_type: str | None = None,
) -> list[dict]:
    where = ""
    params: list = []
    if event_type:
        where = "WHERE event_type = ? "
        params.append(event_type)
    params.extend([int(limit), int(offset)])
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, event_id, event_type, payload, received_at "
            f"FROM events {where}ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def count_events(*, event_type: str | None = None) -> int:
    with _connect() as conn:
        if event_type:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE event_type = ?",
                (event_type,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"]) if row else 0


def known_event_types() -> list[str]:
    """Distinct event_type values seen so far — drives the /admin filter."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT event_type FROM events ORDER BY event_type"
        ).fetchall()
        return [r["event_type"] for r in rows]
