"""
Style checker — enforces rules/writing_style.md on all generated text.

Em dash (—) is a HARD FAILURE — same severity as an unsupported claim.
Buzzwords are WARNINGS — logged, reported in resume_diff.md, and the
rephraser prompt already bans them; this is a second enforcement layer.

Usage:
    from src.verifier.style_checker import check_style, StyleViolation
    violations = check_style(text)
    hard = [v for v in violations if v.hard_fail]
    if hard:
        raise StyleGateError(...)
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional


# ── Hard failures (gate blocks output) ───────────────────────────────────────

_EM_DASH_RE = re.compile(r"—|(?<!\s)--(?!\s)")   # — or -- used as em dash

HARD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (_EM_DASH_RE, "em dash (—) — use a comma, colon, or rewrite"),
]


# ── Banned words / phrases (warnings) ────────────────────────────────────────

# Each entry: (regex_or_string, suggestion)
_BANNED: list[tuple[str, str]] = [
    # Verbs
    (r"\bleverage[ds]?\b",           "use → 'used' or 'applied'"),
    (r"\butili[sz]e[sd]?\b",         "use → 'used'"),
    (r"\bdelved?\b",                 "use → 'explored' or 'investigated'"),
    (r"\bfoster(ed|ing|s)?\b",       "use → 'built' or 'grew'"),
    (r"\bfacilitate[sd]?\b",         "use → 'enabled' or 'supported'"),
    (r"\bstreamline[sd]?\b",         "use → 'simplified' or 'reduced'"),
    (r"\bempowe(r|red|ring)\b",      "use → 'enabled' or 'let'"),
    (r"\bspearhead(ed|ing|s)?\b",    "use → 'led'"),
    (r"\borchestr(ate|ated|ating)\b","use → 'ran' or 'coordinated'"),
    (r"\bharness(ed|ing|es)?\b",     "use → 'applied' or 'used'"),
    # Adjectives / nouns
    (r"\brobust\b",                  "be specific: 'fault-tolerant', 'tested', 'production-grade'"),
    (r"\binnovative\b",              "describe what is novel instead"),
    (r"\bcutting[- ]edge\b",         "name the actual technology"),
    (r"\btransformative\b",          "state the measurable outcome"),
    (r"\bsynerg(y|ies)\b",           "delete entirely"),
    (r"\bparadigm shift\b",          "delete entirely"),
    (r"\bseamlessly\b",              "describe how it works instead"),
    (r"\bimpactful\b",               "state the impact in numbers"),
    (r"\bpassionate\b",              "delete — show it through the work"),
    (r"\bexcited?\b",                "delete — state the value instead"),
    (r"\bthrilled?\b",               "delete"),
    (r"\bworld[- ]class\b",          "delete"),
    (r"\bground[- ]breaking\b",      "delete"),
    (r"\bexceptional\b",             "delete — let the metric speak"),
    (r"\boutstanding\b",             "delete — let the metric speak"),
    # Filler phrases
    (r"in today'?s (fast[- ]paced|[a-z]+ )?(world|landscape|environment)",
                                     "delete the filler; start with the point"),
    (r"it is (worth noting|important to (mention|note))",
                                     "delete — state the point directly"),
    (r"i hope this (email |message )?finds you well",
                                     "delete"),
    (r"i am writing to express my interest",
                                     "rewrite: open with your strongest credential"),
    (r"i would be (thrilled|excited|delighted)",
                                     "delete — state your value instead"),
    (r"\bhard[- ]working individual\b", "delete"),
    (r"\bteam player\b",             "delete — show collaboration through bullet points"),
    (r"\bresults[- ]driven\b",       "delete"),
    (r"\bdetail[- ]oriented\b",      "delete"),
    (r"\bself[- ]starter\b",         "delete"),
    (r"\bgo[- ]getter\b",            "delete"),
    (r"\bplease don'?t hesitate to (reach out|contact)",
                                     "delete"),
]

_COMPILED_BANNED: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), suggestion)
    for pat, suggestion in _BANNED
]


@dataclass
class StyleViolation:
    text: str           # the matched text
    suggestion: str     # how to fix it
    line: str           # the full line where it appeared
    hard_fail: bool     # True → gate failure; False → warning


class StyleGateError(Exception):
    """Raised when a hard style violation (em dash) is found."""
    def __init__(self, violations: list[StyleViolation]):
        hard = [v for v in violations if v.hard_fail]
        super().__init__(
            f"{len(hard)} hard style violation(s): "
            + "; ".join(f"{v.text!r} in {v.line[:60]!r}" for v in hard[:3])
        )
        self.violations = violations


def check_style(text: str, raise_on_hard: bool = False) -> list[StyleViolation]:
    """
    Scan text for style violations.

    Returns list of StyleViolation.
    If raise_on_hard=True and any hard violations found, raises StyleGateError.
    """
    violations: list[StyleViolation] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Hard checks
        for pattern, suggestion in HARD_PATTERNS:
            for m in pattern.finditer(line):
                violations.append(StyleViolation(
                    text=m.group(0),
                    suggestion=suggestion,
                    line=stripped,
                    hard_fail=True,
                ))

        # Warning checks
        for pattern, suggestion in _COMPILED_BANNED:
            for m in pattern.finditer(line):
                violations.append(StyleViolation(
                    text=m.group(0),
                    suggestion=suggestion,
                    line=stripped,
                    hard_fail=False,
                ))

    if raise_on_hard:
        hard = [v for v in violations if v.hard_fail]
        if hard:
            raise StyleGateError(violations)

    return violations


def style_report(violations: list[StyleViolation]) -> str:
    """Format violations for resume_diff.md."""
    if not violations:
        return "Style check: OK\n"

    hard  = [v for v in violations if v.hard_fail]
    warns = [v for v in violations if not v.hard_fail]

    lines = [f"Style check: {len(hard)} error(s), {len(warns)} warning(s)"]
    if hard:
        lines.append("\nHARD FAILURES (must fix before submission):")
        for v in hard:
            lines.append(f"  FAIL  {v.text!r:30s} → {v.suggestion}")
            lines.append(f"        in: {v.line[:80]}")
    if warns:
        lines.append("\nWARNINGS (AI language to avoid):")
        for v in warns[:20]:   # cap at 20 to keep diff readable
            lines.append(f"  WARN  {v.text!r:30s} → {v.suggestion}")
    return "\n".join(lines) + "\n"
