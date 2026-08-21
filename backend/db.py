"""SQLite storage. Small, dependency-free, and safe to open from many threads."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(os.environ.get("PWM_DB", Path(__file__).resolve().parent.parent / "data" / "app.db"))

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    age          INTEGER,
    phone        TEXT    DEFAULT '',
    caregiver_name  TEXT DEFAULT '',
    caregiver_phone TEXT DEFAULT '',
    routine      TEXT    NOT NULL DEFAULT '{}',
    language     TEXT    NOT NULL DEFAULT 'en-IN',
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS prescriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,          -- 'image' | 'text'
    doctor      TEXT    DEFAULT '',
    raw_text    TEXT    DEFAULT '',
    image_path  TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS medicines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    prescription_id INTEGER REFERENCES prescriptions(id) ON DELETE SET NULL,
    name            TEXT    NOT NULL,
    form            TEXT    NOT NULL DEFAULT 'tablet',
    strength        TEXT    DEFAULT '',
    dose_qty        REAL    NOT NULL DEFAULT 1,
    dose_unit       TEXT    NOT NULL DEFAULT 'tablet',
    slots           TEXT    NOT NULL DEFAULT '[]',
    slot_qty        TEXT    NOT NULL DEFAULT '{}',
    interval_hours  INTEGER,
    day_interval    INTEGER NOT NULL DEFAULT 1,
    frequency_label TEXT    DEFAULT '',
    food            TEXT    NOT NULL DEFAULT 'any',
    duration_days   INTEGER,
    start_date      TEXT    NOT NULL,
    prn             INTEGER NOT NULL DEFAULT 0,
    instructions    TEXT    DEFAULT '',
    confidence      REAL    DEFAULT 1.0,
    raw_line        TEXT    DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS dose_log (
    dose_id      TEXT PRIMARY KEY,
    medicine_id  INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
    patient_id   INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL,
    acted_at     TEXT,
    snooze_until TEXT,
    note         TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_medicines_patient ON medicines(patient_id, active);
CREATE INDEX IF NOT EXISTS idx_doselog_patient ON dose_log(patient_id, scheduled_at);
"""

JSON_COLUMNS = {"slots", "slot_qty", "routine"}


def connect() -> sqlite3.Connection:
    """One connection per thread; uvicorn runs handlers on a thread pool."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()


def reset_connection() -> None:
    """Drop this thread's handle -- used by the tests when swapping databases."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    rows = connect().execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(row) for row in rows]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    row = connect().execute(sql, tuple(params)).fetchone()
    return _row_to_dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    conn = connect()
    cursor = conn.execute(sql, tuple(params))
    conn.commit()
    return cursor.lastrowid


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for column in JSON_COLUMNS & data.keys():
        try:
            data[column] = json.loads(data[column]) if data[column] else ([] if column == "slots" else {})
        except (TypeError, ValueError):
            data[column] = [] if column == "slots" else {}
    if "prn" in data:
        data["prn"] = bool(data["prn"])
    if "active" in data:
        data["active"] = bool(data["active"])
    return data


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
