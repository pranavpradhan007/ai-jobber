"""
Overnight runner.

Drains the work queue through: parse → score → tailor → verify → package.
auto_safe jobs: submit immediately (email → MCP draft action queued).
gated jobs: transition to WAITING_FOR_USER_APPROVAL.

Injected dependencies (all mocked in tests):
  - scorer_fn:    ScorerFn
  - rephraser_fn: RephraserFn
  - extractor_fn: ExtractorFn (verifier claim extractor)
  - gmail_client: defaults to MCPGmailClient (no OAuth needed)
"""
from __future__ import annotations
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from src.db.applications import get_application, update_application
from src.db.state_machine import transition, IllegalTransitionError
from src.db.audit import log_action
from src.queues.queue import enqueue, acquire_lease, release_lease
from src.parsing.jd_parser import parse_jd
from src.keywords.extractor import extract_keywords
from src.scoring.scorer import score_job
from src.scoring.gate import apply_score_gate
from src.resume.pipeline import run_tailoring, PageLimitError
from src.browser.submit import submit_auto_safe, submit_gated_approved
from src.browser.auto_submit import auto_submit_portal, build_candidate_answers
from src.verifier.diff_verifier import VerifierGateError
from src.browser.trap_detector import detect_traps_in_jd

logger = logging.getLogger(__name__)


@dataclass
class OvernightStats:
    scored: int = 0
    skipped: int = 0
    tailored: int = 0
    submitted: int = 0
    gated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_overnight(
    conn: sqlite3.Connection,
    *,
    scorer_fn=None,
    rephraser_fn=None,
    extractor_fn=None,
    gmail_client=None,          # defaults to MCPGmailClient when None
    candidate_name: str = "Candidate",
    max_jobs: int = 50,
    dry_run: bool = False,
) -> OvernightStats:
    """
    Main overnight pipeline. Returns statistics.
    Processes all DISCOVERED applications up to max_jobs.
    Also processes WAITING_FOR_USER_APPROVAL apps where approved_by_user=1.
    """
    if gmail_client is None:
        from src.gmail.client import MCPGmailClient  # noqa: PLC0415
        gmail_client = MCPGmailClient()

    stats = OvernightStats()

    # Phase 1: DISCOVERED apps (full pipeline)
    cur = conn.execute(
        "SELECT id FROM applications WHERE state='DISCOVERED' LIMIT ?",
        (max_jobs,),
    )
    discovered_ids = [row["id"] for row in cur.fetchall()]

    logger.info("overnight: %d discovered apps to process", len(discovered_ids))

    for app_id in discovered_ids:
        try:
            _process_one(
                conn, app_id, stats,
                scorer_fn=scorer_fn,
                rephraser_fn=rephraser_fn,
                extractor_fn=extractor_fn,
                gmail_client=gmail_client,
                candidate_name=candidate_name,
                dry_run=dry_run,
            )
        except Exception as exc:
            msg = f"app_id={app_id} unhandled error: {exc}"
            logger.error(msg)
            stats.errors.append(msg)
            stats.failed += 1
            log_action(conn, action="overnight_error", application_id=app_id,
                       details={"error": str(exc)})

    # Phase 2: Submit approved gated apps (user replied APPROVE)
    # RemoteOK is excluded — bot detection too strong, no reliable apply URL
    cur = conn.execute(
        """
        SELECT a.id FROM applications a JOIN jobs j ON a.job_id=j.id
        WHERE a.state='WAITING_FOR_USER_APPROVAL' AND a.approved_by_user=1
        AND LOWER(j.platform) NOT IN ('remoteok')
        LIMIT ?
        """,
        (max_jobs,),
    )
    approved_ids = [row["id"] for row in cur.fetchall()]
    logger.info("overnight: %d approved gated apps to submit", len(approved_ids))

    for app_id in approved_ids:
        try:
            _submit_approved(conn, app_id, stats, gmail_client=gmail_client, dry_run=dry_run)
        except Exception as exc:
            msg = f"app_id={app_id} approved submit error: {exc}"
            logger.error(msg)
            stats.errors.append(msg)
            stats.failed += 1
            log_action(conn, action="overnight_error", application_id=app_id,
                       details={"error": str(exc)})

    logger.info(
        "overnight complete: scored=%d skipped=%d tailored=%d "
        "submitted=%d gated=%d failed=%d",
        stats.scored, stats.skipped, stats.tailored,
        stats.submitted, stats.gated, stats.failed,
    )
    return stats


