"""
Monitoring processor.

For each inbound email thread:
  1. Classify the email.
  2. Write a monitoring_events row.
  3. Update application.monitoring_status and color_status.
  4. If event is interview or positive_reply → send notification.

Color mapping:
  receipt        → gray
  rejection      → red
  oa             → yellow
  interview      → green
  positive_reply → green
  unknown        → gray
"""
from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.monitoring.classifier import (
    ClassifierFn,
    ClassificationResult,
    classify_email,
    keyword_classifier,
)
from src.db.applications import get_application, update_application
from src.db.state_machine import transition

logger = logging.getLogger(__name__)

# Event types that trigger a user notification
NOTIFY_EVENTS = frozenset({"interview", "positive_reply"})

COLOR_MAP: dict[str, str] = {
    "receipt":        "gray",
    "rejection":      "red",
    "oa":             "yellow",
    "interview":      "green",
    "positive_reply": "green",
    "unknown":        "gray",
}

STATE_MAP: dict[str, Optional[str]] = {
    "interview":      "INTERVIEW_SCHEDULED",
    "oa":             "OA_RECEIVED",
    "rejection":      "REJECTED",
    "positive_reply": None,   # stay in MONITORING
    "receipt":        None,
    "unknown":        None,
}


@dataclass
class ProcessResult:
    application_id: int
    event_type: str
    confidence: float
    color_status: str
    notification_sent: bool = False


def process_inbound_email(
    conn: sqlite3.Connection,
    application_id: int,
    subject: str,
    body: str,
    gmail_thread_id: Optional[str] = None,
    *,
    classifier: Optional[ClassifierFn] = None,
    gmail_client=None,
    notification_recipient: Optional[str] = None,
) -> ProcessResult:
    """
    Process one inbound email for an application.
    Returns a ProcessResult with the classification outcome.
    """
    if classifier is None:
        classifier = keyword_classifier

    # 1. Classify
    result: ClassificationResult = classify_email(
        subject, body, classifier=classifier
    )

    # 2. Write monitoring_events
    conn.execute(
        """
        INSERT INTO monitoring_events
            (application_id, event_type, email_subject, email_snippet,
             confidence, raw_excerpt, gmail_thread_id)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            application_id,
            result.event_type,
            subject[:200],
            body[:200],
            result.confidence,
            result.raw_excerpt if result.event_type == "unknown" else None,
            gmail_thread_id,
        ),
    )
    conn.commit()

    # 3. Update status and color
    color = COLOR_MAP.get(result.event_type, "gray")
    update_application(
        conn, application_id,
        monitoring_status=result.event_type,
        color_status=color,
    )

    # 4. Advance state machine where appropriate
    app = get_application(conn, application_id)
    target_state = STATE_MAP.get(result.event_type)
    if target_state and app.state == "MONITORING":
        try:
            transition(conn, application_id, target_state,
                       reason=f"monitoring: {result.event_type}")
        except Exception as exc:
            logger.warning(
                "Could not advance state for app %d to %s: %s",
                application_id, target_state, exc,
            )

    # 5. Notify on positive events
    notification_sent = False
    if result.event_type in NOTIFY_EVENTS:
        notification_sent = _send_notification(
            conn, application_id, result, gmail_client, notification_recipient
        )

    logger.info(
        "monitoring app_id=%d event=%s confidence=%.2f color=%s notify=%s",
        application_id, result.event_type, result.confidence,
        color, notification_sent,
    )

    return ProcessResult(
        application_id=application_id,
        event_type=result.event_type,
        confidence=result.confidence,
        color_status=color,
        notification_sent=notification_sent,
    )


def process_inbox(
    conn: sqlite3.Connection,
    gmail_client,
    *,
    classifier: Optional[ClassifierFn] = None,
    notification_recipient: Optional[str] = None,
) -> list[ProcessResult]:
    """
    Fetch unread monitoring emails and process them against active applications.
    Matches emails to applications by looking up application receipts / thread IDs.
    """
    # Get all MONITORING applications
    cur = conn.execute(
        "SELECT id, receipt FROM applications WHERE state='MONITORING'"
    )
    monitoring_apps = {row["id"]: row["receipt"] for row in cur.fetchall()}

    if not monitoring_apps:
        logger.info("No MONITORING applications — inbox check skipped")
        return []

    # Fetch inbox (mocked in tests)
    messages = gmail_client.get_inbox(query="label:inbox")
    results = []

    for msg in messages:
        # Simple heuristic: match by app_id mentioned in subject or body
        matched_app_id = _match_message_to_app(msg, monitoring_apps)
        if matched_app_id is None:
            logger.debug("No matching application for email: %r", msg.subject)
            continue

        r = process_inbound_email(
            conn, matched_app_id,
            msg.subject, msg.body,
            gmail_thread_id=msg.thread_id,
            classifier=classifier,
            gmail_client=gmail_client,
            notification_recipient=notification_recipient,
        )
        results.append(r)

    return results


def _match_message_to_app(msg, monitoring_apps: dict) -> Optional[int]:
    """Match an inbound email to an application by receipt tag in subject."""
    combined = (msg.subject + " " + msg.body).lower()
    for app_id in monitoring_apps:
        if f"app #{app_id}" in combined or f"app-{app_id}" in combined:
            return app_id
    return None


def _send_notification(
    conn, app_id: int, result: ClassificationResult,
    gmail_client, recipient: Optional[str],
) -> bool:
    """Send a notification email for positive outcomes."""
    if gmail_client is None or recipient is None:
        logger.info(
            "Notification skipped app_id=%d event=%s (no client/recipient)",
            app_id, result.event_type,
        )
        return False

    cur = conn.execute(
        "SELECT j.company, j.title FROM applications a "
        "JOIN jobs j ON a.job_id=j.id WHERE a.id=?",
        (app_id,),
    )
    row = cur.fetchone()
    company = row["company"] if row else "Unknown"
    title = row["title"] if row else "Unknown"

    subject = f"[Job-Agent] {result.event_type.replace('_',' ').title()}: {company} — {title}"
    body = (
        f"Good news! Application #{app_id} has a new update:\n\n"
        f"Event: {result.event_type}\n"
        f"Company: {company}\n"
        f"Role: {title}\n"
        f"Confidence: {result.confidence:.0%}\n"
    )
    gmail_client.send_email(to=recipient, subject=subject, body=body)
    logger.info(
        "notification sent app_id=%d event=%s to=%s",
        app_id, result.event_type, recipient,
    )
    return True
