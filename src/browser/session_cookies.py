"""
Browser session cookie persistence.

Saves cookies from a live CDP Chrome session to JSON files and restores
them into Playwright headless contexts — so LinkedIn/Indeed sessions
survive across overnight runs without requiring a re-login.

One-time setup:
  1. Launch JobAgent Chrome (launch_chrome_debug.bat)
  2. Manually log into LinkedIn and Indeed in that Chrome window
  3. Run:  python scripts/save_session.py
  This saves cookies to cookies/linkedin.json and cookies/indeed.json.

Every subsequent headless Playwright session will auto-load these cookies.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_COOKIES_DIR = Path("cookies")
_PLATFORMS = {
    "linkedin": ["linkedin.com", ".linkedin.com"],
    "indeed":   ["indeed.com", ".indeed.com", "smartapply.indeed.com"],
}


def save_cookies_from_page(page, platform: str) -> Path:
    """
    Extract cookies for the given platform from the current page context
    and save them to cookies/{platform}.json.

    platform: 'linkedin' | 'indeed'
    """
    _COOKIES_DIR.mkdir(exist_ok=True)
    out = _COOKIES_DIR / f"{platform}.json"
    try:
        all_cookies = page.context.cookies()
        domains = _PLATFORMS.get(platform, [])
        filtered = [
            c for c in all_cookies
            if any(d in (c.get("domain") or "") for d in domains)
        ]
        out.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
        logger.info("Saved %d %s cookies to %s", len(filtered), platform, out)
        return out
    except Exception as exc:
        logger.error("save_cookies_from_page failed for %s: %s", platform, exc)
        raise


def load_cookies_into_context(context, *platforms: str) -> int:
    """
    Load saved cookies for the given platforms into a Playwright browser context.
    Returns the number of cookies loaded. Silently skips missing files.

    Call before page.goto() to restore the login session.
    """
    all_cookies: list[dict] = []
    for platform in platforms:
        path = _COOKIES_DIR / f"{platform}.json"
        if not path.is_file():
            logger.debug("No saved cookies for %s (file missing: %s)", platform, path)
            continue
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
            all_cookies.extend(cookies)
            logger.info("Loaded %d saved %s cookies", len(cookies), platform)
        except Exception as exc:
            logger.warning("Could not load %s cookies: %s", platform, exc)

    if all_cookies:
        try:
            context.add_cookies(all_cookies)
        except Exception as exc:
            logger.warning("add_cookies failed: %s", exc)
    return len(all_cookies)


def cookies_exist(*platforms: str) -> bool:
    """Return True if cookie files exist for all given platforms."""
    return all((_COOKIES_DIR / f"{p}.json").is_file() for p in platforms)


def clear_cookies(*platforms: str) -> None:
    """Delete saved cookie files (call after a session expires/logout)."""
    for platform in platforms:
        path = _COOKIES_DIR / f"{platform}.json"
        if path.is_file():
            path.unlink()
            logger.info("Cleared saved cookies for %s", platform)
