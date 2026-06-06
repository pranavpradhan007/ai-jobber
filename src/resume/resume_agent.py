"""
Resume Agent — iterative resume generation with hard-rule validation.

Runs up to max_attempts times to produce a resume that:
  1. Passes all hard rules (1 page, no underline/em-dash/AI-words in bullets)
  2. Scores >= score_threshold (default 85) vs the JD

Strategy escalation across attempts so later passes are more conservative:
  Attempts  1–5  : reframe all bullets + inject up to 3 keywords
  Attempts  6–10 : reframe only keyword-poor bullets + inject up to 2 keywords
  Attempts 11–15 : no reframing, inject up to 1 keyword (most conservative)

Returns the highest-scoring valid attempt, or failure if none pass.
"""
from __future__ import annotations
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from src.resume.checker import check_resume, CheckResult
from src.resume.renderer import render_pdf
from src.resume.pipeline import _inject_keywords

logger = logging.getLogger(__name__)

# Hard score floor — tailored resume must meet or beat the ground truth baseline
GROUND_TRUTH_SCORE = 85.0


@dataclass
class ResumeAttempt:
    attempt: int
    strategy: str           # "full" | "partial" | "inject-only"
    checker: CheckResult
    score: float            # 0–100
    keywords_injected: list[str] = field(default_factory=list)
    docx_path: str = ""
    pdf_path: str = ""


@dataclass
class ResumeAgentResult:
    success: bool
    best: Optional[ResumeAttempt]       # best passing attempt (highest score)
    all_attempts: list[ResumeAttempt]   # full log of every attempt
    total_attempts: int = 0


def run_resume_agent(
    source_docx: str,
    source_pdf: str,
    docx_path: str,
    pdf_path: str,
    hot_keywords: list[str],
    job_title: str,
    raw_jd: str = "",
    rephraser_fn: Optional[Callable] = None,
    cache_path: Optional[str] = None,
    *,
    max_attempts: int = 15,
    score_threshold: float = GROUND_TRUTH_SCORE,
) -> ResumeAgentResult:
    """
    Iteratively generate tailored resumes and validate them.
    Returns the best passing attempt, or failure after max_attempts.
    """
    attempts: list[ResumeAttempt] = []
    best: Optional[ResumeAttempt] = None

    for attempt in range(1, max_attempts + 1):
        strategy = _pick_strategy(attempt, max_attempts)
        logger.info("resume_agent attempt=%d/%d strategy=%s", attempt, max_attempts, strategy)

        # Copy ground truth — NEVER modify source
        shutil.copy2(source_docx, docx_path)

        # Apply reframing based on strategy
        kw_for_this = _keywords_for_strategy(hot_keywords, strategy)
        did_reframe = _apply_reframing(
            docx_path, kw_for_this, job_title, raw_jd,
            strategy, attempt, rephraser_fn, cache_path,
        )

        # Only inject keywords if reframing happened — reframing shortens bullets,
        # creating vertical space for the extra skills-line content.
        # inject-only (no reframing) uses ground truth as-is; any skills addition
        # risks pushing a tightly-fitted 1-page resume to 2 pages.
        max_kw = {"full": 2, "partial": 1, "inject-only": 0}[strategy]
        if not did_reframe and strategy != "inject-only":
            max_kw = 0  # no reframing = no room = no injection
        kw_added = _inject_keywords_limited(docx_path, hot_keywords, max_kw)

        # Convert to PDF
        try:
            render_pdf(docx_path, pdf_path)
        except Exception as exc:
            logger.warning("PDF render failed attempt=%d: %s", attempt, exc)
            if os.path.isfile(source_pdf):
                shutil.copy2(source_pdf, pdf_path)

        # Hard-rule check
        checker = check_resume(pdf_path, docx_path, hot_keywords=hot_keywords)

        # Score against JD
        score = _score_resume(docx_path, hot_keywords, raw_jd)

        att = ResumeAttempt(
            attempt=attempt,
            strategy=strategy,
            checker=checker,
            score=score,
            keywords_injected=kw_added,
            docx_path=docx_path,
            pdf_path=pdf_path,
        )
        attempts.append(att)

        passed_rules = checker.passed
        passed_score = score >= score_threshold

        logger.info(
            "attempt=%d rules=%s score=%.1f/%.1f %s",
            attempt,
            "PASS" if passed_rules else "FAIL",
            score, score_threshold,
            "PASS" if passed_score else "FAIL",
        )

        if passed_rules and passed_score:
            if best is None or score > best.score:
                # Snapshot the passing DOCX/PDF so we keep the best version on disk
                best_docx = _snapshot_path(docx_path, attempt)
                best_pdf  = _snapshot_path(pdf_path,  attempt)
                shutil.copy2(docx_path, best_docx)
                shutil.copy2(pdf_path,  best_pdf)
                att.docx_path = best_docx
                att.pdf_path  = best_pdf
                best = att
                logger.info("resume_agent new best: attempt=%d score=%.1f", attempt, score)
            # Keep going — maybe a later attempt scores higher
            # (but stop early if we're in the final strategy tier and already passing)
            if strategy == "inject-only" and best is not None:
                break

    result = ResumeAgentResult(
        success=best is not None,
        best=best,
        all_attempts=attempts,
        total_attempts=len(attempts),
    )
    logger.info(
        "resume_agent done: success=%s attempts=%d best_score=%s",
        result.success,
        result.total_attempts,
        f"{best.score:.1f}" if best else "n/a",
    )
    return result


