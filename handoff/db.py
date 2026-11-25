"""SQLite schema and transaction helpers.

The database is the single source of truth.  Every mutating operation runs in
one ``BEGIN IMMEDIATE`` transaction, so a crash either leaves the previous
state or the whole new state - never a half-released frame or a half-recorded
intake.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = "1"

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shifts (
        shift_no    TEXT PRIMARY KEY,
        start_frame INTEGER NOT NULL,
        opened_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channels (
        shift_no TEXT NOT NULL REFERENCES shifts(shift_no),
        channel  TEXT NOT NULL CHECK (channel IN ('T', 'D')),
        name     TEXT NOT NULL,
        unit     TEXT NOT NULL,
        cal_a    TEXT NOT NULL,
        cal_b    TEXT NOT NULL,
        frozen_at TEXT NOT NULL,
        PRIMARY KEY (shift_no, channel)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shift_state (
        shift_no     TEXT PRIMARY KEY REFERENCES shifts(shift_no),
        loaded       INTEGER NOT NULL DEFAULT 0,
        pos          INTEGER NOT NULL DEFAULT 0,
        pending_ack  INTEGER,
        source_open  INTEGER NOT NULL DEFAULT 1,
        bridge_open  INTEGER NOT NULL DEFAULT 1,
        archive_open INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_frames (
        shift_no    TEXT NOT NULL REFERENCES shifts(shift_no),
        seq         INTEGER NOT NULL,
        frame_no    INTEGER NOT NULL,
        temp_raw    INTEGER,
        disp_raw    INTEGER,
        temp_source TEXT,
        disp_source TEXT,
        loaded_at   TEXT NOT NULL,
        PRIMARY KEY (shift_no, seq),
        UNIQUE (shift_no, frame_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS intakes (
        shift_no     TEXT NOT NULL,
        frame_no     INTEGER NOT NULL,
        channel      TEXT NOT NULL CHECK (channel IN ('T', 'D')),
        raw          INTEGER,
        source       TEXT,
        persisted_at TEXT NOT NULL,
        PRIMARY KEY (shift_no, frame_no, channel)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS releases (
        shift_no    TEXT NOT NULL,
        frame_no    INTEGER NOT NULL,
        released_at TEXT NOT NULL,
        ack_json    TEXT NOT NULL,
        PRIMARY KEY (shift_no, frame_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS archive_items (
        shift_no     TEXT NOT NULL,
        frame_no     INTEGER NOT NULL,
        channel      TEXT NOT NULL CHECK (channel IN ('T', 'D')),
        evidence     TEXT NOT NULL,
        token        TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        PRIMARY KEY (shift_no, frame_no, channel)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        handoff_no TEXT PRIMARY KEY,
        shift_no   TEXT NOT NULL,
        frame_no   INTEGER NOT NULL,
        issued_at  TEXT NOT NULL,
        UNIQUE (shift_no, frame_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claims (
        handoff_no TEXT NOT NULL REFERENCES handoffs(handoff_no),
        channel    TEXT NOT NULL CHECK (channel IN ('T', 'D')),
        token      TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        PRIMARY KEY (handoff_no, channel)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS journal (
        seq       INTEGER PRIMARY KEY AUTOINCREMENT,
        at        TEXT NOT NULL,
        operation TEXT NOT NULL,
        shift_no  TEXT,
        frame_no  INTEGER,
        channel   TEXT,
        detail    TEXT NOT NULL
    )
    """,
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
