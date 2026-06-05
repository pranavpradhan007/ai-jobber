"""
Unit tests for Sprint 6: monitoring classifier, processor, notifications.
All Gmail mocked. Fixture threads used for deterministic classification.
"""
import json
import os
import pytest

from src.db.jobs import create_job
from src.db.applications import create_application, get_application, update_application
from src.db.state_machine import transition
from src.gmail.client import MockGmailClient, GmailMessage
from src.monitoring.classifier import (
    keyword_classifier,
    classify_email,
    ClassificationResult,
    CONFIDENCE_THRESHOLD,
)
from src.monitoring.processor import (
    process_inbound_email,
    ProcessResult,
    COLOR_MAP,
    NOTIFY_EVENTS,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


# ── Load fixture threads ─────────────────────────────────────────────────────

def _load_fixtures():
    with open(os.path.join(FIXTURES_DIR, "fake_gmail_threads.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_by_type(event_type: str) -> dict:
    """Return the fixture thread corresponding to expected event_type."""
    mapping = {
        "rejection":      "thread_rejection_001",
        "oa":             "thread_oa_001",
        "interview":      "thread_interview_001",
        "positive_reply": "thread_positive_001",
        "receipt":        "thread_receipt_001",
        "unknown":        "thread_unknown_001",
    }
    threads = {t["thread_id"]: t for t in _load_fixtures()}
    return threads[mapping[event_type]]


def _make_monitoring_app(conn, url_suffix=""):
    job = create_job(conn, source="test",
                     url=f"https://mon.test/{url_suffix or id(conn)}")
    app = create_application(conn, job_id=job.id)
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING",
               "READY_TO_SUBMIT", "SUBMITTING", "SUBMITTED", "MONITORING"]:
        if to == "SUBMITTING":
            update_application(conn, app.id, verifier_passed=1, auto_safe=1)
        transition(conn, app.id, to)
    return get_application(conn, app.id)


# ── 1. Classifier: fixture threads classify correctly ────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("expected_type", [
    "rejection", "oa", "interview", "positive_reply", "receipt"
])
def test_keyword_classifier_fixture(expected_type):
    thread = _fixture_by_type(expected_type)
    result = keyword_classifier(thread["subject"], thread["body"])
    assert result.event_type == expected_type, (
        f"Expected {expected_type}, got {result.event_type} "
        f"for subject={thread['subject']!r}"
    )
    assert result.confidence >= CONFIDENCE_THRESHOLD


@pytest.mark.unit
def test_low_confidence_becomes_unknown():
    """classify_email must downgrade low-confidence results to unknown."""
    def always_low(s, b):
        return ClassificationResult("rejection", 0.3, "excerpt")
    result = classify_email("some subject", "some body", classifier=always_low)
    assert result.event_type == "unknown"


@pytest.mark.unit
def test_unknown_fixture_classified_as_unknown():
    thread = _fixture_by_type("unknown")
    result = classify_email(thread["subject"], thread["body"],
                            classifier=keyword_classifier)
    assert result.event_type == "unknown"


# ── 2. Processor: correct event type + status/color set ──────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("expected_type,expected_color", [
    ("rejection",      "red"),
    ("oa",             "yellow"),
    ("interview",      "green"),
    ("positive_reply", "green"),
    ("receipt",        "gray"),
    ("unknown",        "gray"),
])
def test_processor_sets_color(tmp_db, expected_type, expected_color):
    app = _make_monitoring_app(tmp_db, f"col_{expected_type}")
    thread = _fixture_by_type(expected_type)

    pr = process_inbound_email(
        tmp_db, app.id,
        thread["subject"], thread["body"],
        classifier=keyword_classifier,
    )
    assert pr.event_type == expected_type
    assert pr.color_status == expected_color

    saved = get_application(tmp_db, app.id)
    assert saved.color_status == expected_color
    assert saved.monitoring_status == expected_type


