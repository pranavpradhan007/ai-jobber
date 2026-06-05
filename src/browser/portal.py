"""
Portal classifier.
Detects the ATS platform from page HTML/URL so the field mapper
knows which selectors to use.

Supported: workday | greenhouse | lever | ashby | custom
Detection is entirely static — no live browser calls needed.
"""
from __future__ import annotations
import re
from typing import Optional


SUPPORTED_PORTALS = frozenset(
    {"workday", "greenhouse", "lever", "ashby", "custom"}
)


def classify_portal(
    html: str = "",
    url: str = "",
) -> str:
    """
    Return the portal name ('workday', 'greenhouse', etc.) or 'custom'.
    Checks URL patterns first, then HTML signatures.
    """
    url_lower = url.lower()
    html_lower = html.lower()

    # ── URL-based detection ──────────────────────────────────────────────────
    if "myworkday.com" in url_lower or "wd1.myworkdayjobs.com" in url_lower:
        return "workday"
    if "greenhouse.io" in url_lower or "boards.greenhouse.io" in url_lower:
        return "greenhouse"
    if "lever.co" in url_lower or "jobs.lever.co" in url_lower:
        return "lever"
    if "ashbyhq.com" in url_lower or "jobs.ashbyhq.com" in url_lower:
        return "ashby"

    # ── HTML signature detection ──────────────────────────────────────────────
    if any(sig in html_lower for sig in (
        "workday-application", "wd-application-form", '"generator" content="workday"',
        "myworkdayjobs", "wd5-allinone",
    )):
        return "workday"

    if any(sig in html_lower for sig in (
        "greenhouse-app", 'id="application_form"', 'name="gh-application"',
        "boards.greenhouse.io", "gh_jid",
    )):
        return "greenhouse"

    if any(sig in html_lower for sig in (
        "lever-jobs", "jobs.lever.co", "lever-application",
    )):
        return "lever"

    if any(sig in html_lower for sig in (
        "ashby", "ashbyhq",
    )):
        return "ashby"

    return "custom"
