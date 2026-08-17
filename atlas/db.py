"""
Database connection layer.

Local dev/test: SQLite (zero setup, stdlib only) — this is what runs when
DATABASE_URL is unset.

Production (Railway): set the DATABASE_URL env var to a Postgres connection
string and this module switches to psycopg2 automatically. Both paths are
written against the SAME query text — every query in tools.py/ingestion.py
uses '?' placeholders and RETURNING/ON CONFLICT syntax that works
unchanged on modern SQLite (3.35+) and Postgres. The wrapper below only
translates '?' -> '%s' and normalizes the row/cursor interface; it does not
rewrite queries. NOTE: the psycopg2 path is written carefully against
psycopg2's documented behavior but has not been run against a live
Postgres instance in this environment (no network access here) — verify
it against your actual Railway Postgres on first deploy, in particular
the RETURNING id handling in ingestion.ingest_file.
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "atlas.db"
DATABASE_URL = os.environ.get("DATABASE_URL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS stations (
    code        TEXT PRIMARY KEY,
    line_code   TEXT,
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS addresses (
    address      TEXT PRIMARY KEY,
    station_code TEXT REFERENCES stations(code),
    addr_type    TEXT NOT NULL,
    description  TEXT NOT NULL,
    fidelity     TEXT NOT NULL DEFAULT 'SYNTHETIC_CONTEXT'
);

CREATE TABLE IF NOT EXISTS relationships (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_address  TEXT NOT NULL,
    to_address    TEXT NOT NULL,
    rel_type      TEXT NOT NULL,
    condition_expr TEXT,
    description   TEXT
);

CREATE TABLE IF NOT EXISTS fault_codes (
    code         INTEGER PRIMARY KEY,
    station_code TEXT,
    name         TEXT NOT NULL,
    description  TEXT
);

CREATE TABLE IF NOT EXISTS fault_addresses (
    fault_code INTEGER,
    address    TEXT,
    role       TEXT,
    PRIMARY KEY (fault_code, address)
);

CREATE TABLE IF NOT EXISTS live_state (
    address TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    doc_type      TEXT NOT NULL,
    document_kind TEXT,
    line_code     TEXT,
    station_code  TEXT,
    description   TEXT,
    extracted_text TEXT,
    extraction_method TEXT,
    status        TEXT NOT NULL DEFAULT 'pending_review',
    pending_questions TEXT,
    fidelity      TEXT NOT NULL DEFAULT 'SOURCE_DERIVED',
    uploaded_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    confirmed_at  TEXT
);

CREATE TABLE IF NOT EXISTS document_address_refs (
    document_id INTEGER,
    address     TEXT,
    excerpt     TEXT,
    PRIMARY KEY (document_id, address)
);

CREATE TABLE IF NOT EXISTS station_flows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    line_code     TEXT,
    from_station  TEXT,
    to_station    TEXT,
    step_order    INTEGER,
    source_document_id INTEGER,
    description   TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fault_code   INTEGER,
    occurred_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    symptom      TEXT,
    root_cause   TEXT,
    confirmed    INTEGER DEFAULT 0,
    reported_by  TEXT
);
"""


class _SQLiteConn:
    """Thin wrapper so call sites can do conn.execute(...).fetchone()/.fetchall()
    uniformly, matching the Postgres wrapper's interface."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, query, params=()):
        return self._raw.execute(query, params)

    def executemany(self, query, seq):
        return self._raw.executemany(query, seq)

    def executescript(self, script):
        return self._raw.executescript(script)

    def commit(self):
        self._raw.commit()


class _PGCursorResult:
    """Wraps a psycopg2 cursor so .fetchone()/.fetchall() return dict-like
    rows (via RealDictCursor) the same way sqlite3.Row does when passed to
    dict(). Also exposes .lastrowid as None deliberately — call sites must
    use RETURNING id instead (see module docstring)."""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PGConn:
    def __init__(self, url):
        import psycopg2
        import psycopg2.extras
        self._conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, query, params=()):
        cur = self._conn.cursor()
        cur.execute(query.replace("?", "%s"), params)
        return _PGCursorResult(cur)

    def executemany(self, query, seq):
        cur = self._conn.cursor()
        cur.executemany(query.replace("?", "%s"), seq)
        return _PGCursorResult(cur)

    def executescript(self, script):
        cur = self._conn.cursor()
        cur.execute(script)
        return _PGCursorResult(cur)

    def commit(self):
        self._conn.commit()


def get_conn(db_path: Path = DB_PATH):
    if DATABASE_URL:
        return _PGConn(DATABASE_URL)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return _SQLiteConn(conn)


def init_db(db_path: Path = DB_PATH, wipe: bool = True):
    """Local SQLite dev/test setup only. For Postgres/Railway, run
    schema_postgres.sql directly against your database instead — this
    function's inline SCHEMA is SQLite-specific syntax."""
    if wipe and db_path.exists():
        db_path.unlink()
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