@pytest.mark.unit
def test_processor_writes_monitoring_event(tmp_db):
    app = _make_monitoring_app(tmp_db, "ev1")
    thread = _fixture_by_type("rejection")
    process_inbound_email(tmp_db, app.id, thread["subject"], thread["body"],
                          classifier=keyword_classifier)

    cur = tmp_db.execute(
        "SELECT * FROM monitoring_events WHERE application_id=?", (app.id,)
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "rejection"
    assert rows[0]["confidence"] >= CONFIDENCE_THRESHOLD


# ── 3. Low-confidence → unknown with raw_excerpt preserved ───────────────────

@pytest.mark.unit
def test_unknown_keeps_raw_excerpt(tmp_db):
    app = _make_monitoring_app(tmp_db, "unk1")
    process_inbound_email(
        tmp_db, app.id,
        "Random subject", "No keywords here at all.",
        classifier=keyword_classifier,
    )
    cur = tmp_db.execute(
        "SELECT raw_excerpt, event_type FROM monitoring_events WHERE application_id=?",
        (app.id,),
    )
    row = cur.fetchone()
    assert row["event_type"] == "unknown"
    assert row["raw_excerpt"] is not None
    assert len(row["raw_excerpt"]) > 0


# ── 4. State transitions on positive events ──────────────────────────────────

@pytest.mark.unit
def test_interview_advances_to_interview_scheduled(tmp_db):
    app = _make_monitoring_app(tmp_db, "int1")
    thread = _fixture_by_type("interview")
    process_inbound_email(tmp_db, app.id, thread["subject"], thread["body"],
                          classifier=keyword_classifier)
    saved = get_application(tmp_db, app.id)
    assert saved.state == "INTERVIEW_SCHEDULED"


@pytest.mark.unit
def test_oa_advances_to_oa_received(tmp_db):
    app = _make_monitoring_app(tmp_db, "oa1")
    thread = _fixture_by_type("oa")
    process_inbound_email(tmp_db, app.id, thread["subject"], thread["body"],
                          classifier=keyword_classifier)
    saved = get_application(tmp_db, app.id)
    assert saved.state == "OA_RECEIVED"


@pytest.mark.unit
def test_rejection_advances_to_rejected(tmp_db):
    app = _make_monitoring_app(tmp_db, "rej1")
    thread = _fixture_by_type("rejection")
    process_inbound_email(tmp_db, app.id, thread["subject"], thread["body"],
                          classifier=keyword_classifier)
    saved = get_application(tmp_db, app.id)
    assert saved.state == "REJECTED"


@pytest.mark.unit
def test_positive_reply_stays_in_monitoring(tmp_db):
    app = _make_monitoring_app(tmp_db, "pos1")
    thread = _fixture_by_type("positive_reply")
    process_inbound_email(tmp_db, app.id, thread["subject"], thread["body"],
                          classifier=keyword_classifier)
    saved = get_application(tmp_db, app.id)
    # positive_reply does not have a target state — stays in MONITORING
    assert saved.state == "MONITORING"
    assert saved.monitoring_status == "positive_reply"


# ── 5. Notifications: only on interview + positive_reply ─────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("event_type,should_notify", [
    ("interview",      True),
    ("positive_reply", True),
    ("oa",             False),
    ("rejection",      False),
    ("receipt",        False),
    ("unknown",        False),
])
def test_notification_only_on_positive_events(tmp_db, event_type, should_notify):
    app = _make_monitoring_app(tmp_db, f"notif_{event_type}")
    thread = _fixture_by_type(event_type)
    gmail = MockGmailClient()

    pr = process_inbound_email(
        tmp_db, app.id,
        thread["subject"], thread["body"],
        classifier=keyword_classifier,
        gmail_client=gmail,
        notification_recipient="user@example.com",
    )
    assert pr.notification_sent == should_notify
    if should_notify:
        assert len(gmail.sent) == 1
        assert event_type.replace("_", " ") in gmail.sent[0]["subject"].lower() or \
               event_type in gmail.sent[0]["subject"].lower()
    else:
        assert len(gmail.sent) == 0


@pytest.mark.unit
def test_no_notification_without_gmail_client(tmp_db):
    """Notification silently skipped if no gmail_client provided."""
    app = _make_monitoring_app(tmp_db, "nonotif1")
    thread = _fixture_by_type("interview")
    pr = process_inbound_email(
        tmp_db, app.id,
        thread["subject"], thread["body"],
        classifier=keyword_classifier,
        gmail_client=None,
        notification_recipient="user@example.com",
    )
    assert pr.notification_sent is False
