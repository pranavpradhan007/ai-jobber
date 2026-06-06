"""
Save LinkedIn and Indeed session cookies from a live Chrome CDP session.

Run this ONCE after you've manually logged into LinkedIn and Indeed in your
JobAgent Chrome window (launched via launch_chrome_debug.bat):

    python scripts/save_session.py

Cookies are saved to cookies/linkedin.json and cookies/indeed.json.
Every subsequent Playwright fallback session will auto-load these cookies
so you don't have to sign in again.

Re-run this script if you get logged out or session expires.
"""
from __future__ import annotations
import sys
import os

# Make sure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def main():
    cdp_port = int(os.environ.get("CHROME_CDP_PORT", "9222"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && playwright install chrome")
        sys.exit(1)

    from src.browser.session_cookies import save_cookies_from_page, cookies_exist

    logger.info("Connecting to Chrome on CDP port %d ...", cdp_port)
    logger.info("Make sure Chrome is running (launch_chrome_debug.bat) and you are logged in.")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
        except Exception as exc:
            logger.error(
                "Could not connect to Chrome on port %d: %s\n"
                "Launch Chrome first via launch_chrome_debug.bat",
                cdp_port, exc,
            )
            sys.exit(1)

        if not browser.contexts:
            logger.error("No browser contexts found. Open Chrome and log into LinkedIn/Indeed first.")
            sys.exit(1)

        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Check LinkedIn cookies
        all_cookies = ctx.cookies()
        linkedin_cookies = [c for c in all_cookies if "linkedin" in (c.get("domain") or "")]
        indeed_cookies   = [c for c in all_cookies if "indeed"   in (c.get("domain") or "")]

        logger.info("Found %d LinkedIn cookies and %d Indeed cookies in current session",
                    len(linkedin_cookies), len(indeed_cookies))

        if not linkedin_cookies and not indeed_cookies:
            logger.warning(
                "No LinkedIn or Indeed cookies found. "
                "Please log into both sites in Chrome first, then re-run this script."
            )

        saved = []
        if linkedin_cookies:
            out = save_cookies_from_page(page, "linkedin")
            saved.append(f"LinkedIn ({len(linkedin_cookies)} cookies) → {out}")

        if indeed_cookies:
            out = save_cookies_from_page(page, "indeed")
            saved.append(f"Indeed ({len(indeed_cookies)} cookies) → {out}")

        if saved:
            logger.info("Saved sessions:")
            for s in saved:
                logger.info("  %s", s)
            logger.info(
                "\nDone. The agent will now auto-load these cookies in fallback Playwright sessions."
            )
        else:
            logger.warning("No sessions were saved. Log into LinkedIn/Indeed in Chrome and retry.")


if __name__ == "__main__":
    main()
