"""
Work queue with SQLite-backed lease acquisition.

Design:
  - Lease acquisition uses a single UPDATE + WHERE to avoid races
    (SQLite's WAL mode serialises writes).
  - Lease expiry reclaim: any 'running' item whose lease_expires_at < now()
    is reset to 'pending' with incremented attempts.
  - Exponential backoff: available_at = now + 2^attempts seconds (capped at 1h).
  - Dead-letter: attempts >= max_attempts → state = 'dead'.

No threading is used — this runs in a single overnight process.
"""
from __future__ import annotations
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.db.models import WorkQueueItem

logger = logging.getLogger(__name__)

# Maximum backoff cap in seconds (1 hour)
MAX_BACKOFF_SECONDS = 3600


def enqueue(
    conn: sqlite3.Connection,
    *,
    application_id: int,
    task_type: str,
    payload: Optional[dict] = None,
    max_attempts: int = 3,
    delay_seconds: int = 0,
) -> WorkQueueItem:
    """Add a task to the queue. Returns the new queue item."""
    payload_json = json.dumps(payload) if payload else None
    available_at = _future_ts(delay_seconds)
    cur = conn.execute(
        """
        INSERT INTO work_queue
            (application_id, task_type, payload, state, max_attempts, available_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (application_id, task_type, payload_json, max_attempts, available_at),
    )
    conn.commit()
    item = _get_item(conn, cur.lastrowid)
    logger.info(
        "enqueued task_type=%s app_id=%d queue_id=%d",
        task_type, application_id, item.id,
    )
    return item


def acquire_lease(
    conn: sqlite3.Connection,
    task_type: str,
    *,
    lease_seconds: int = 300,
    worker_id: Optional[str] = None,
) -> Optional[WorkQueueItem]:
    """
    Claim one pending item of `task_type` that is ready now.
    Returns the claimed item, or None if the queue is empty.
    """
    worker_id = worker_id or str(uuid.uuid4())[:8]
    expires_at = _future_ts(lease_seconds)
    now = _now_ts()

    # Reclaim expired leases first
    reclaim_expired(conn)

    # Find and lock one item atomically
    cur = conn.execute(
        """
        SELECT id FROM work_queue
        WHERE task_type = ?
          AND state = 'pending'
          AND available_at <= ?
        ORDER BY available_at
        LIMIT 1
        """,
        (task_type, now),
    )
    row = cur.fetchone()
    if row is None:
        return None

    item_id = row["id"]
    conn.execute(
        """
        UPDATE work_queue
        SET state = 'running',
            lease_owner = ?,
            lease_expires_at = ?,
            attempts = attempts + 1
        WHERE id = ?
          AND state = 'pending'
        """,
        (worker_id, expires_at, item_id),
    )
    conn.commit()

    item = _get_item(conn, item_id)
    if item.state != "running" or item.lease_owner != worker_id:
        # Another writer claimed it first (shouldn't happen with WAL, but be safe)
        return None

    logger.info(
        "acquired lease queue_id=%d task_type=%s worker=%s attempts=%d",
        item_id, task_type, worker_id, item.attempts,
    )
    return item


def release_lease(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    success: bool,
    result: Optional[dict] = None,
) -> None:
    """
    Release a lease after processing.
    success=True → state='done'
    success=False → reschedule with backoff, or dead-letter if max_attempts reached.
    """
    item = _get_item(conn, item_id)

    if success:
        conn.execute(
            "UPDATE work_queue SET state='done', lease_owner=NULL, "
            "lease_expires_at=NULL WHERE id=?",
            (item_id,),
        )
        logger.info("queue_id=%d completed successfully", item_id)
    else:
        if item.attempts >= item.max_attempts:
            conn.execute(
                "UPDATE work_queue SET state='dead', lease_owner=NULL, "
                "lease_expires_at=NULL WHERE id=?",
                (item_id,),
            )
            logger.warning(
                "queue_id=%d dead-lettered after %d attempts",
                item_id, item.attempts,
            )
        else:
            backoff = min(2 ** item.attempts, MAX_BACKOFF_SECONDS)
            available_at = _future_ts(backoff)
            conn.execute(
                """
                UPDATE work_queue
                SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                    available_at=?
                WHERE id=?
                """,
                (available_at, item_id),
            )
            logger.warning(
                "queue_id=%d failed (attempt %d/%d), retry in %ds",
                item_id, item.attempts, item.max_attempts, backoff,
            )
    conn.commit()


def reclaim_expired(conn: sqlite3.Connection) -> int:
    """
    Reset any 'running' items whose leases have expired back to 'pending'.
    Returns the number of items reclaimed.
    """
    now = _now_ts()
    cur = conn.execute(
        """
        UPDATE work_queue
        SET state='pending', lease_owner=NULL, lease_expires_at=NULL
        WHERE state='running'
          AND lease_expires_at < ?
        """,
        (now,),
    )
    conn.commit()
    count = cur.rowcount
    if count:
        logger.warning("reclaimed %d expired lease(s)", count)
    return count


def pending_count(
    conn: sqlite3.Connection,
    task_type: Optional[str] = None,
) -> int:
    """Count pending (not yet done/dead) items."""
    if task_type:
        cur = conn.execute(
            "SELECT COUNT(*) FROM work_queue WHERE state='pending' AND task_type=?",
            (task_type,),
        )
    else:
        cur = conn.execute(
            "SELECT COUNT(*) FROM work_queue WHERE state='pending'"
        )
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _get_item(conn: sqlite3.Connection, item_id: int) -> WorkQueueItem:
    cur = conn.execute("SELECT * FROM work_queue WHERE id = ?", (item_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"WorkQueueItem {item_id} not found")
    return WorkQueueItem.from_row(row)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _future_ts(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")
