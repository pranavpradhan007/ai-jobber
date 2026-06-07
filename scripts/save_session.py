"""
One-time browser profile setup — log into LinkedIn and Indeed interactively.

This opens a visible Chromium window with a persistent profile stored in
browser_profile/.  Log into LinkedIn and Indeed in that window, then press
Enter here.  The profile (cookies + localStorage + everything) is saved to
disk permanently and reused on every overnight run — no re-login ever needed.

    python scripts/save_session.py

Re-run only if you explicitly log out of a site inside that browser.
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    from src.browser.auto_submit import _BROWSER_PROFILE_DIR, _ANTI_DETECT_ARGS, _ANTI_DETECT_IGNORE

    profile_dir = os.path.abspath(_BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)
    logger.info("Browser profile directory: %s", profile_dir)
    logger.info("")
    logger.info("A browser window will open. Please:")
    logger.info("  1. Log into LinkedIn:  https://www.linkedin.com/login")
    logger.info("  2. Log into Indeed:    https://secure.indeed.com/account/login")
    logger.info("  3. Come back here and press Enter.")
    logger.info("")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=_ANTI_DETECT_ARGS,
            ignore_default_args=_ANTI_DETECT_IGNORE,
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        input("Press Enter once you are logged into both LinkedIn and Indeed...")

        # Confirm logged-in state
        li_cookies = [c for c in ctx.cookies() if "linkedin" in c.get("domain", "")]
        indeed_cookies = [c for c in ctx.cookies() if "indeed" in c.get("domain", "")]
        logger.info("LinkedIn cookies in profile: %d", len(li_cookies))
        logger.info("Indeed cookies in profile:   %d", len(indeed_cookies))

        ctx.close()

    logger.info("")
    logger.info("Profile saved to %s", profile_dir)
    logger.info("The agent will use this profile automatically on every run.")


if __name__ == "__main__":
    main()
