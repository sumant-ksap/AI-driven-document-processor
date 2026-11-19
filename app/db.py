"""SQLite store standing in for the YMS master data and visit tables.

Swap these functions for calls to the real YMS API/database when integrating.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("YMS_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "yms.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS carriers (
    scac TEXT PRIMARY KEY, name TEXT NOT NULL, usdot_number TEXT, mc_number TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, license_number TEXT, license_state TEXT,
    carrier_scac TEXT
);
CREATE TABLE IF NOT EXISTS trailers (
    trailer_number TEXT PRIMARY KEY, carrier_scac TEXT, license_plate TEXT
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY, bol_number TEXT, po_numbers TEXT, carrier_scac TEXT,
    expected_date TEXT, status TEXT NOT NULL DEFAULT 'scheduled'
);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'checked_in',
    trailer_number TEXT, tractor_number TEXT, bol_number TEXT, seal_number TEXT,
    driver_license TEXT, carrier_scac TEXT, fields_json TEXT NOT NULL,
    reviewed_by TEXT, corrections_json TEXT, line_items_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_visits_trailer ON visits(trailer_number);
CREATE INDEX IF NOT EXISTS ix_visits_bol ON visits(bol_number);
"""


@contextmanager
def connect(path: Path | None = None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    """Add columns introduced after a database was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(visits)")}
    if "line_items_json" not in cols:
        conn.execute("ALTER TABLE visits ADD COLUMN line_items_json TEXT")


def rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def create_visit(conn, fields: dict[str, str | None], reviewed_by: str | None,
                 corrections: dict | None, line_items: list[dict] | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO visits (created_at, trailer_number, tractor_number, bol_number, seal_number,
               driver_license, carrier_scac, fields_json, reviewed_by, corrections_json, line_items_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            fields.get("trailer_number"), fields.get("tractor_number"), fields.get("bol_number"),
            fields.get("seal_number"), fields.get("driver_license"), fields.get("carrier_scac"),
            json.dumps(fields), reviewed_by, json.dumps(corrections or {}), json.dumps(line_items or []),
        ),
    )
    return cur.lastrowid


def seed_demo_data(conn) -> None:
    """Small sample master-data set so validation has something to match against."""
    if conn.execute("SELECT COUNT(*) FROM carriers").fetchone()[0]:
        return
    conn.executemany(
        "INSERT INTO carriers (scac, name, usdot_number, mc_number, active) VALUES (?,?,?,?,?)",
        [
            ("RDWY", "Roadway Freight Lines", "1234567", "MC-123456", 1),
            ("SWFT", "Swift Haul Transport", "2345678", "MC-234567", 1),
            ("OLDX", "Old Express Inc", "3456789", "MC-345678", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO drivers (name, license_number, license_state, carrier_scac) VALUES (?,?,?,?)",
        [("John A. Smith", "S1234567", "TX", "RDWY"), ("Maria Lopez", "L7654321", "CA", "SWFT")],
    )
    conn.executemany(
        "INSERT INTO trailers (trailer_number, carrier_scac, license_plate) VALUES (?,?,?)",
        [("RDWY53001", "RDWY", "TRL1234"), ("SWFT48812", "SWFT", "TRL9876")],
    )
    conn.executemany(
        "INSERT INTO appointments (bol_number, po_numbers, carrier_scac, expected_date) VALUES (?,?,?,?)",
        [("BOL-778812", "PO-4471,PO-4472", "RDWY", datetime.now().date().isoformat()),
         ("BOL-990011", "PO-5100", "SWFT", datetime.now().date().isoformat())],
    )
