"""
Unit tests for src/queues/queue.py
Covers: enqueue, acquire_lease, release (success/failure), backoff,
        dead-letter, lease expiry reclaim.
"""
import time
import pytest

from src.queues.queue import (
    enqueue,
    acquire_lease,
    release_lease,
    reclaim_expired,
    pending_count,
)
from src.db.jobs import create_job
from src.db.applications import create_application


def _seed(conn):
    job = create_job(conn, source="test", url=f"https://q.test/{id(conn)}")
    app = create_application(conn, job_id=job.id)
    return app


# ── 1. Basic enqueue + acquire ───────────────────────────────────────────────

@pytest.mark.unit
def test_enqueue_and_acquire(tmp_db):
    app = _seed(tmp_db)
    item = enqueue(tmp_db, application_id=app.id, task_type="score")
    assert item.state == "pending"

    claimed = acquire_lease(tmp_db, "score", worker_id="worker1")
    assert claimed is not None
    assert claimed.id == item.id
    assert claimed.state == "running"
    assert claimed.lease_owner == "worker1"
    assert claimed.attempts == 1


@pytest.mark.unit
def test_queue_empty_returns_none(tmp_db):
    result = acquire_lease(tmp_db, "score")
    assert result is None


@pytest.mark.unit
def test_task_type_isolation(tmp_db):
    """Acquiring 'tailor' should not pick up a 'score' item."""
    app = _seed(tmp_db)
    enqueue(tmp_db, application_id=app.id, task_type="score")
    result = acquire_lease(tmp_db, "tailor")
    assert result is None


# ── 2. Release — success path ─────────────────────────────────────────────────

@pytest.mark.unit
def test_release_success(tmp_db):
    app = _seed(tmp_db)
    enqueue(tmp_db, application_id=app.id, task_type="score")
    item = acquire_lease(tmp_db, "score")
    release_lease(tmp_db, item.id, success=True)

    cur = tmp_db.execute("SELECT state FROM work_queue WHERE id=?", (item.id,))
    assert cur.fetchone()["state"] == "done"
    assert pending_count(tmp_db, "score") == 0


# ── 3. Release — failure with backoff ────────────────────────────────────────

@pytest.mark.unit
def test_release_failure_increments_attempts_and_reschedules(tmp_db):
    app = _seed(tmp_db)
    enqueue(tmp_db, application_id=app.id, task_type="score", max_attempts=3)
    item = acquire_lease(tmp_db, "score")
    release_lease(tmp_db, item.id, success=False)

    cur = tmp_db.execute("SELECT * FROM work_queue WHERE id=?", (item.id,))
    row = cur.fetchone()
    # After 1 attempt it should be pending again (not dead) with a future available_at
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["available_at"] > row["created_at"]


# ── 4. Dead-letter after max_attempts ────────────────────────────────────────

@pytest.mark.unit
def test_dead_letter_after_max_attempts(tmp_db):
    app = _seed(tmp_db)
    enqueue(tmp_db, application_id=app.id, task_type="score", max_attempts=2)

    for _ in range(2):
        # Force available_at to now so we can re-acquire immediately
        tmp_db.execute(
            "UPDATE work_queue SET available_at = datetime('now', '-1 second') "
            "WHERE application_id=? AND task_type='score'",
            (app.id,),
        )
        tmp_db.commit()
        item = acquire_lease(tmp_db, "score")
        assert item is not None
        release_lease(tmp_db, item.id, success=False)

    cur = tmp_db.execute(
        "SELECT state FROM work_queue WHERE application_id=? AND task_type='score'",
        (app.id,),
    )
    assert cur.fetchone()["state"] == "dead"


# ── 5. Lease expiry reclaim ───────────────────────────────────────────────────

@pytest.mark.unit
def test_reclaim_expired_lease(tmp_db):
    app = _seed(tmp_db)
    enqueue(tmp_db, application_id=app.id, task_type="score")
    item = acquire_lease(tmp_db, "score", lease_seconds=1)
    assert item.state == "running"

    # Force lease to be expired
    tmp_db.execute(
        "UPDATE work_queue SET lease_expires_at = datetime('now', '-10 seconds') WHERE id=?",
        (item.id,),
    )
    tmp_db.commit()

    reclaimed = reclaim_expired(tmp_db)
    assert reclaimed == 1

    cur = tmp_db.execute("SELECT state FROM work_queue WHERE id=?", (item.id,))
    assert cur.fetchone()["state"] == "pending"


@pytest.mark.unit
def test_active_lease_not_reclaimed(tmp_db):
    """An item with a future lease_expires_at must not be reclaimed."""
    app = _seed(tmp_db)
    enqueue(tmp_db, application_id=app.id, task_type="score")
    item = acquire_lease(tmp_db, "score", lease_seconds=3600)
    assert item.state == "running"

    reclaimed = reclaim_expired(tmp_db)
    assert reclaimed == 0

    cur = tmp_db.execute("SELECT state FROM work_queue WHERE id=?", (item.id,))
    assert cur.fetchone()["state"] == "running"


# ── 6. Pending count ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_pending_count(tmp_db):
    app = _seed(tmp_db)
    assert pending_count(tmp_db) == 0
    enqueue(tmp_db, application_id=app.id, task_type="score")
    enqueue(tmp_db, application_id=app.id, task_type="tailor")
    assert pending_count(tmp_db) == 2
    assert pending_count(tmp_db, "score") == 1
    assert pending_count(tmp_db, "tailor") == 1