def _submit_approved(
    conn,
    app_id: int,
    stats: OvernightStats,
    *,
    gmail_client,
    dry_run: bool,
) -> None:
    """
    Handle a gated app that the user has approved.

    For email/api platforms: auto-submit via submit_gated_approved.
    For portal platforms (greenhouse, workday, indeed, remoteok, etc.):
      V1 cannot auto-submit portal forms — output the apply URL + artifact
      folder so the user can apply manually with the tailored resume.
    """
    cur = conn.execute(
        "SELECT j.platform, j.url, j.company, j.title, a.folder_path, a.resume_path "
        "FROM applications a JOIN jobs j ON a.job_id=j.id WHERE a.id=?",
        (app_id,),
    )
    row = cur.fetchone()
    platform = (row["platform"] or "email").lower()

    if platform in ("email", "api"):
        result = submit_gated_approved(
            conn, app_id,
            gmail_client=gmail_client,
            dry_run=dry_run,
        )
        if result.success:
            transition(conn, app_id, "MONITORING", reason="submitted after user approval")
            stats.submitted += 1
            logger.info("submitted approved app_id=%d receipt=%s", app_id, result.receipt)
        else:
            stats.failed += 1
            logger.error("submit failed approved app_id=%d error=%s", app_id, result.error)
    else:
        # Portal-based job: use Playwright browser automation to navigate + submit
        answers = build_candidate_answers(
            app_id,
            job_title=row["title"] or "",
            company=row["company"] or "",
            resume_path=row["resume_path"] or "",
        )
        folder = (row["folder_path"] or "").replace("/", os.sep)

        from src.browser.prefill import CaptchaDetected, MFADetected, AITrapDetected  # noqa: PLC0415
        try:
            result = auto_submit_portal(
                app_id, answers,
                url=row["url"] or "",
                folder_path=folder,
                dry_run=dry_run,
            )
        except CaptchaDetected:
            transition(conn, app_id, "WAITING_FOR_CAPTCHA",
                       reason="CAPTCHA encountered during portal submit")
            stats.failed += 1
            logger.warning("CAPTCHA on app_id=%d — needs human intervention", app_id)
            return
        except MFADetected:
            transition(conn, app_id, "WAITING_FOR_MFA",
                       reason="MFA encountered during portal submit")
            stats.failed += 1
            return
        except AITrapDetected as e:
            transition(conn, app_id, "AI_TRAP_DETECTED",
                       reason=f"AI trap on apply form: {e}")
            stats.skipped += 1
            return

        if result.success:
            from src.browser.submit import _finalise_submission  # noqa: PLC0415
            transition(conn, app_id, "SUBMITTING",
                       reason="browser auto-submit")
            _finalise_submission(conn, app_id, result.receipt or f"PORTAL:{platform}")
            transition(conn, app_id, "MONITORING",
                       reason="submitted after user approval via browser")
            stats.submitted += 1
            logger.info(
                "portal submitted app_id=%d receipt=%s screenshot=%s",
                app_id, result.receipt, result.screenshot_path,
            )
        elif result.captcha_detected:
            transition(conn, app_id, "WAITING_FOR_CAPTCHA",
                       reason="CAPTCHA on portal form")
            stats.failed += 1
        elif result.mfa_detected:
            transition(conn, app_id, "WAITING_FOR_MFA",
                       reason="MFA/login required on portal form")
            stats.failed += 1
        elif result.ai_trap_detected:
            transition(conn, app_id, "AI_TRAP_DETECTED",
                       reason="AI trap found on portal form")
            stats.skipped += 1
        else:
            logger.error(
                "portal submit failed app_id=%d error=%s",
                app_id, result.error,
            )
            stats.failed += 1


