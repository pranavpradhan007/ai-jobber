"""
Typed accessor layer for the `applications` table.
State changes must go through state_machine.transition(), not direct updates.
"""
from __future__ import annotations
import sqlite3
import logging
from typing import Optional

from .models import Application

logger = logging.getLogger(__name__)


def create_application(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    state: str = "DISCOVERED",
) -> Application:
    """Create a new application row in DISCOVERED state."""
    cur = conn.execute(
        "INSERT INTO applications (job_id, state) VALUES (?, ?)",
        (job_id, state),
    )
    conn.commit()
    app_id = cur.lastrowid
    logger.info("created application id=%d job_id=%d", app_id, job_id)
    return get_application(conn, app_id)


def get_application(conn: sqlite3.Connection, app_id: int) -> Application:
    cur = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"Application {app_id} not found")
    return Application.from_row(row)


def get_applications_by_state(
    conn: sqlite3.Connection, state: str
) -> list[Application]:
    cur = conn.execute(
        "SELECT * FROM applications WHERE state = ? ORDER BY created_at",
        (state,),
    )
    return [Application.from_row(r) for r in cur.fetchall()]


def update_application(
    conn: sqlite3.Connection, app_id: int, **fields
) -> Application:
    """
    Update non-state columns (score, paths, flags, etc.).
    Never use this to change `state` — use state_machine.transition() instead.
    """
    if "state" in fields:
        raise ValueError(
            "Use state_machine.transition() to change application state, "
            "not update_application()."
        )
    if not fields:
        return get_application(conn, app_id)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {set_clause} WHERE id = ?", values)
    conn.commit()
    logger.debug("updated application id=%d fields=%s", app_id, list(fields))
    return get_application(conn, app_id)
