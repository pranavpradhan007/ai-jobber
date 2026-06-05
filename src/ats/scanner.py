"""
ATS (Applicant Tracking System) resume scanner.

Runs on the rendered resume text after tailoring, before the application
is queued for user approval or submission.

Checks:
  1. Keyword coverage  — what fraction of hot JD keywords appear in resume
  2. Page count        — estimated lines; hard fail if > 1 page
  3. Action verb rate  — Google resume formula: bullet must start with a verb
  4. Quantification    — fraction of experience bullets that contain a number
  5. Style             — no AI language, no em dashes (delegated to style_checker)

Score (0-100):
  keyword_coverage * 40  +  action_verb_rate * 15  +  quantified_rate * 20
  +  page_ok * 25

passed = score >= 50 AND page_ok is True
"""
from __future__ import annotations
import logging
import math
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---- Action verb list (past tense, human-engineer style) -------------------

_ACTION_VERBS = frozenset({
    "built", "trained", "designed", "deployed", "improved", "reduced",
    "integrated", "benchmarked", "evaluated", "wrote", "shipped", "led",
    "ran", "extended", "implemented", "developed", "created", "automated",
    "optimised", "optimized", "refactored", "migrated", "scaled", "tested",
    "maintained", "contributed", "researched", "analysed", "analyzed",
    "published", "released", "prototyped", "profiled", "debugged",
    "reviewed", "coordinated", "delivered", "launched", "completed",
    "increased", "decreased", "achieved", "collaborated",
})

# Chars per line assumption (standard 1-page resume, 10–11 pt, 0.5" margins)
_CHARS_PER_LINE = 90
# Lines that fit on one page (header + body + section headings)
_LINES_PER_PAGE = 55


@dataclass
class ATSScanResult:
    keyword_coverage: float      # 0–1
    page_count_est: float        # estimated pages (1.0 = exactly 1 page)
    action_verb_rate: float      # 0–1 over all bullets
    quantified_rate: float       # 0–1 over experience bullets
    style_issues: list[str]      # em-dash / AI-language violations found
    ats_score: int               # 0–100 composite
    passed: bool                 # True if score >= 50 AND page_ok
    gap_keywords: list[str]      # JD keywords NOT found in resume
    issues: list[str] = field(default_factory=list)   # human-readable notes


def ats_scan(
    resume_text: str,
    hot_keywords: list[str],
    *,
    experience_bullets: list[str] | None = None,
) -> ATSScanResult:
    """
    Scan a rendered resume text.

    resume_text      : full plain-text resume (all sections joined with newlines)
    hot_keywords     : list of JD keywords extracted from the job description
    experience_bullets : optional list of experience/metric bullets for quantification check
    """
    resume_lower = resume_text.lower()
    lines = [ln for ln in resume_text.splitlines() if ln.strip()]
    bullets = _extract_bullets(lines)

    # ── 1. Keyword coverage ─────────────────────────────────────────────────
    found_kw = [kw for kw in hot_keywords if kw.lower() in resume_lower]
    gap_kw   = [kw for kw in hot_keywords if kw.lower() not in resume_lower]
    coverage = len(found_kw) / len(hot_keywords) if hot_keywords else 1.0

    # ── 2. Page count ───────────────────────────────────────────────────────
    total_lines = sum(math.ceil(len(ln) / _CHARS_PER_LINE) for ln in lines)
    page_count  = total_lines / _LINES_PER_PAGE
    page_ok     = page_count <= 1.05   # 5% tolerance for rounding

    # ── 3. Action verb rate ─────────────────────────────────────────────────
    if bullets:
        verb_count = sum(1 for b in bullets if _starts_with_action_verb(b))
        verb_rate  = verb_count / len(bullets)
    else:
        verb_rate = 0.0

    # ── 4. Quantification rate (experience bullets) ─────────────────────────
    exp_bullets = experience_bullets or bullets
    if exp_bullets:
        quant_count = sum(1 for b in exp_bullets if re.search(r'\d', b))
        quant_rate  = quant_count / len(exp_bullets)
    else:
        quant_rate = 0.0

    # ── 5. Style issues ─────────────────────────────────────────────────────
    style_issues = _check_style_fast(resume_text)

    # ── Composite score ──────────────────────────────────────────────────────
    raw = (
        coverage  * 40 +
        verb_rate * 15 +
        quant_rate * 20 +
        (25 if page_ok else 0)
    )
    ats_score = max(0, min(100, int(round(raw))))
    passed = ats_score >= 50 and page_ok

    # ── Human-readable issues ────────────────────────────────────────────────
    issues: list[str] = []
    if not page_ok:
        issues.append(
            f"Resume exceeds 1 page (est {page_count:.2f} pages, "
            f"{total_lines} lines). Must be trimmed to 1 page."
        )
    if coverage < 0.4:
        issues.append(
            f"Low keyword coverage {coverage:.0%} — "
            f"{len(gap_kw)} JD keywords missing from resume."
        )
    if verb_rate < 0.6:
        issues.append(
            f"Only {verb_rate:.0%} of bullets start with a strong action verb."
        )
    if quant_rate < 0.3:
        issues.append(
            f"Only {quant_rate:.0%} of bullets contain a quantified result."
        )
    issues.extend(style_issues)

    logger.info(
        "ATS scan: score=%d coverage=%.0f%% page=%.2f verb=%.0f%% quant=%.0f%% passed=%s",
        ats_score, coverage * 100, page_count, verb_rate * 100,
        quant_rate * 100, passed,
    )

    return ATSScanResult(
        keyword_coverage=coverage,
        page_count_est=page_count,
        action_verb_rate=verb_rate,
        quantified_rate=quant_rate,
        style_issues=style_issues,
        ats_score=ats_score,
        passed=passed,
        gap_keywords=gap_kw,
        issues=issues,
    )


