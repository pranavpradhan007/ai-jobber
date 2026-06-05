"""
AI trap / honeypot detector.

Two entry points:
  detect_traps_in_html(html) — scans a form page for honeypot fields
  detect_traps_in_jd(jd_text) — scans JD text for trap-style screening patterns

Hard rule: if trap_found=True, the application must be skipped immediately.
Never attempt to fill or answer a trap field.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TrapResult:
    trap_found: bool
    trap_type: str    # e.g. "hidden_honeypot", "bot_check_label", "jd_trap_text"
    evidence: str     # snippet that triggered the detection


_TRAP_NONE = TrapResult(trap_found=False, trap_type="", evidence="")

# ---------------------------------------------------------------------------
# JD-text level trap patterns
# ---------------------------------------------------------------------------

# Phrases that suggest the JD itself contains an AI-screening instruction
_JD_TRAP_PATTERNS = [
    re.compile(r"leave\s+this\s+field\s+blank", re.I),
    re.compile(r"do\s+not\s+fill\s+(in\s+)?this", re.I),
    re.compile(r"type\s+the\s+word\s+['\"]?\w+['\"]?", re.I),
    re.compile(r"if\s+you\s+are\s+(a\s+)?human", re.I),
    re.compile(r"bot\s+(check|detection|test)", re.I),
    re.compile(r"anti[\s-]?bot", re.I),
    re.compile(r"honeypot", re.I),
    re.compile(r"only\s+humans?\s+should", re.I),
    re.compile(r"ai\s+agent[s]?\s+(should|must|do\s+not)\s+(not\s+)?(apply|fill|submit)", re.I),
    re.compile(r"no\s+automated\s+applications?", re.I),
    re.compile(r"must\s+be\s+(a\s+)?human", re.I),
]

# ---------------------------------------------------------------------------
# HTML-level trap patterns
# ---------------------------------------------------------------------------

# Input names / ids commonly used as honeypot fields.
# Note: "url" is omitted — ATS forms legitimately use that for portfolio fields.
# "website" is kept because anti-spam libraries use it as a honeypot name and
# legitimate ATS fields use more specific names (websiteUrl, urls[Portfolio], etc.).
_HONEYPOT_NAMES = frozenset({
    "website", "homepage", "company2", "phone2", "address2",
    "email2", "name2", "lastname2", "username2", "fax",
    "leave_blank", "leave-blank", "leaveblank",
    "trap", "honeypot", "bot_check", "bot-check",
    "ohnohoney", "gotcha",
})

# If an input's id or class contains any of these substrings it's blog/CMS
# infrastructure, not an application form field — skip honeypot checks for it.
_BLOG_FIELD_KEYWORDS = frozenset({
    "carousel", "comment-form", "wp-comment", "jetpack", "blog",
})

# Labels / aria-label text that signal honeypot
_HONEYPOT_LABEL_PATTERNS = [
    re.compile(r"leave\s+(this\s+)?(field\s+)?blank", re.I),
    re.compile(r"do\s+not\s+fill", re.I),
    re.compile(r"bot\s+(check|trap|field|test)", re.I),
    re.compile(r"honeypot", re.I),
    re.compile(r"leave\s+empty", re.I),
    re.compile(r"should\s+be\s+empty", re.I),
]

# CSS patterns that hide an element from human users (but not bots)
_HIDDEN_CSS_PATTERNS = [
    re.compile(r"display\s*:\s*none", re.I),
    re.compile(r"visibility\s*:\s*hidden", re.I),
    re.compile(r"opacity\s*:\s*0[^.]", re.I),
    re.compile(r"position\s*:\s*absolute.*left\s*:\s*-\d{3,}", re.I),
    re.compile(r"height\s*:\s*0", re.I),
    re.compile(r"width\s*:\s*0", re.I),
]

# Regex to find <input ...> tags
_INPUT_TAG_RE = re.compile(
    r"<input\b([^>]*)>",
    re.I | re.S,
)

# Strip script/style blocks before scanning for label patterns so that
# JavaScript variable names (e.g. "honeypot_job_card_tst") don't trigger false positives.
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
_STYLE_TAG_RE  = re.compile(r"<style\b[^>]*>.*?</style>",  re.I | re.S)


def _strip_scripts(html: str) -> str:
    html = _SCRIPT_TAG_RE.sub("", html)
    html = _STYLE_TAG_RE.sub("", html)
    return html

# Regex to extract a named attribute from a tag fragment
def _attr(fragment: str, name: str) -> str:
    m = re.search(
        rf'\b{re.escape(name)}\s*=\s*["\']([^"\']*)["\']',
        fragment, re.I,
    )
    return m.group(1) if m else ""


def detect_traps_in_jd(jd_text: str) -> TrapResult:
    """
    Scan a job description text for AI-trap / anti-bot screening patterns.
    Returns TrapResult(trap_found=True, ...) on first match.
    """
    for pat in _JD_TRAP_PATTERNS:
        m = pat.search(jd_text)
        if m:
            evidence = jd_text[max(0, m.start() - 20): m.end() + 20].strip()
            logger.warning(
                "AI trap detected in JD text: type=jd_trap_text evidence=%r",
                evidence[:80],
            )
            return TrapResult(
                trap_found=True,
                trap_type="jd_trap_text",
                evidence=evidence,
            )
    return _TRAP_NONE


def detect_traps_in_html(html: str) -> TrapResult:
    """
    Scan a form page HTML for honeypot / AI-trap fields.

    Checks:
      1. <input> elements whose name/id matches known honeypot names
      2. <input> elements inside a CSS-hidden container (display:none, etc.)
      3. Aria-labels or surrounding labels with trap-pattern text
      4. tabindex="-1" on a text input (visually present but skipped by keyboard users)
    """
    html_lower = html.lower()

    for match in _INPUT_TAG_RE.finditer(html):
        fragment = match.group(1)
        input_name = _attr(fragment, "name").lower()
        input_id   = _attr(fragment, "id").lower()
        input_type = _attr(fragment, "type").lower() or "text"

        # Skip inputs that are legitimately hidden (hidden type = normal pattern)
        if input_type == "hidden":
            continue

        # Skip blog/CMS infrastructure fields (WordPress comment forms, carousels, etc.)
        input_class = _attr(fragment, "class").lower()
        field_context = input_id + " " + input_class
        if any(kw in field_context for kw in _BLOG_FIELD_KEYWORDS):
            continue

        # 1. Honeypot name/id — exact match only (substring would catch e.g. linkedinUrl)
        for hname in _HONEYPOT_NAMES:
            if hname == input_name or hname == input_id:
                evidence = match.group(0)[:120]
                logger.warning(
                    "AI trap: honeypot field name=%r id=%r", input_name, input_id
                )
                return TrapResult(
                    trap_found=True,
                    trap_type="hidden_honeypot",
                    evidence=evidence,
                )

        # 2. CSS-hidden input (likely a honeypot — legitimate hidden fields use type=hidden)
        style_val = _attr(fragment, "style")
        if style_val:
            for css_pat in _HIDDEN_CSS_PATTERNS:
                if css_pat.search(style_val):
                    evidence = match.group(0)[:120]
                    logger.warning(
                        "AI trap: CSS-hidden input name=%r style=%r",
                        input_name, style_val[:60],
                    )
                    return TrapResult(
                        trap_found=True,
                        trap_type="css_hidden_input",
                        evidence=evidence,
                    )

        # 3. tabindex="-1" on a visible text input (screen-reader / bot trap)
        tabindex = _attr(fragment, "tabindex")
        if tabindex == "-1" and input_type in ("text", "email", "tel", "url", ""):
            evidence = match.group(0)[:120]
            logger.warning(
                "AI trap: tabindex=-1 on visible input name=%r", input_name
            )
            return TrapResult(
                trap_found=True,
                trap_type="tabindex_trap",
                evidence=evidence,
            )

    # 4. Label text with honeypot patterns (search HTML with scripts stripped
    #    to avoid false positives on JS variable names like "honeypot_job_card_tst")
    html_no_scripts = _strip_scripts(html)
    for pat in _HONEYPOT_LABEL_PATTERNS:
        m = pat.search(html_no_scripts)
        if m:
            evidence = html[max(0, m.start() - 20): m.end() + 40].strip()
            logger.warning(
                "AI trap: honeypot label text evidence=%r", evidence[:80]
            )
            return TrapResult(
                trap_found=True,
                trap_type="bot_check_label",
                evidence=evidence,
            )

    return _TRAP_NONE
