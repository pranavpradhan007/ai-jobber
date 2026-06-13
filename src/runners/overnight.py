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
    rate_limited: int = 0
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
        WHERE a.state IN ('WAITING_FOR_USER_APPROVAL', 'READY_TO_SUBMIT') AND a.approved_by_user=1
        AND LOWER(j.platform) NOT IN ('remoteok', 'hackernews', 'custom')
        AND a.id NOT IN (3, 47, 48, 50, 56, 93, 125)
        LIMIT ?
        """,
        (max_jobs,),
    )
    approved_ids = [row["id"] for row in cur.fetchall()]
    logger.info("overnight: %d approved gated apps to submit", len(approved_ids))

    for app_id in approved_ids:
        # Stop early if LinkedIn daily rate limit is active — no point trying more jobs
        if stats.rate_limited > 0:
            logger.info(
                "overnight: rate limit active — skipping remaining %d approved jobs",
                len(approved_ids) - approved_ids.index(app_id),
            )
            break
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
        "submitted=%d gated=%d failed=%d rate_limited=%d",
        stats.scored, stats.skipped, stats.tailored,
        stats.submitted, stats.gated, stats.failed, stats.rate_limited,
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
        "SELECT j.platform, j.url, j.company, j.title, j.clean_jd, j.raw_jd, a.folder_path, a.resume_path "
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
        folder = (row["folder_path"] or "").replace("/", os.sep)
        if not folder:
            from src.storage.folders import create_application_folder  # noqa: PLC0415
            folder = create_application_folder(conn, app_id).replace("/", os.sep)

        # Pre-compute screener answers BEFORE the browser session opens
        # — eliminates all LLM latency from the hot fill path
        from src.browser.screener_engine import precompute_screener_answers  # noqa: PLC0415
        base_answers = build_candidate_answers(
            app_id,
            job_title=row["title"] or "",
            company=row["company"] or "",
            resume_path=row["resume_path"] or "",
        )
        screener = precompute_screener_answers(
            app_id=app_id,
            job_title=row["title"] or "",
            company=row["company"] or "",
            clean_jd=row["clean_jd"] or row["raw_jd"] or "",
            candidate_answers=base_answers,
            cache_dir=folder,
            llm_timeout=5,  # fail fast — manifests aren't serviced in automated runs
        )
        answers = build_candidate_answers(
            app_id,
            job_title=row["title"] or "",
            company=row["company"] or "",
            resume_path=row["resume_path"] or "",
            screener_answers=screener.answers,
        )
        if screener.llm_timed_out:
            logger.warning("app_id=%d screener LLM timed out — using safe defaults", app_id)

        from src.browser.prefill import CaptchaDetected, MFADetected, AITrapDetected  # noqa: PLC0415
        from src.browser.captcha_notifier import send_captcha_notification, poll_for_captcha_reply  # noqa: PLC0415

        def _handle_captcha_or_mfa(challenge_type: str) -> None:
            state = "WAITING_FOR_CAPTCHA" if challenge_type == "CAPTCHA" else "WAITING_FOR_MFA"
            transition(conn, app_id, state, reason=f"{challenge_type} encountered during portal submit")
            stats.failed += 1
            logger.warning("%s on app_id=%d — notifying user", challenge_type, app_id)
            send_captcha_notification(
                gmail_client,
                app_id=app_id,
                company=row["company"] or "",
                title=row["title"] or "",
                portal_url=row["url"] or "",
                challenge_type=challenge_type,
            )

        try:
            result = auto_submit_portal(
                app_id, answers,
                url=row["url"] or "",
                folder_path=folder,
                dry_run=dry_run,
            )
        except CaptchaDetected:
            _handle_captcha_or_mfa("CAPTCHA")
            return
        except MFADetected:
            _handle_captcha_or_mfa("MFA")
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
            # Learning loop: write application note and record screener answers
            try:
                from src.learning.notes import write_learning_note  # noqa: PLC0415
                from src.learning.defaults import record_question    # noqa: PLC0415
                write_learning_note(conn, app_id)
                # Record universal screener answers so future apps reuse consistent answers
                _ROLE_SPECIFIC = {"why_this_company", "why_this_role", "strengths",
                                   "biggest_project", "challenge_overcome",
                                   "tell_us_about", "relevant_experience"}
                _CANONICAL_LABELS = {
                    "eeo_gender": "What is your gender?",
                    "eeo_race": "What is your race/ethnicity?",
                    "eeo_ethnicity": "What is your race/ethnicity?",
                    "eeo_hispanic": "Are you Hispanic or Latino?",
                    "eeo_veteran": "What is your veteran status?",
                    "eeo_disability": "Do you have a disability?",
                    "requires_sponsorship": "Do you require visa sponsorship for employment?",
                    "work_authorization": "Are you authorized to work in the United States?",
                    "salary_expectation": "What is your expected salary?",
                    "years_experience": "How many years of relevant experience do you have?",
                    "willing_to_relocate": "Are you willing to relocate?",
                    "remote_preference": "What is your preference for remote work?",
                    "graduation_year": "What year did you graduate?",
                    "education_degree": "What is your highest level of education?",
                }
                for key, canonical_q in _CANONICAL_LABELS.items():
                    val = answers.get(key)
                    if val and isinstance(val, str):
                        record_question(canonical_q, val, application_id=app_id)
            except Exception as _le:
                logger.debug("learning loop post-submit: %s", _le)
        elif result.captcha_detected:
            transition(conn, app_id, "WAITING_FOR_CAPTCHA",
                       reason="CAPTCHA on portal form")
            stats.failed += 1
            send_captcha_notification(
                gmail_client,
                app_id=app_id,
                company=row["company"] or "",
                title=row["title"] or "",
                portal_url=row["url"] or "",
                challenge_type="CAPTCHA",
            )
        elif result.mfa_detected:
            transition(conn, app_id, "WAITING_FOR_MFA",
                       reason="MFA/login required on portal form")
            stats.failed += 1
            send_captcha_notification(
                gmail_client,
                app_id=app_id,
                company=row["company"] or "",
                title=row["title"] or "",
                portal_url=row["url"] or "",
                challenge_type="MFA",
            )
        elif result.ai_trap_detected:
            transition(conn, app_id, "AI_TRAP_DETECTED",
                       reason="AI trap found on portal form")
            stats.skipped += 1
        else:
            err = result.error or ""
            logger.error("portal submit failed app_id=%d error=%s", app_id, err)
            # LinkedIn daily rate limit — leave in READY_TO_SUBMIT for retry next pass
            _RATE_LIMIT_SIGNALS = (
                "daily rate limit reached", "limit daily submission",
                "apply tomorrow", "prevent bots",
            )
            if any(p in err.lower() for p in _RATE_LIMIT_SIGNALS):
                logger.warning(
                    "app_id=%d LinkedIn daily rate limit hit — keeping READY_TO_SUBMIT for retry",
                    app_id,
                )
                stats.rate_limited += 1
                return  # leave state unchanged; will retry on next pass
            # Permanent failures → SKIPPED so they are never retried
            _PERMANENT_ERRORS = (
                "job is closed", "no longer accepting", "modal did not open",
                "no easy apply", "no external apply", "easy apply button not found",
                "stuck", "unfilled required fields",
                "no continue/submit button", "form may have a required field",
                "unsupported portal",
            )
            if any(p in err.lower() for p in _PERMANENT_ERRORS):
                try:
                    transition(conn, app_id, "SKIPPED", reason=f"permanent: {err[:120]}")
                    logger.info("app_id=%d permanently skipped: %s", app_id, err[:80])
                    stats.skipped += 1
                except Exception:
                    stats.failed += 1
            else:
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

    # Always approve immediately — route to portal submit path
    conn.execute("UPDATE applications SET approved_by_user=1 WHERE id=?", (app_id,))
    conn.commit()

    # ── 8. Submit via portal browser (covers both auto_safe and gated) ───────
    _submit_approved(conn, app_id, stats, gmail_client=gmail_client, dry_run=dry_run)
