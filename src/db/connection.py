"""
DB connection bootstrap.
Sprint 0: minimal — just enough to let the test harness boot and run the schema.
Sprint 1 will expand this into the full typed accessor layer.
"""
import sqlite3
import os
from pathlib import Path


def init_db(db_path: str, schema_path: "str | None" = None) -> sqlite3.Connection:
    """Create (or open) a SQLite DB and apply schema.sql if tables are absent."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    if schema_path is None:
        schema_path = _default_schema_path()

    _apply_schema(conn, schema_path)
    return conn


def get_connection(db_path: "str | None" = None) -> sqlite3.Connection:
    """Return a connection to the configured database."""
    if db_path is None:
        db_path = os.environ.get("JOB_AGENT_DB", "job_agent.db")
    schema_path = _default_schema_path()
    return init_db(db_path, schema_path)


def _default_schema_path() -> str:
    here = Path(__file__).resolve()
    # Walk up to repo root (contains schema.sql)
    for parent in here.parents:
        candidate = parent / "schema.sql"
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("schema.sql not found in any parent directory")


def _apply_schema(conn: sqlite3.Connection, schema_path: str) -> None:
    """Execute schema.sql using executescript — safe on existing DB (IF NOT EXISTS)."""
    with open(schema_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    # executescript handles multi-statement SQL and commits automatically.
    # It also handles PRAGMA statements correctly.
    conn.executescript(sql)
