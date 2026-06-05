"""
Morning batch digest builder.
Builds a single email listing all WAITING_FOR_USER_APPROVAL applications.
Each entry includes: company, title, score, resume link, and reply instructions.
"""
from __future__ import annotations
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from src.db.applications import get_applications_by_state, get_application

logger = logging.getLogger(__name__)

REPLY_INSTRUCTIONS = """\
Reply with one command per line. Format: APP-{id} COMMAND

Commands:
  APPROVE  — submit this application
  REJECT   — skip permanently
  SKIP     — skip for now (revisit later)
  EDIT     — re-tailor the resume
  SNOOZE   — remind me tomorrow
  DONE     — mark this digest as fully processed
  MANUAL   — flag for manual handling
"""


def build_digest(
    conn: sqlite3.Connection,
    recipient: str,
    *,
    gmail_client=None,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Build and send the morning digest email.
    Returns the message_id if sent, or None if no pending apps.
    """
    pending = get_applications_by_state(conn, "WAITING_FOR_USER_APPROVAL")
    if not pending:
        logger.info("No WAITING_FOR_USER_APPROVAL apps — skipping digest")
        return None

    subject, body = _render_digest(conn, pending)

    if dry_run:
        logger.info("DIGEST (dry_run):\n%s\n%s", subject, body)
        return "DRY_RUN"

    if gmail_client is None:
        raise ValueError("gmail_client required to send digest")

    msg_id = gmail_client.send_email(
        to=recipient,
        subject=subject,
        body=body,
    )
    logger.info(
        "sent digest to %s: %d apps, message_id=%s",
        recipient, len(pending), msg_id,
    )
    return msg_id


def _render_digest(conn, pending) -> tuple[str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"[Job-Agent] Morning Digest — {len(pending)} app(s) awaiting approval ({now})"

    lines = [
        f"Good morning! {len(pending)} application(s) need your approval.",
        "",
        "─" * 60,
        "",
    ]

    for app in pending:
        cur = conn.execute(
            "SELECT j.company, j.title, j.url FROM jobs j "
            "JOIN applications a ON a.job_id = j.id WHERE a.id=?",
            (app.id,),
        )
        row = cur.fetchone()
        company = row["company"] or "Unknown"
        title = row["title"] or "Unknown"
        url = row["url"] or ""

        lines += [
            f"APP-{app.id}: {company} — {title}",
            f"  Score:   {app.score or 'n/a'}",
            f"  Tier:    {app.submit_tier}",
            f"  URL:     {url}",
            f"  Resume:  {app.resume_path or 'n/a'}",
            f"  Diff:    {app.resume_diff_path or 'n/a'}",
            "",
        ]

    lines += [
        "─" * 60,
        "",
        REPLY_INSTRUCTIONS,
    ]

    return subject, "\n".join(lines)
