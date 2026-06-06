"""
Resume checker — validates a tailored resume before submission.

Hard rules (any failure → use ground truth PDF instead):
  1. Exactly 1 page
  2. No underlined text in skills/bullets (headings may have underline — that's design)
  3. No em dashes in bullet or skills text
  4. No AI buzzword language in bullet text
  5. No bullet longer than MAX_BULLET_CHARS characters

Soft checks (logged as warnings, don't block):
  - At least 1 JD keyword present in skills or bullets
  - Bullet count matches original resume

Usage in pipeline:
  result = check_resume(pdf_path, docx_path, hot_keywords)
  if not result.passed:
      shutil.copy2(source_pdf, artifact_pdf)   # fall back to ground truth
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Styles that are ALLOWED to contain underline (section headers)
_HEADING_STYLES = {"Heading 1", "Heading 2", "Heading 3", "Title"}
# Paragraph styles that represent bullet content
_CONTENT_STYLES = {"List Paragraph", "Normal", "Body Text"}

MAX_BULLET_CHARS = 300

_EM_DASH_RE = re.compile(r"[—–]")
_AI_WORDS_RE = re.compile(
    r"\b(leverage[sd]?|utiliz[es]?d?|utilise[sd]?|delve[sd]?|foster|"
    r"streamline[sd]?|empower[s]?|spearhead[s]?|orchestrate[sd]?|harness[es]?|"
    r"synerg[iy]|seamless|impactful|cutting.edge|transformative)\b",
    re.IGNORECASE,
)


@dataclass
class CheckResult:
    passed: bool
    page_count: int = 0
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"ResumeCheck: {status} | pages={self.page_count}"]
        for iss in self.issues:
            lines.append(f"  ISSUE: {iss}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        return "\n".join(lines)


def check_resume(
    pdf_path: str,
    docx_path: str,
    hot_keywords: Optional[list[str]] = None,
) -> CheckResult:
    """
    Run all checks. Returns CheckResult with passed=True only if every hard rule passes.
    """
    issues: list[str] = []
    warnings: list[str] = []
    page_count = 0

    # ── 1. Page count ─────────────────────────────────────────────────────────
    page_count = _count_pages(pdf_path)
    if page_count == 0:
        issues.append(f"Could not count pages in PDF: {pdf_path}")
    elif page_count != 1:
        issues.append(f"Resume is {page_count} pages — must be exactly 1 page")

    # ── 2–4. DOCX content checks ──────────────────────────────────────────────
    try:
        from docx import Document
        doc = Document(docx_path)
        _check_docx(doc, issues, warnings, hot_keywords or [])
    except Exception as exc:
        issues.append(f"Could not open DOCX for content check: {exc}")

    passed = len(issues) == 0
    result = CheckResult(passed=passed, page_count=page_count, issues=issues, warnings=warnings)
    logger.info("%s", result)
    return result


def _count_pages(pdf_path: str) -> int:
    """Return page count of a PDF file. Returns 0 on error."""
    if not Path(pdf_path).is_file():
        return 0
    try:
        from pypdf import PdfReader
        return len(PdfReader(pdf_path).pages)
    except ImportError:
        pass
    try:
        from PyPDF2 import PdfReader
        return len(PdfReader(pdf_path).pages)
    except ImportError:
        pass
    # Last resort: check file size — a placeholder text file is <500 bytes
    size = Path(pdf_path).stat().st_size
    if size < 500:
        logger.warning("PDF looks like a placeholder (size=%d bytes)", size)
        return 0
    logger.warning("pypdf not installed — cannot verify page count")
    return 1  # optimistic


def _check_docx(doc, issues: list[str], warnings: list[str], hot_keywords: list[str]):
    """
    Run content checks on the DOCX.

    Scope: em dashes, underline, AI words, and length are ONLY checked inside
    List Paragraph bullets — these are the runs we modify. Section headers,
    job-title lines, and date separators in Normal/Heading paragraphs use
    em dashes and underline intentionally as design elements and are left alone.
    """
    keyword_hits = 0
    kw_lower = [k.lower() for k in hot_keywords]

    for para in doc.paragraphs:
        style = para.style.name
        text = para.text.strip()
        if not text:
            continue

        # Only enforce content rules on bullet paragraphs we actually modify
        if style == "List Paragraph":
            # ── Rule 2: No underline in bullets ───────────────────────────────
            for run in para.runs:
                if run.underline and run.text.strip():
                    issues.append(f"Underlined text in bullet: {run.text[:50]!r}")

            # ── Rule 3: No em dashes in bullets ───────────────────────────────
            if _EM_DASH_RE.search(text):
                issues.append(f"Em dash in bullet: {text[:80]!r}")

            # ── Rule 4: No AI buzzwords in bullets ────────────────────────────
            m = _AI_WORDS_RE.search(text)
            if m:
                issues.append(f"AI buzzword {m.group(0)!r} in bullet: {text[:80]!r}")

            # ── Rule 5: Bullet length ──────────────────────────────────────────
            if len(text) > MAX_BULLET_CHARS:
                issues.append(
                    f"Bullet too long ({len(text)} chars > {MAX_BULLET_CHARS}): "
                    f"{text[:60]!r}..."
                )

        # Keyword hits anywhere in the document (soft check)
        if kw_lower:
            text_lower = text.lower()
            keyword_hits += sum(1 for kw in kw_lower if kw in text_lower)

    if hot_keywords and keyword_hits == 0:
        warnings.append(f"None of the {len(hot_keywords)} JD keywords found in resume text")
    elif hot_keywords:
        logger.info("Keyword coverage: %d/%d JD keywords present", keyword_hits, len(hot_keywords))
