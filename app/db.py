"""SQLite persistence.

Write operations validate the *whole resulting project* inside a single
transaction; on :class:`app.engine.EngineError` the transaction is rolled
back so the saved project is never left half-changed.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional

from .engine import EngineError, Item

DB_PATH = os.environ.get("LIGHTS_DB", "/data/lights.db" if os.path.isdir("/data") else "data/lights.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fixture    TEXT NOT NULL,
    channel    INTEGER NOT NULL,
    target     INTEGER NOT NULL,
    duration   INTEGER NOT NULL,
    start_mode   TEXT NOT NULL,
    start_ms     INTEGER,
    ref_id       INTEGER,
    ref_edge     TEXT,
    ref_offset   INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)


def row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        fixture=row["fixture"],
        channel=row["channel"],
        target=row["target"],
        duration=row["duration"],
        start_mode=row["start_mode"],
        start_ms=row["start_ms"],
        ref_id=row["ref_id"],
        ref_edge=row["ref_edge"],
        ref_offset=row["ref_offset"],
    )


def load_items(conn: sqlite3.Connection, project_id: int) -> List[Item]:
    rows = conn.execute(
        "SELECT * FROM items WHERE project_id = ? ORDER BY sort_order, id",
        (project_id,),
    ).fetchall()
    return [row_to_item(r) for r in rows]


def project_exists(conn: sqlite3.Connection, project_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)
    ).fetchone() is not None


def create_project(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("INSERT INTO projects(name) VALUES (?)", (name,))
    return cur.lastrowid


def list_projects(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY id").fetchall()


def project_name(conn: sqlite3.Connection, project_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return row["name"] if row else None


def next_sort_order(conn: sqlite3.Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM items WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return row["n"]


def insert_item(conn: sqlite3.Connection, project_id: int, it: Item,
                sort_order: int) -> int:
    cur = conn.execute(
        """INSERT INTO items
           (project_id, fixture, channel, target, duration, start_mode,
            start_ms, ref_id, ref_edge, ref_offset, sort_order)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, it.fixture, it.channel, it.target, it.duration,
         it.start_mode, it.start_ms, it.ref_id, it.ref_edge, it.ref_offset,
         sort_order),
    )
    return cur.lastrowid


def update_item(conn: sqlite3.Connection, project_id: int, it: Item) -> None:
    cur = conn.execute(
        """UPDATE items SET fixture=?, channel=?, target=?, duration=?,
              start_mode=?, start_ms=?, ref_id=?, ref_edge=?, ref_offset=?
           WHERE id=? AND project_id=?""",
        (it.fixture, it.channel, it.target, it.duration, it.start_mode,
         it.start_ms, it.ref_id, it.ref_edge, it.ref_offset,
         it.id, project_id),
    )
    if cur.rowcount == 0:
        raise EngineError("提示项不存在")


def delete_item_row(conn: sqlite3.Connection, project_id: int,
                    item_id: int) -> None:
    cur = conn.execute(
        "DELETE FROM items WHERE id=? AND project_id=?", (item_id, project_id)
    )
    if cur.rowcount == 0:
        raise EngineError("提示项不存在")


def is_referenced(conn: sqlite3.Connection, project_id: int,
                  item_id: int) -> List[int]:
    rows = conn.execute(
        "SELECT id FROM items WHERE project_id=? AND ref_id=? ORDER BY id",
        (project_id, item_id),
    ).fetchall()
    return [r["id"] for r in rows]
