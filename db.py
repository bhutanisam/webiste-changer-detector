"""db.py - tiny helpers for the SQLite database.

Every call opens a fresh connection. That is a bit slower than sharing one
connection, but it is safe when several threads (web server + checker) use it.
"""
import os
import sqlite3
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "detector.db")


def now():
    """Current time (UTC) as text, e.g. 2026-10-16T14:30:00+00:00"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row  # lets us use row["name"]
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        -- One row per watched URL. The URL is UNIQUE so it is only ever
        -- fetched once, no matter how many people watch it.
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            interval_minutes INTEGER NOT NULL,
            watch_selector TEXT DEFAULT '',
            ignore_selector TEXT DEFAULT '',
            sensitivity TEXT DEFAULT 'medium',   -- low / medium / high
            status TEXT DEFAULT 'pending',       -- pending / ok / error / blocked / noisy
            last_error TEXT DEFAULT '',
            last_checked TEXT,
            last_changed TEXT,
            changes_in_a_row INTEGER DEFAULT 0
        );

        -- People who want alerts for a page.
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL,
            discord_webhook TEXT DEFAULT '',
            email TEXT DEFAULT ''
        );

        -- A snapshot is only saved when the content hash changes.
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            screenshot TEXT NOT NULL,            -- file path inside data/
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def query(sql, args=(), one=False):
    """Run a SELECT. Returns a list of rows, or a single row if one=True."""
    conn = get_db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    """Run INSERT / UPDATE / DELETE. Returns the id of the inserted row."""
    conn = get_db()
    cursor = conn.execute(sql, args)
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id


def update(table, row_id, **fields):
    """update('pages', 3, status='ok') -> UPDATE pages SET status='ok' WHERE id=3
    (Column names come from our own code, never from user input.)"""
    columns = ", ".join(f"{name}=?" for name in fields)
    execute(f"UPDATE {table} SET {columns} WHERE id=?", (*fields.values(), row_id))
