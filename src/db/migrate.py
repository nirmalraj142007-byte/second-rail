"""Schema migration and connection helpers.

This is the only module in the codebase that imports sqlite3 for schema
management. src/db/repo.py is the only other module that talks to SQLite
at all — everything else goes through repo.py's typed helpers.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def migrate(db_path: Path) -> list[str]:
    """Create every table and index in schema.sql. Idempotent — running
    this twice creates nothing new and returns the same table list."""
    conn = get_connection(db_path)
    try:
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)
        conn.commit()
        return _table_names(conn)
    finally:
        conn.close()


def db_check(db_path: Path) -> dict[str, int]:
    """Return {table_name: row_count} for every table in the schema."""
    conn = get_connection(db_path)
    try:
        counts: dict[str, int] = {}
        for name in _table_names(conn):
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
            counts[name] = row["n"]
        return counts
    finally:
        conn.close()


def _main(argv: list[str]) -> None:
    from src.config import load_settings

    db_path = load_settings().db_path

    if "--check" in argv:
        counts = db_check(db_path)
        width = max((len(name) for name in counts), default=5)
        print(f"{'TABLE':<{width}} ROWS")
        for name, count in counts.items():
            print(f"{name:<{width}} {count}")
        return

    tables = migrate(db_path)
    print(f"migrated {db_path}: {len(tables)} tables")
    for name in tables:
        print(f"  - {name}")


if __name__ == "__main__":
    _main(sys.argv[1:])
