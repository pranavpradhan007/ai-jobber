"""
auto_safe submit path.
Handles email-apply and API submit for `auto_safe` tier applications.
Enforces: verifier_passed=1 must be set before calling.
CAPTCHA/MFA → transitions to hand-off state, never auto-solves.
"""
from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.db.applications import get_application, update_application
from src.db.state_machine import transition, SubmittingInvariantError
from src.db.audit import log_action

logger = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    success: bool
    receipt: Optional[str] = None
    error: Optional[str] = None
    captcha_detected: bool = False
    mfa_detected: bool = False


class AutoSafeSubmitError(Exception):
    pass


def submit_auto_safe(
    conn: sqlite3.Connection,
    app_id: int,
    *,
    gmail_client=None,   # injected — mocked in tests
    dry_run: bool = False,
) -> SubmitResult:
    """
    Submit an auto_safe application (email apply or API).
    Transitions: READY_TO_SUBMIT → SUBMITTING → SUBMITTED
    CAPTCHA/MFA detected → WAITING_FOR_CAPTCHA / WAITING_FOR_MFA
    """
    app = get_application(conn, app_id)

    # Enforce pre-conditions (state machine also enforces, but be explicit)
    if not app.verifier_passed:
        raise AutoSafeSubmitError(
            f"Cannot submit app {app_id}: verifier_passed=0"
        )
    if not app.auto_safe:
        raise AutoSafeSubmitError(
            f"Cannot use auto_safe path for app {app_id}: auto_safe=0 (use approval flow)"
        )

    # Transition to SUBMITTING (state machine enforces invariant)
    transition(conn, app_id, "SUBMITTING", reason="auto_safe submit")

    if dry_run:
        receipt = f"DRY_RUN_{app_id}"
        _finalise_submission(conn, app_id, receipt)
        return SubmitResult(success=True, receipt=receipt)

    # Determine submit method from job
    from src.db.connection import get_connection  # avoid circular
    cur = conn.execute(
        "SELECT j.platform, j.email_apply_addr "
        "FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id=?",
        (app_id,),
    )
    row = cur.fetchone()
    platform = (row["platform"] or "email").lower()
    email_addr = row["email_apply_addr"]

    try:
        if platform == "email":
            result = _submit_email(conn, app_id, email_addr, gmail_client)
        elif platform == "api":
            result = _submit_api(conn, app_id)
        else:
            # Unexpected — route to manual
            result = SubmitResult(success=False, error=f"Unknown platform {platform}")
    except _CaptchaDetected:
        transition(conn, app_id, "WAITING_FOR_CAPTCHA",
                   reason="CAPTCHA encountered during submit")
        return SubmitResult(success=False, captcha_detected=True)
    except _MFADetected:
        transition(conn, app_id, "WAITING_FOR_MFA",
                   reason="MFA encountered during submit")
        return SubmitResult(success=False, mfa_detected=True)
    except Exception as exc:
        logger.error("submit failed app_id=%d error=%s", app_id, exc)
        result = SubmitResult(success=False, error=str(exc))

    if result.success and result.receipt:
        _finalise_submission(conn, app_id, result.receipt)
    else:
        transition(conn, app_id, "FAILED", reason=result.error or "submit failed")

    return result


def _finalise_submission(
    conn: sqlite3.Connection,
    app_id: int,
    receipt: str,
) -> None:
    from datetime import datetime, timezone
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    update_application(
        conn, app_id,
        receipt=receipt,
        submitted_at=submitted_at,
    )
    transition(conn, app_id, "SUBMITTED", reason="submit receipt received")
    log_action(conn, action="auto_safe_submit", application_id=app_id,
               details={"receipt": receipt})
    logger.info("submitted app_id=%d receipt=%s", app_id, receipt)


def _submit_email(
    conn, app_id: int, email_addr: Optional[str], gmail_client
) -> SubmitResult:
    """Send application via email."""
    if not email_addr:
        raise AutoSafeSubmitError(f"No email_apply_addr for app {app_id}")
    if gmail_client is None:
        raise AutoSafeSubmitError("gmail_client is required for email submit")

    app = get_application(conn, app_id)
    subject = f"Application for position (App #{app_id})"
    body = f"Please find my application attached.\n\nApplication ID: {app_id}"

    # gmail_client.send_email returns a message_id (str)
    message_id = gmail_client.send_email(
        to=email_addr,
        subject=subject,
        body=body,
        attachments=[app.resume_path] if app.resume_path else [],
    )
    return SubmitResult(success=True, receipt=f"GMAIL:{message_id}")


def _submit_api(conn, app_id: int) -> SubmitResult:
    """API-based submit (stub — Sprint 5 scope is email primary)."""
    logger.info("API submit app_id=%d (stub)", app_id)
    return SubmitResult(success=True, receipt=f"API_STUB_{app_id}")


class _CaptchaDetected(Exception):
    pass

class _MFADetected(Exception):
    pass
