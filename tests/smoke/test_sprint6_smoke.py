"""
Sprint 6 smoke:
  Feed fixture threads (rejection, OA, interview, positive_reply) →
  correct event types → red/green statuses → positive triggers notification.
"""
import json
import os
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, get_application, update_application
from src.db.state_machine import transition
from src.gmail.client import MockGmailClient
from src.monitoring.classifier import keyword_classifier
from src.monitoring.processor import process_inbound_email

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _load_fixture(thread_id):
    with open(os.path.join(FIXTURES_DIR, "fake_gmail_threads.json"), encoding="utf-8") as fh:
        threads = json.load(fh)
    return next(t for t in threads if t["thread_id"] == thread_id)


def _make_monitoring_app(conn, url_suffix=""):
    job = create_job(conn, source="test",
                     url=f"https://s6.test/{url_suffix}")
    app = create_application(conn, job_id=job.id)
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING",
               "READY_TO_SUBMIT", "SUBMITTING", "SUBMITTED", "MONITORING"]:
        if to == "SUBMITTING":
            update_application(conn, app.id, verifier_passed=1, auto_safe=1)
        transition(conn, app.id, to)
    return get_application(conn, app.id)


@pytest.mark.smoke
def test_all_fixture_types_classify_correctly(tmp_path):
    db_path = str(tmp_path / "sprint6.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))

    cases = [
        ("thread_rejection_001",  "rejection",      "red"),
        ("thread_oa_001",         "oa",             "yellow"),
        ("thread_interview_001",  "interview",      "green"),
        ("thread_positive_001",   "positive_reply", "green"),
    ]

    for thread_id, expected_type, expected_color in cases:
        app = _make_monitoring_app(conn, url_suffix=thread_id)
        thread = _load_fixture(thread_id)
        gmail = MockGmailClient()

        pr = process_inbound_email(
            conn, app.id,
            thread["subject"], thread["body"],
            classifier=keyword_classifier,
            gmail_client=gmail,
            notification_recipient="user@example.com",
        )

        assert pr.event_type == expected_type, \
            f"thread={thread_id}: expected {expected_type}, got {pr.event_type}"
        assert pr.color_status == expected_color, \
            f"thread={thread_id}: expected color {expected_color}, got {pr.color_status}"

        saved = get_application(conn, app.id)
        assert saved.monitoring_status == expected_type
        assert saved.color_status == expected_color

        # Notification only for interview and positive_reply
        if expected_type in ("interview", "positive_reply"):
            assert pr.notification_sent is True
            assert len(gmail.sent) >= 1
        else:
            assert pr.notification_sent is False

    conn.close()


@pytest.mark.smoke
def test_unknown_low_confidence_preserved(tmp_path):
    db_path = str(tmp_path / "sprint6b.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    app = _make_monitoring_app(conn, url_suffix="unknown_smoke")
    thread = _load_fixture("thread_unknown_001")

    pr = process_inbound_email(
        conn, app.id,
        thread["subject"], thread["body"],
        classifier=keyword_classifier,
    )
    assert pr.event_type == "unknown"
    assert pr.color_status == "gray"

    # raw_excerpt kept in DB
    cur = conn.execute(
        "SELECT raw_excerpt FROM monitoring_events WHERE application_id=?",
        (app.id,),
    )
    row = cur.fetchone()
    assert row["raw_excerpt"] is not None
    conn.close()
