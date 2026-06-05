"""
Morning batch digest builder.

Sends a single email listing all WAITING_FOR_USER_APPROVAL applications.
The email is designed to be read and replied to from a phone.

Subject format: [ai-jobber] 3 jobs need review — Jun 05 [DIGEST-20260605-a3f9]

Reply format (any order, one per line):
  APP-1 APPROVE
  APP-2 REJECT
  APP-3 SKIP
  APP-2 EDIT "focus the opening on RL research not game AI"
  APP-1 SNOOZE 2
"""
from __future__ import annotations
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from src.db.applications import get_applications_by_state, update_application

logger = logging.getLogger(__name__)


def _digest_id(ts: str) -> str:
    """Deterministic short ID based on timestamp."""
    h = hashlib.md5(ts.encode()).hexdigest()[:8]
    date = ts[:10].replace("-", "")
    return f"DIGEST-{date}-{h}"


def build_digest(
    conn: sqlite3.Connection,
    recipient: str,
    *,
    gmail_client=None,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Build and (MCP-)queue the morning digest email.
    Returns the action_id / message_id, or None if nothing pending.
    """
    pending = get_applications_by_state(conn, "WAITING_FOR_USER_APPROVAL")
    if not pending:
        logger.info("No WAITING_FOR_USER_APPROVAL apps — skipping digest")
        return None

    now_utc  = datetime.now(timezone.utc)
    digest_id = _digest_id(now_utc.isoformat())
    subject, body = _render_digest(conn, pending, digest_id, now_utc)

    # Stamp each application with its label and digest_id
    for i, app in enumerate(pending, start=1):
        label = f"APP-{app.id}"
        update_application(conn, app.id, app_label=label, digest_id=digest_id)

    # Persist the digest record
    app_ids = json.dumps([a.id for a in pending])
    conn.execute(
        """
        INSERT OR IGNORE INTO digests
            (digest_id, subject, recipient, app_ids_json)
        VALUES (?, ?, ?, ?)
        """,
        (digest_id, subject, recipient, app_ids),
    )
    conn.commit()

    if dry_run:
        logger.info("DIGEST (dry_run) id=%s:\n%s\n\n%s", digest_id, subject, body)
        return digest_id

    if gmail_client is None:
        raise ValueError("gmail_client required to send digest")

    # MCPGmailClient queues a create_draft action; Claude Code executes it via MCP
    action_id = gmail_client.send_email(
        to=recipient,
        subject=subject,
        body=body,
    )
    logger.info(
        "digest queued action_id=%s digest_id=%s apps=%d",
        action_id, digest_id, len(pending),
    )
    return action_id


def _render_digest(
    conn: sqlite3.Connection,
    pending: list,
    digest_id: str,
    now: datetime,
) -> tuple[str, str]:
    date_str = now.strftime("%b %d")
    n = len(pending)
    subject = (
        f"[ai-jobber] {n} job{'s' if n != 1 else ''} need your review "
        f"— {date_str} [{digest_id}]"
    )

    lines = [
        f"Hi Pranav — {n} application{'s' if n != 1 else ''} need your review.",
        "",
        "Reply from any device with commands (one per line):",
        "  APP-N APPROVE          → submit this application",
        "  APP-N REJECT           → skip permanently",
        "  APP-N SKIP             → skip for now",
        '  APP-N EDIT "your note" → re-tailor with your instructions',
        "  APP-N SNOOZE           → remind tomorrow",
        "  APP-N SNOOZE 3         → remind in 3 days",
        "",
        "=" * 56,
        "",
    ]

    for app in pending:
        cur = conn.execute(
            "SELECT j.company, j.title, j.url, j.platform, j.has_screener "
            "FROM jobs j JOIN applications a ON a.job_id=j.id WHERE a.id=?",
            (app.id,),
        )
        row = cur.fetchone()
        company  = row["company"]      or "Unknown"
        title    = row["title"]        or "Unknown"
        url      = row["url"]          or ""
        platform = row["platform"]     or "unknown"
        screener = "YES" if row["has_screener"] else "no"
        score    = f"{app.score:.0f}/100" if app.score else "n/a"
        tier     = app.submit_tier or "gated"

        lines += [
            f"APP-{app.id}  |  {company}",
            f"  Role    : {title}",
            f"  Score   : {score}  |  Tier: {tier}  |  Platform: {platform}  |  Screener: {screener}",
            f"  Apply   : {url}",
        ]

        # Show diff path only if it's short enough to be useful on phone
        if app.resume_diff_path:
            diff_name = app.resume_diff_path.split("\\")[-1].split("/")[-1]
            lines.append(f"  Diff    : {diff_name} (in artifact folder)")

        lines.append("")

    lines += [
        "=" * 56,
        "",
        "Reply with your commands above.",
        "Anything in quotes after EDIT becomes the tailoring instruction.",
        f"(Digest reference: {digest_id})",
    ]

    return subject, "\n".join(lines)
