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
from src.verifier.retrieval import BankItem
from src.resume.retrieval import retrieve_bullets
from src.resume.rephrase import rephrase_bullets, RephraserFn
from src.resume.renderer import render_docx, render_pdf
from src.resume.diff import write_resume_diff

logger = logging.getLogger(__name__)


@dataclass
class TailoringResult:
    resume_path: str
    resume_pdf_path: str
    resume_diff_path: str
    verifier_passed: bool


def run_tailoring(
    conn: sqlite3.Connection,
    app_id: int,
    *,
    hot_keywords: list[str],
    job_title: str = "",
    candidate_name: str = "Candidate",
    rephraser: Optional[RephraserFn] = None,
    extractor=None,   # verifier claim extractor (injected in tests)
    surface: str = "resume",
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

    # 3. Bounded rephrase
    rephrased = rephrase_bullets(
        all_bullets, hot_keywords, job_title, rephraser=rephraser
    )

    # 4. Render DOCX + PDF
    docx_path = artifact_path(folder, "resume.docx")
    rephrased_by_type = _group_rephrased(all_bullets, rephrased)
    render_docx(rephrased_by_type, candidate_name=candidate_name,
                job_title=job_title, out_path=docx_path)
    pdf_path = render_pdf(docx_path, artifact_path(folder, "resume.pdf"))

    # 5. Verify all rephrased text
    full_text = " ".join(rephrased)
    verifier_report = verify_text(
        conn, app_id, full_text, surface=surface, extractor=extractor
    )

    # 6. Write diff
    diff_path = write_resume_diff(
        folder, all_bullets, rephrased, verifier_report, job_title=job_title
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

    # 8. All claims passed — persist and advance state
    update_application(
        conn, app_id,
        resume_path=docx_path,
        resume_pdf_path=pdf_path,
        resume_diff_path=diff_path,
        verifier_passed=1,
    )
    transition(conn, app_id, "RESUME_VERIFIED", reason="verifier passed")

    logger.info(
        "tailoring complete app_id=%d resume=%s", app_id, docx_path
    )
    return TailoringResult(
        resume_path=docx_path,
        resume_pdf_path=pdf_path,
        resume_diff_path=diff_path,
        verifier_passed=True,
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
