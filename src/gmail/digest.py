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
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from src.db.applications import get_applications_by_state, update_application

logger = logging.getLogger(__name__)

# Quiet hours: no digest emails sent outside 8am–11pm local time.
# Override via .env: DIGEST_QUIET_START=23  DIGEST_QUIET_END=8
_QUIET_START = int(os.environ.get("DIGEST_QUIET_START", "23"))  # 11pm
_QUIET_END   = int(os.environ.get("DIGEST_QUIET_END",   "8"))   # 8am


def is_quiet_hours(local_hour: Optional[int] = None) -> bool:
    """Return True if current local time is in the quiet window (no emails)."""
    if local_hour is None:
        local_hour = datetime.now().hour
    # Overnight window e.g. 23–8: quiet if hour >= 23 OR hour < 8
    if _QUIET_START > _QUIET_END:
        return local_hour >= _QUIET_START or local_hour < _QUIET_END
    # Same-day window e.g. 9–17: quiet if 9 <= hour < 17
    return _QUIET_START <= local_hour < _QUIET_END


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

    if is_quiet_hours():
        logger.info(
            "Quiet hours (%d:00–%d:00) — digest suppressed until 08:00",
            _QUIET_START, _QUIET_END,
        )
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
        "=" * 56,
        "HOW TO REPLY FROM YOUR PHONE",
        "=" * 56,
        "",
        "Reply to THIS email with one command per line.",
        "You can send all your decisions in a single reply.",
        "Lines starting with > (quoted text) are ignored automatically.",
        "",
        "COMMANDS",
        "--------",
        "",
        "  APP-N APPROVE",
        "      Submit this application on next overnight run.",
        "",
        "  APP-N REJECT",
        "      Skip permanently. Will not appear in future digests.",
        "",
        "  APP-N SKIP",
        "      Skip for now (same effect as REJECT).",
        "",
        '  APP-N EDIT "your instruction here"',
        "      Re-tailor the resume with your direction.",
        "      The instruction adjusts emphasis/tone only —",
        "      the verifier still blocks any invented facts.",
        "",
        "      Examples:",
        '        APP-2 EDIT "lead with JAX and atmospheric ML work"',
        '        APP-3 EDIT "emphasise RL and adversarial robustness"',
        '        APP-4 EDIT "shorter bullets, max one line each"',
        '        APP-5 EDIT "this is a healthcare role — lead with XRayGen"',
        "",
        "  APP-N SNOOZE",
        "      Park for tomorrow. Will reappear in next digest.",
        "",
        "  APP-N SNOOZE 3",
        "      Park for N days (replace 3 with any number).",
        "",
        "EXAMPLE REPLY (copy-paste and edit):",
        "---",
        "APP-1 APPROVE",
        'APP-2 EDIT "focus on data engineering and PySpark pipelines"',
        "APP-3 REJECT",
        "APP-4 SNOOZE 2",
        "APP-5 APPROVE",
        "---",
        "",
        "=" * 56,
        "YOUR APPLICATIONS",
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
        "Quick reminder — reply with commands like:",
        "  APP-1 APPROVE",
        '  APP-2 EDIT "your note here"',
        "  APP-3 REJECT",
        "",
        "Full guide: see USAGE.md in the repo or",
        "scroll up in this email for the full command list.",
        "",
        f"Digest ID: {digest_id}",
        "-- ai-jobber",
    ]

    return subject, "\n".join(lines)
