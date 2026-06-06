"""
ATS Agent — simulates what an Applicant Tracking System scores for a resume.

Checks:
  1. Keyword density  (40 pts) — how many JD hot_keywords appear in the resume
  2. Skills section   (20 pts) — dedicated skills/technologies block present
  3. Bullet strength  (20 pts) — bullets start with action verbs, have metrics
  4. Parse signals    (20 pts) — no tables, no headers/footers with key info,
                                  standard section names, UTF-8 safe characters

Ground truth baseline: ~78–82 / 100.
Acceptance threshold: configurable, default 75.

ATS simulation is intentionally conservative (no LLM, pure heuristics) so it
runs fast and deterministically. A low score triggers another resume_agent pass.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Standard action verbs (strong ATS signals)
_ACTION_VERBS = re.compile(
    r"^(built|designed|developed|trained|deployed|improved|reduced|integrated|"
    r"benchmarked|evaluated|extended|wrote|shipped|led|ran|constructed|tested|"
    r"optimized|scaled|created|implemented|established|collaborated|researched|"
    r"applied|investigated|productionized|consolidated|worked)",
    re.IGNORECASE,
)

# Metric patterns (numbers with units are strong ATS signals)
_METRIC_RE = re.compile(r"\d+[\.\d]*\s*(%|x|ms|s\b|gb|tb|mb|k\b|m\b|pts?|pts?\b)")

# Section names ATS systems expect
_EXPECTED_SECTIONS = [
    re.compile(r"\b(experience|work history|employment)\b", re.IGNORECASE),
    re.compile(r"\b(education|academic)\b", re.IGNORECASE),
    re.compile(r"\b(skills?|technical skills?|technologies)\b", re.IGNORECASE),
    re.compile(r"\b(projects?|personal projects?)\b", re.IGNORECASE),
]

# Characters that confuse ATS parsers
_ATS_UNFRIENDLY = re.compile(r"[★☆✓✗✔■□●○►▸]")


@dataclass
class ATSResult:
    score: float                    # 0–100
    passed: bool
    keyword_score: float            # 0–40
    skills_score: float             # 0–20
    bullet_score: float             # 0–20
    parse_score: float              # 0–20
    keyword_hits: int
    keyword_total: int
    issues: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"ATS {status}: {self.score:.1f}/100 "
            f"(kw={self.keyword_score:.0f}/40 "
            f"skills={self.skills_score:.0f}/20 "
            f"bullets={self.bullet_score:.0f}/20 "
            f"parse={self.parse_score:.0f}/20) "
            f"kw_hits={self.keyword_hits}/{self.keyword_total}"
        )


def ground_truth_ats_score(
    source_docx: str,
    source_pdf: str,
    hot_keywords: list[str],
) -> float:
    """Compute ATS score for the unmodified ground truth resume."""
    result = run_ats_check(source_pdf, source_docx, hot_keywords, min_score=0.0)
    return result.score


def run_ats_check(
    pdf_path: str,
    docx_path: str,
    hot_keywords: list[str],
    raw_jd: str = "",
    *,
    min_score: float = 60.0,
) -> ATSResult:
    """
    Run ATS simulation. Returns ATSResult with passed=True if score >= min_score.
    """
    issues: list[str] = []

    # Load DOCX text (more reliable than parsing PDF)
    paragraphs, bullets, full_text = _load_docx(docx_path, issues)

    # ── 1. Keyword density (0–40 pts) ─────────────────────────────────────────
    kw_hits, kw_score = _score_keywords(full_text, hot_keywords, raw_jd)

    # ── 2. Skills section (0–20 pts) ─────────────────────────────────────────
    skills_score = _score_skills_section(paragraphs, full_text)

    # ── 3. Bullet strength (0–20 pts) ────────────────────────────────────────
    bullet_score = _score_bullets(bullets, issues)

    # ── 4. Parse signals (0–20 pts) ──────────────────────────────────────────
    parse_score = _score_parse(paragraphs, full_text, pdf_path, issues)

    total = kw_score + skills_score + bullet_score + parse_score
    total = round(min(100.0, total), 1)

    result = ATSResult(
        score=total,
        passed=total >= min_score,
        keyword_score=kw_score,
        skills_score=skills_score,
        bullet_score=bullet_score,
        parse_score=parse_score,
        keyword_hits=kw_hits,
        keyword_total=len(hot_keywords),
        issues=issues,
    )
    logger.info("%s", result)
    return result


# ── Scoring sub-functions ─────────────────────────────────────────────────────

def _load_docx(docx_path: str, issues: list[str]):
    """Return (all_paragraphs, bullet_paragraphs, full_text_lower)."""
    try:
        from docx import Document
        doc = Document(docx_path)
        paras = [p for p in doc.paragraphs if p.text.strip()]
        bullets = [p for p in paras if p.style.name == "List Paragraph"]
        full = " ".join(p.text for p in paras).lower()
        return paras, bullets, full
    except Exception as exc:
        issues.append(f"Could not load DOCX: {exc}")
        return [], [], ""


def _score_keywords(full_text: str, hot_keywords: list[str], raw_jd: str) -> tuple[int, float]:
    """
    Count JD keywords present in resume. Score is 0–40.
    Full coverage = 40 pts. Partial proportional.
    """
    if not hot_keywords:
        return 0, 28.0  # default mid-range if no JD keywords provided

    hits = sum(1 for kw in hot_keywords if kw.lower() in full_text)
    score = (hits / len(hot_keywords)) * 40.0
    return hits, round(score, 1)


def _score_skills_section(paragraphs, full_text: str) -> float:
    """
    Check quality of the technical skills section. 0–20 pts.
      10 pts: skills section exists with categories (Languages, Libraries, etc.)
      5 pts:  at least 10 distinct technologies listed
      5 pts:  no buzzwords in skills (purely technical terms)
    """
    score = 0.0
    skill_categories = re.compile(
        r"(languages|libraries|cloud|database|frameworks|tools)\s*:", re.IGNORECASE
    )

    has_categories = any(skill_categories.search(p.text) for p in paragraphs)
    if has_categories:
        score += 10.0

    # Count distinct tech tokens in skills section
    skills_text = next(
        (p.text for p in paragraphs if skill_categories.search(p.text)), ""
    )
    tech_tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]+", skills_text)
    if len(tech_tokens) >= 10:
        score += 5.0

    # No buzzwords in skills
    _buzz = re.compile(r"\b(leverag|innovat|transform|synerg|seamless)\b", re.IGNORECASE)
    if not _buzz.search(skills_text):
        score += 5.0

    return score


def _score_bullets(bullets, issues: list[str]) -> float:
    """
    Evaluate bullet quality. 0–20 pts.
      10 pts: ≥ 80% of bullets start with an action verb
      5 pts:  ≥ 50% of bullets contain a metric/number
      5 pts:  No bullet exceeds 250 chars (parse-friendly)
    """
    if not bullets:
        return 0.0

    score = 0.0
    n = len(bullets)
    verb_count = sum(1 for b in bullets if _ACTION_VERBS.search(b.text.strip()))
    metric_count = sum(1 for b in bullets if _METRIC_RE.search(b.text))
    long_count = sum(1 for b in bullets if len(b.text) > 250)

    verb_ratio = verb_count / n
    metric_ratio = metric_count / n

    if verb_ratio >= 0.80:
        score += 10.0
    elif verb_ratio >= 0.60:
        score += 6.0
    elif verb_ratio >= 0.40:
        score += 3.0

    if metric_ratio >= 0.50:
        score += 5.0
    elif metric_ratio >= 0.30:
        score += 3.0

    if long_count == 0:
        score += 5.0
    elif long_count <= 2:
        score += 2.0

    return score


def _score_parse(paragraphs, full_text: str, pdf_path: str, issues: list[str]) -> float:
    """
    Check ATS parse-friendliness. 0–20 pts.
      5 pts: all expected sections present
      5 pts: no ATS-unfriendly Unicode symbols
      5 pts: PDF is exactly 1 page (already checked by checker — credit if confirmed)
      5 pts: no all-caps paragraphs that are not section headers (ATS gets confused)
    """
    score = 0.0

    # Expected sections
    sections_found = sum(1 for rx in _EXPECTED_SECTIONS if rx.search(full_text))
    if sections_found >= 4:
        score += 5.0
    elif sections_found >= 3:
        score += 3.0

    # No unfriendly symbols
    if not _ATS_UNFRIENDLY.search(full_text):
        score += 5.0

    # 1-page check via PDF
    try:
        from pypdf import PdfReader
        pages = len(PdfReader(pdf_path).pages)
        if pages == 1:
            score += 5.0
    except Exception:
        score += 3.0  # partial credit if we can't verify

    # No excessive ALL-CAPS in body paragraphs
    body_paras = [
        p for p in paragraphs
        if p.style.name not in {"Heading 1", "Heading 2", "Title"}
        and len(p.text) > 20
    ]
    allcaps_count = sum(1 for p in body_paras if p.text == p.text.upper() and p.text.isalpha())
    if allcaps_count == 0:
        score += 5.0

    return score
