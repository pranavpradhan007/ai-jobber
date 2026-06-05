"""
Audit and cost logging.
Every action taken on behalf of an application is logged here.
No credential values ever appear in this table (enforced by the caller).
"""
from __future__ import annotations
import json
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


def log_action(
    conn: sqlite3.Connection,
    *,
    action: str,
    application_id: Optional[int] = None,
    model: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    details: Optional[dict] = None,
) -> int:
    """Write one audit_log row. Returns the new row id."""
    details_json = json.dumps(details) if details else None
    cur = conn.execute(
        """
        INSERT INTO audit_log
            (application_id, action, model, input_tokens, output_tokens,
             cost_usd, details)
        VALUES (?,?,?,?,?,?,?)
        """,
        (application_id, action, model, input_tokens, output_tokens,
         cost_usd, details_json),
    )
    conn.commit()
    logger.debug(
        "audit action=%s app_id=%s cost=%.6f usd",
        action, application_id, cost_usd,
    )
    return cur.lastrowid


def get_total_cost(
    conn: sqlite3.Connection,
    application_id: Optional[int] = None,
) -> float:
    """Return cumulative cost in USD for one application or all."""
    if application_id is not None:
        cur = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM audit_log WHERE application_id=?",
            (application_id,),
        )
    else:
        cur = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM audit_log")
    return float(cur.fetchone()[0])
