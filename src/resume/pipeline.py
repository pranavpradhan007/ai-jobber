"""
Resume tailoring pipeline.

Full flow:
  1. Retrieve relevant bank items for hot_keywords
  2. Bounded rephrase
  3. Render DOCX + PDF
  4. Invoke diff-verifier on all rephrased text
  5. Write resume_diff.md
  6. If verifier passes → set verifier_passed=1 and advance to RESUME_VERIFIED
     If verifier fails → raise VerifierGateError (caller handles FAILED state)
  7. Store artifact paths on application row
"""
from __future__ import annotations
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

from src.db.applications import update_application
from src.db.state_machine import transition
from src.storage.folders import create_application_folder, artifact_path
from src.verifier.diff_verifier import verify_text, VerifierGateError
from src.verifier.style_checker import check_style, StyleGateError, style_report
from src.verifier.retrieval import BankItem
from src.resume.retrieval import retrieve_bullets
from src.resume.rephrase import rephrase_bullets, RephraserFn
from src.resume.renderer import render_docx, render_pdf
from src.resume.diff import write_resume_diff
from src.ats.scanner import ats_scan, ats_report_text

logger = logging.getLogger(__name__)


@dataclass
class TailoringResult:
    resume_path: str
    resume_pdf_path: str
    resume_diff_path: str
    verifier_passed: bool
    ats_report_path: str = ""
    ats_score: int = 0


class PageLimitError(Exception):
    """Raised when the rendered resume exceeds 1 page after trimming."""


def run_tailoring(
    conn: sqlite3.Connection,
    app_id: int,
    *,
    hot_keywords: list[str],
    job_title: str = "",
    candidate_name: str = "Candidate",
    rephraser: Optional[RephraserFn] = None,
    extractor=None,           # verifier claim extractor (injected in tests)
    surface: str = "resume",
    edit_instruction: Optional[str] = None,  # from APP-N EDIT "..." reply
) -> TailoringResult:
    """
    Execute the full tailoring pipeline for one application.
    Returns TailoringResult with artifact paths and verifier status.
    Raises VerifierGateError if blocked claims are found (caller transitions to FAILED).
    """
    # 1. Folder
    folder = create_application_folder(conn, app_id)

    # 2. Retrieve bullets
    bullets_by_type = retrieve_bullets(conn, hot_keywords, surface=surface)
    all_bullets: list[BankItem] = [
        b for blist in bullets_by_type.values() for b in blist
    ]

    # 3. Bounded rephrase (pass edit_instruction for user-directed re-tailoring)
    rephrased = rephrase_bullets(
        all_bullets, hot_keywords, job_title,
        rephraser=rephraser,
        edit_instruction=edit_instruction,
    )

    # 4. Render DOCX + PDF
    docx_path = artifact_path(folder, "resume.docx")
    rephrased_by_type = _group_rephrased(all_bullets, rephrased)
    render_docx(rephrased_by_type, candidate_name=candidate_name,
                job_title=job_title, out_path=docx_path)
    pdf_path = render_pdf(docx_path, artifact_path(folder, "resume.pdf"))

    # 5a. Style check — em dashes and AI language (BEFORE verifier)
    full_text = "\n".join(rephrased)
    style_violations = check_style(full_text, raise_on_hard=False)
    hard_style = [v for v in style_violations if v.hard_fail]
    if hard_style:
        logger.error(
            "style gate blocked app_id=%d: %d hard violation(s) — %s",
            app_id, len(hard_style),
            "; ".join(v.text for v in hard_style[:3]),
        )
        raise StyleGateError(style_violations)

    if style_violations:
        logger.warning(
            "app_id=%d style warnings (%d): %s",
            app_id, len(style_violations),
            ", ".join(v.text for v in style_violations[:5]),
        )

    # 5b. Claim verifier
    verifier_report = verify_text(
        conn, app_id, full_text, surface=surface, extractor=extractor
    )

    # 6. Write diff (includes style report)
    diff_path = write_resume_diff(
        folder, all_bullets, rephrased, verifier_report,
        job_title=job_title,
        style_report=style_report(style_violations),
    )

    # 7. Gate on verifier
    if not verifier_report.passed:
        logger.error(
            "tailoring blocked app_id=%d: %d blocked claims",
            app_id, len(verifier_report.blocked_claims),
        )
        # Store partial paths but do NOT set verifier_passed
        update_application(
            conn, app_id,
            resume_path=docx_path,
            resume_pdf_path=pdf_path,
            resume_diff_path=diff_path,
            verifier_passed=0,
        )
        raise VerifierGateError(
            f"Tailoring blocked: {len(verifier_report.blocked_claims)} "
            f"unsupported claim(s) found."
        )

    # 8. ATS scan — run on the full rephrased text
    experience_bullets = [
        rephrased[i] for i, item in enumerate(all_bullets)
        if i < len(rephrased) and item.item_type == "metric"
    ]
    ats_result = ats_scan(
        full_text,
        hot_keywords,
        experience_bullets=experience_bullets or None,
    )
    ats_report = ats_report_text(ats_result, job_title=job_title)
    ats_path = artifact_path(folder, "ats_report.txt")
    with open(ats_path, "w", encoding="utf-8") as fh:
        fh.write(ats_report)
    logger.info(
        "ATS scan app_id=%d score=%d page=%.2f passed=%s",
        app_id, ats_result.ats_score, ats_result.page_count_est, ats_result.passed,
    )

    # Hard gate: 1-page limit
    if not ats_result.passed and ats_result.page_count_est > 1.05:
        logger.error(
            "ATS gate blocked app_id=%d: resume estimated %.2f pages (must be <= 1)",
            app_id, ats_result.page_count_est,
        )
        raise PageLimitError(
            f"Resume exceeds 1 page (est {ats_result.page_count_est:.2f}). "
            "Reduce bullet count or shorten bullets."
        )

    # 9. All claims passed — persist and advance state
    update_application(
        conn, app_id,
        resume_path=docx_path,
        resume_pdf_path=pdf_path,
        resume_diff_path=diff_path,
        verifier_passed=1,
    )
    transition(conn, app_id, "RESUME_VERIFIED", reason="verifier passed")

    logger.info(
        "tailoring complete app_id=%d resume=%s ats=%d", app_id, docx_path, ats_result.ats_score
    )
    return TailoringResult(
        resume_path=docx_path,
        resume_pdf_path=pdf_path,
        resume_diff_path=diff_path,
        verifier_passed=True,
        ats_report_path=ats_path,
        ats_score=ats_result.ats_score,
    )


def _group_rephrased(
    bullets: list[BankItem], rephrased: list[str]
) -> dict[str, list[str]]:
    """Map rephrased strings back to item types."""
    result: dict[str, list[str]] = {}
    for i, item in enumerate(bullets):
        if i < len(rephrased):
            result.setdefault(item.item_type, []).append(rephrased[i])
    return result