def ats_report_text(result: ATSScanResult, job_title: str = "") -> str:
    """Render a plain-text ATS report saved to the artifact folder."""
    lines = [
        f"ATS Scan Report{' — ' + job_title if job_title else ''}",
        "=" * 56,
        f"ATS Score        : {result.ats_score}/100",
        f"Keyword coverage : {result.keyword_coverage:.0%} "
        f"({len(result.gap_keywords)} gap keywords)",
        f"Page count (est) : {result.page_count_est:.2f}",
        f"Action verb rate : {result.action_verb_rate:.0%}",
        f"Quantified rate  : {result.quantified_rate:.0%}",
        f"Result           : {'PASS' if result.passed else 'FAIL'}",
        "",
    ]
    if result.gap_keywords:
        lines.append("Gap keywords (in JD, missing from resume):")
        for kw in result.gap_keywords[:20]:
            lines.append(f"  - {kw}")
        lines.append("")
    if result.issues:
        lines.append("Issues:")
        for issue in result.issues:
            lines.append(f"  ! {issue}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _extract_bullets(lines: list[str]) -> list[str]:
    """Extract bullet-like lines from resume text."""
    bullets = []
    for ln in lines:
        stripped = ln.strip()
        # Lines starting with bullet markers or continuation lines
        if stripped.startswith(("-", "*", "•", "+")):
            bullets.append(stripped.lstrip("-*•+ ").strip())
        elif len(stripped) > 20 and not stripped.endswith(":"):
            # Treat any content line as a potential bullet
            bullets.append(stripped)
    return bullets


def _starts_with_action_verb(bullet: str) -> bool:
    first = bullet.split()[0].lower().rstrip(".,;") if bullet.split() else ""
    return first in _ACTION_VERBS


_AI_LANGUAGE = frozenset({
    "leverage", "leveraged", "utilize", "utilized", "utilise", "utilised",
    "delve", "foster", "facilitate", "streamline", "empower", "spearhead",
    "orchestrate", "harness", "robust", "innovative", "cutting-edge",
    "transformative", "synergy", "seamlessly", "impactful",
})
_EM_DASH_RE = re.compile(r"—|--")


def _check_style_fast(text: str) -> list[str]:
    """Fast style check — returns a list of violation strings."""
    issues = []
    text_lower = text.lower()
    if _EM_DASH_RE.search(text):
        issues.append("Em dash or double-dash found in resume text.")
    for word in _AI_LANGUAGE:
        if word in text_lower:
            issues.append(f"AI-language word '{word}' found in resume text.")
    return issues
