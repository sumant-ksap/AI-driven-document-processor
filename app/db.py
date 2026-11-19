"""SQLite store standing in for the YMS master data and visit tables.

Swap these functions for calls to the real YMS API/database when integrating.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- processing | ready | failed | checked_in
    files_json TEXT NOT NULL,        -- [{index, filename, media_type, size, stored}]
    result_json TEXT, error TEXT, visit_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_scans_created ON scans(created_at);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,   -- sha256 of the key; the key itself is never stored
    created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT
);
"""

VISIT_STATUSES = ("checked_in", "checked_out", "deleted")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    for col in ("line_items_json", "scan_id", "updated_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE visits ADD COLUMN {col} TEXT")


def rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# Visit fields copied into their own columns for searching and duplicate checks.
INDEXED_VISIT_FIELDS = ("trailer_number", "tractor_number", "bol_number", "seal_number", "driver_license", "carrier_scac")


def create_visit(conn, fields: dict[str, str | None], reviewed_by: str | None,
                 corrections: dict | None, line_items: list[dict] | None = None,
                 scan_id: str | None = None) -> int:
    ts = now()
    cur = conn.execute(
        """INSERT INTO visits (created_at, updated_at, trailer_number, tractor_number, bol_number, seal_number,
               driver_license, carrier_scac, fields_json, reviewed_by, corrections_json, line_items_json, scan_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, ts, *(fields.get(k) for k in INDEXED_VISIT_FIELDS),
         json.dumps(fields), reviewed_by, json.dumps(corrections or {}), json.dumps(line_items or []), scan_id),
    )
    return cur.lastrowid


def visit_dict(row) -> dict:
    """A visits row with its JSON columns decoded."""
    d = dict(row)
    d["fields"] = json.loads(d.pop("fields_json") or "{}")
    d["line_items"] = json.loads(d.pop("line_items_json") or "[]")
    d["corrections"] = json.loads(d.pop("corrections_json") or "{}")
    return d


def get_visit(conn, visit_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone()
    return visit_dict(row) if row else None


def update_visit(conn, visit_id: int, fields: dict, line_items: list[dict], corrections: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in INDEXED_VISIT_FIELDS)
    conn.execute(
        f"UPDATE visits SET updated_at = ?, fields_json = ?, line_items_json = ?, corrections_json = ?, {sets} WHERE id = ?",
        (now(), json.dumps(fields), json.dumps(line_items), json.dumps(corrections),
         *(fields.get(k) for k in INDEXED_VISIT_FIELDS), visit_id),
    )


def search_visits(conn, *, status: str | None = None, date_from: str | None = None, date_to: str | None = None,
                  trailer_number: str | None = None, bol_number: str | None = None, carrier_scac: str | None = None,
                  include_deleted: bool = False, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    elif not include_deleted:
        where.append("status != 'deleted'")
    if date_from:
        where.append("substr(created_at, 1, 10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(created_at, 1, 10) <= ?")
        args.append(date_to)
    for col, val in (("trailer_number", trailer_number), ("bol_number", bol_number), ("carrier_scac", carrier_scac)):
        if val:
            where.append(f"upper({col}) = upper(?)")
            args.append(val)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM visits {clause}", args).fetchone()[0]
    found = conn.execute(f"SELECT * FROM visits {clause} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, offset])
    return [visit_dict(r) for r in found], total


# ---------------------------------------------------------------- scans


def create_scan(conn, scan_id: str, files: list[dict]) -> None:
    ts = now()
    conn.execute("INSERT INTO scans (id, created_at, updated_at, status, files_json) VALUES (?,?,?,?,?)",
                 (scan_id, ts, ts, "processing", json.dumps(files)))


def finish_scan(conn, scan_id: str, *, result_json: str | None = None, error: str | None = None) -> None:
    conn.execute("UPDATE scans SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE id = ?",
                 ("ready" if error is None else "failed", result_json, error, now(), scan_id))


def mark_scan_checked_in(conn, scan_id: str, visit_id: int) -> None:
    conn.execute("UPDATE scans SET status = 'checked_in', visit_id = ?, updated_at = ? WHERE id = ?",
                 (visit_id, now(), scan_id))


def get_scan(conn, scan_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["files"] = json.loads(d.pop("files_json"))
    raw = d.pop("result_json")
    d["result"] = json.loads(raw) if raw else None
    return d


def fail_interrupted_scans(conn) -> None:
    """Scans still 'processing' at startup were cut off by a restart."""
    conn.execute("UPDATE scans SET status = 'failed', error = 'Server restarted while reading; scan again.', "
                 "updated_at = ? WHERE status = 'processing'", (now(),))


# ---------------------------------------------------------------- API keys


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(conn, name: str) -> tuple[int, str]:
    key = "gsk_" + secrets.token_urlsafe(32)
    cur = conn.execute("INSERT INTO api_keys (name, prefix, key_hash, created_at) VALUES (?,?,?,?)",
                       (name, key[:10], _hash_key(key), now()))
    return cur.lastrowid, key


def check_api_key(conn, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (_hash_key(key),)).fetchone()
    if row:
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), row["id"]))
    return dict(row) if row else None


def list_api_keys(conn) -> list[dict]:
    return rows(conn, "SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_keys ORDER BY id")


def revoke_api_key(conn, key_id: int) -> bool:
    return conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (now(), key_id)).rowcount > 0


# ---------------------------------------------------------------- master data

# table -> (key column, columns, key is auto-numbered). Table and column names come only from here.
MASTER_TABLES = {
    "carriers": ("scac", ["scac", "name", "usdot_number", "mc_number", "active"], False),
    "drivers": ("id", ["id", "name", "license_number", "license_state", "carrier_scac"], True),
    "trailers": ("trailer_number", ["trailer_number", "carrier_scac", "license_plate"], False),
    "appointments": ("id", ["id", "bol_number", "po_numbers", "carrier_scac", "expected_date", "status"], True),
}


def list_master(conn, table: str, limit: int = 500, offset: int = 0) -> tuple[list[dict], int]:
    key, cols, _ = MASTER_TABLES[table]
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return rows(conn, f"SELECT {', '.join(cols)} FROM {table} ORDER BY {key} LIMIT ? OFFSET ?", limit, offset), total


def upsert_master(conn, table: str, record: dict) -> dict:
    """Insert, or update the row with the same key. Returns the stored row."""
    key, cols, auto = MASTER_TABLES[table]
    values = {c: record[c] for c in cols if c in record}
    if auto and values.get(key) is None:
        values.pop(key, None)
        cur = conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                           list(values.values()))
        key_value = cur.lastrowid
    else:
        key_value = values[key]
        updates = {c: v for c, v in values.items() if c != key}
        exists = conn.execute(f"SELECT 1 FROM {table} WHERE {key} = ?", (key_value,)).fetchone()
        if exists and updates:
            conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in updates)} WHERE {key} = ?",
                         [*updates.values(), key_value])
        elif not exists:
            conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                         list(values.values()))
    row = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {key} = ?", (key_value,)).fetchone()
    return dict(row)


def delete_master(conn, table: str, key_value) -> bool:
    key, _, _ = MASTER_TABLES[table]
    return conn.execute(f"DELETE FROM {table} WHERE {key} = ?", (key_value,)).rowcount > 0


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