def _process_one(
    conn, app_id, stats: OvernightStats,
    *,
    scorer_fn, rephraser_fn, extractor_fn,
    gmail_client, candidate_name, dry_run,
) -> None:
    app = get_application(conn, app_id)

    # ── 1. Load job details ──────────────────────────────────────────────────
    cur = conn.execute(
        "SELECT * FROM jobs WHERE id=?", (app.job_id,)
    )
    job = cur.fetchone()
    if job is None:
        raise ValueError(f"Job {app.job_id} not found for app {app_id}")

    # ── 2. Parse JD ─────────────────────────────────────────────────────────
    if job["clean_jd"]:
        parsed_jd = job["clean_jd"]
        jd_hash = job["jd_hash"]
    else:
        from src.parsing.jd_parser import parse_jd as _parse
        p = _parse(job["raw_jd"] or "")
        parsed_jd = p.clean_jd
        jd_hash = p.jd_hash

    # ── 3. Keywords ─────────────────────────────────────────────────────────
    kw_report = extract_keywords(conn, parsed_jd)
    hot_keywords = kw_report.hot_keywords

    # ── 3a. AI trap check (JD-level) ─────────────────────────────────────────
    trap = detect_traps_in_jd(parsed_jd)
    if trap.trap_found:
        logger.warning(
            "AI trap detected in JD for app_id=%d type=%s — skipping",
            app_id, trap.trap_type,
        )
        transition(conn, app_id, "AI_TRAP_DETECTED",
                   reason=f"AI trap in JD: {trap.trap_type}: {trap.evidence[:80]}")
        stats.skipped += 1
        return

    # ── 4. Score ─────────────────────────────────────────────────────────────
    job_info = {
        "title": job["title"],
        "location": job["location"],
        "remote": job["remote"],
        "platform": job["platform"],
        "has_screener": job["has_screener"],
        "clean_jd": parsed_jd,
    }
    profile_info = {}  # Sprint 4 stub — real profile loaded later
    scorecard = score_job(job_info, profile_info, scorer=scorer_fn)
    transition(conn, app_id, "SCORED",
               reason=f"score={scorecard.total_score:.1f}")

    gate = apply_score_gate(
        conn, app_id, scorecard,
        platform=job["platform"] or "custom",
        has_screener=job["has_screener"],
        login_required=job["login_required"],
    )
    # Persist numeric score to the application row
    update_application(conn, app_id, score=scorecard.total_score)
    stats.scored += 1

    if not gate["should_apply"]:
        transition(conn, app_id, "SKIPPED",
                   reason=f"score={scorecard.total_score:.1f} below threshold")
        stats.skipped += 1
        return

    # ── 5. Save scorecard to artifact folder ────────────────────────────────
    from src.storage.folders import create_application_folder, artifact_path  # noqa: PLC0415
    from src.scoring.scorer import save_scorecard  # noqa: PLC0415
    folder = create_application_folder(conn, app_id)
    sc_path = save_scorecard(folder, scorecard)
    update_application(conn, app_id, scorecard_path=sc_path)

    # ── 6. Tailor ─────────────────────────────────────────────────────────────
    # Pick up any edit_instruction stored from a phone EDIT reply
    edit_instruction = app.edit_instruction if hasattr(app, "edit_instruction") else None
    transition(conn, app_id, "TAILORING", reason="score gate passed")
    try:
        run_tailoring(
            conn, app_id,
            hot_keywords=hot_keywords,
            job_title=job["title"] or "",
            candidate_name=candidate_name,
            rephraser=rephraser_fn,
            extractor=extractor_fn,
            edit_instruction=edit_instruction,
        )
        stats.tailored += 1
    except (VerifierGateError, PageLimitError) as exc:
        transition(conn, app_id, "FAILED", reason=str(exc))
        stats.failed += 1
        return

    # ── 7. Package ───────────────────────────────────────────────────────────
    transition(conn, app_id, "PACKAGING", reason="tailoring complete")
    transition(conn, app_id, "READY_TO_SUBMIT", reason="packaging complete")

    # Create pending approval record for gated jobs
    if gate["submit_tier"] == "gated":
        conn.execute(
            "INSERT INTO approvals (application_id, approval_type, status) "
            "VALUES (?, 'submit', 'pending')",
            (app_id,),
        )
        conn.commit()
        transition(conn, app_id, "WAITING_FOR_USER_APPROVAL",
                   reason="gated job — awaiting user approval")
        stats.gated += 1
        return

    # ── 7. auto_safe submit ──────────────────────────────────────────────────
    result = submit_auto_safe(
        conn, app_id,
        gmail_client=gmail_client,
        dry_run=dry_run,
    )
    if result.success:
        transition(conn, app_id, "MONITORING", reason="submitted successfully")
        stats.submitted += 1
    else:
        stats.failed += 1