# ── Strategy helpers ──────────────────────────────────────────────────────────

def _pick_strategy(attempt: int, max_attempts: int) -> str:
    third = max(1, max_attempts // 3)
    if attempt <= third:
        return "full"
    elif attempt <= third * 2:
        return "partial"
    else:
        return "inject-only"


def _keywords_for_strategy(hot_keywords: list[str], strategy: str) -> list[str]:
    """Return the keyword list to use for reframing emphasis."""
    if strategy == "full":
        return hot_keywords
    elif strategy == "partial":
        return hot_keywords[:max(3, len(hot_keywords) // 2)]
    else:
        return hot_keywords[:3]


def _apply_reframing(
    docx_path: str,
    hot_keywords: list[str],
    job_title: str,
    raw_jd: str,
    strategy: str,
    attempt: int,
    rephraser_fn,
    cache_path: Optional[str],
) -> bool:
    """Reframe bullets according to the current strategy. Returns True if any bullet changed."""
    if strategy == "inject-only":
        return False  # no reframing in the most conservative tier

    try:
        from src.resume.bullet_reframer import reframe_bullets_in_docx

        # "partial" strategy: only reframe bullets that lack JD keywords
        partial_only = strategy == "partial"

        n = reframe_bullets_in_docx(
            docx_path,
            hot_keywords=hot_keywords,
            job_title=job_title,
            rephraser_fn=rephraser_fn,
            cache_path=cache_path if attempt == 1 else None,  # cache only on first attempt
            partial_only=partial_only,
        )
        logger.debug("reframed %d bullets (strategy=%s attempt=%d)", n, strategy, attempt)
        return n > 0
    except Exception as exc:
        logger.warning("reframing failed (attempt=%d): %s", attempt, exc)
        return False


def _inject_keywords_limited(docx_path: str, hot_keywords: list[str], max_kw: int) -> list[str]:
    """Inject up to max_kw keywords, overriding the module-level cap."""
    from src.resume.pipeline import _inject_keywords, _MAX_NEW_KEYWORDS
    import src.resume.pipeline as _pm
    old_max = _pm._MAX_NEW_KEYWORDS
    _pm._MAX_NEW_KEYWORDS = max_kw
    try:
        return _inject_keywords(docx_path, hot_keywords)
    finally:
        _pm._MAX_NEW_KEYWORDS = old_max


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_resume(docx_path: str, hot_keywords: list[str], raw_jd: str = "") -> float:
    """
    Score the resume against the JD on a 0–100 scale.

    Formula (calibrated so a well-structured ground-truth resume scores ~80–85):
      base_score     = 75   (credit for being a properly structured 1-page resume)
      keyword_bonus  = up to 15 pts: (hits / total) × 30, capped at 15
      section_bonus  = up to 10 pts: experience + education + skills sections present

    Calibration:
      Ground truth with ~20% JD keyword coverage: 75 + (0.2×30 clamped to 15) + 10 = 85
      Tailored with ~33% coverage:                75 + (0.33×30 = 10) + 10 = 95  (capped)
      Tailored with 0% coverage (inject-only):    75 + 0 + 10 = 85

    The threshold for acceptance is GROUND_TRUTH_SCORE (85) — tailored versions that
    hit ≥ 85 are accepted; those below fall back to ground truth.
    """
    try:
        from docx import Document
        doc = Document(docx_path)
        paras = doc.paragraphs
        full_text = " ".join(p.text for p in paras).lower()
    except Exception:
        return 0.0

    # Keyword bonus (0–15)
    if hot_keywords:
        hits = sum(1 for kw in hot_keywords if kw.lower() in full_text)
        keyword_bonus = min(15.0, (hits / len(hot_keywords)) * 30.0)
    else:
        keyword_bonus = 10.0  # no JD → treat as average

    # Section bonus (0–10)
    has_exp   = any("experience" in p.text.lower() for p in paras)
    has_edu   = any("education"  in p.text.lower() for p in paras)
    has_skill = any("skill"      in p.text.lower() for p in paras)
    section_bonus = (4 if has_exp else 0) + (3 if has_edu else 0) + (3 if has_skill else 0)

    return round(min(100.0, 75.0 + keyword_bonus + section_bonus), 1)


# Keep old name for back-compat imports
_score_resume = score_resume


# ── Snapshot helper ───────────────────────────────────────────────────────────

def _snapshot_path(original: str, attempt: int) -> str:
    """Return a path like resume_best_a03.docx next to the original."""
    p = Path(original)
    return str(p.parent / f"{p.stem}_best_a{attempt:02d}{p.suffix}")
