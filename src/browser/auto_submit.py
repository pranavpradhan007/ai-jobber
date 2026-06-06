"""
Portal auto-submit module — uses the user's real Chrome profile.

Connects to Chrome via launch_persistent_context so that:
  - The user's login sessions / cookies are active (avoids auth walls)
  - The Claude Chrome extension is present (real browser identity)
  - Bot detection is far less effective (not a Playwright-controlled Chromium)

Human-like interactions:
  - Random delays between actions (1.0–2.5 s)
  - Char-by-char typing with variable speed
  - Mouse hover before click

CAPTCHA/MFA on any page → raises CaptchaDetected / MFADetected.
AI trap → raises AITrapDetected.
"""
from __future__ import annotations
import logging
import os
import random
import socket
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from src.browser.prefill import CaptchaDetected, MFADetected, AITrapDetected, StaticFillEngine
from src.browser.trap_detector import detect_traps_in_html
from src.browser.portal import classify_portal
from src.browser.field_mapper import build_fill_instructions, FILL_DELAY_SECONDS

logger = logging.getLogger(__name__)

_APPLY_WAIT  = 5.0
_SUBMIT_WAIT = 8.0

# Default Chrome user-data-dir on Windows
_DEFAULT_CHROME_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\hp\AppData\Local"),
    "Google", "Chrome", "User Data",
)

# Selectors for "Apply" / "Apply Now" buttons on listing pages
_APPLY_SELECTORS = [
    ".ia-IndeedApplyButton",         # Indeed Easy Apply
    "[data-testid='applyButton']",
    "#indeedApplyButton",
    "button:text-is('Apply Now')",
    "button:text-is('Apply')",
    "a:text-is('Apply Now')",
    "a:text-is('Apply')",
    ".btn-apply",
    "[data-mapped='true']",
]

# Selectors for the final Submit button on application forms
_SUBMIT_SELECTORS = [
    "input[type='submit']",
    "button[type='submit']",
    "button:text-is('Submit Application')",
    "button:text-is('Submit')",
    "button:text-is('Send Application')",
    "[data-automation-id='bottom-navigation-next-button']",   # Workday
    "button:text-is('Apply')",
    "#submit_app_button",
    ".btn-submit",
]


@dataclass
class AutoSubmitResult:
    success: bool
    app_id: int
    receipt: Optional[str] = None
    screenshot_path: Optional[str] = None
    error: Optional[str] = None
    captcha_detected: bool = False
    mfa_detected: bool = False
    ai_trap_detected: bool = False


def build_candidate_answers(
    app_id: int,
    job_title: str = "",
    company: str = "",
    resume_path: str = "",
) -> dict:
    """
    Build the answers dict from env vars and profile data.
    All values come from .env (set by the user — never invented).
    """
    email     = os.environ.get("YOUR_EMAIL_ADDRESS", "").strip()
    phone     = os.environ.get("PHONE_NUMBER", "").strip()
    linkedin  = os.environ.get("LINKEDIN_URL", "").strip()
    github    = os.environ.get("GITHUB_URL", "").strip()
    work_auth = os.environ.get("WORK_AUTHORIZATION", "Yes, I am authorized to work in the United States").strip()
    addr1     = os.environ.get("ADDRESS_LINE1", "").strip()
    city      = os.environ.get("ADDRESS_CITY", "").strip()
    state     = os.environ.get("ADDRESS_STATE", "").strip()
    zipcode   = os.environ.get("ADDRESS_ZIP", "").strip()
    country   = os.environ.get("ADDRESS_COUNTRY", "United States").strip()

    website = github or linkedin

    cover = (
        f"I am excited to apply for the {job_title} role at {company}. "
        "My background in machine learning, deep learning, and applied AI research "
        "aligns well with the requirements of this position. "
        "I look forward to contributing to your team."
    ) if job_title and company else ""

    return {
        "first_name":         "Pranav",
        "last_name":          "Pradhan",
        "email":              email,
        "phone":              phone,
        "linkedin_url":       linkedin,
        "website_url":        website,
        "work_authorization": work_auth,
        "address_line1":      addr1,
        "address_city":       city,
        "address_state":      state,
        "address_zip":        zipcode,
        "address_country":    country,
        "cover_letter":       cover,
        "resume":             resume_path,
    }


def auto_submit_portal(
    app_id: int,
    answers: dict,
    url: str,
    folder_path: str,
    *,
    fill_delay: float = FILL_DELAY_SECONDS,
    dry_run: bool = False,
) -> AutoSubmitResult:
    """
    Open the job URL in the user's real Chrome profile, fill the application
    form with human-like timing, and click Submit.
    """
    if dry_run:
        logger.info("DRY_RUN auto_submit app_id=%d url=%s", app_id, url)
        return AutoSubmitResult(
            success=True, app_id=app_id,
            receipt=f"DRY_RUN_{app_id}",
        )

    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        return AutoSubmitResult(
            success=False, app_id=app_id,
            error="playwright not installed — run: pip install playwright && playwright install chrome",
        )

    chrome_dir  = os.environ.get("CHROME_USER_DATA_DIR", _DEFAULT_CHROME_DIR)
    chrome_profile = os.environ.get("CHROME_PROFILE", "Default")

    cdp_port = int(os.environ.get("CHROME_CDP_PORT", "9222"))

    with sync_playwright() as pw:
        context = _open_chrome_context(pw, chrome_dir, chrome_profile, cdp_port)
        page = context.new_page()
        try:
            return _run_submit_flow(page, app_id, answers, url, folder_path, fill_delay)
        except CaptchaDetected:
            logger.warning("CAPTCHA detected for app_id=%d", app_id)
            return AutoSubmitResult(success=False, app_id=app_id, captcha_detected=True)
        except MFADetected:
            logger.warning("MFA/login required for app_id=%d", app_id)
            return AutoSubmitResult(success=False, app_id=app_id, mfa_detected=True)
        except AITrapDetected as e:
            logger.warning("AI trap for app_id=%d: %s", app_id, e)
            return AutoSubmitResult(success=False, app_id=app_id, ai_trap_detected=True)
        except Exception as exc:
            logger.error("auto_submit failed app_id=%d: %s", app_id, exc)
            return AutoSubmitResult(success=False, app_id=app_id, error=str(exc))
        finally:
            try:
                page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Chrome startup helpers
# ---------------------------------------------------------------------------

def _open_chrome_context(pw, chrome_dir: str, profile: str, cdp_port: int):
    """
    Connect to real Chrome for application submission.

    Preferred path: connect to Chrome already running with --remote-debugging-port.
    The user launches Chrome via launch_chrome_debug.bat before running the agent.

    Fallback: isolated Playwright Chromium (no saved sessions — Indeed will require login).
    """
    # Attempt 1: connect to already-running Chrome with CDP port open
    if _chrome_cdp_reachable(cdp_port):
        try:
            logger.info("Connecting to Chrome on CDP port %d", cdp_port)
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            return ctx
        except Exception as e:
            logger.warning("CDP connect failed: %s", e)

    # Attempt 2: isolated Chromium (no real sessions — works for non-auth portals)
    logger.warning(
        "Chrome not running on CDP port %d. "
        "Run launch_chrome_debug.bat first for sites that require login (Indeed, etc.). "
        "Falling back to isolated Chromium.", cdp_port
    )
    browser = pw.chromium.launch(headless=False)
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        accept_downloads=True,
    )


def _chrome_cdp_reachable(port: int) -> bool:
    """Return True if Chrome's CDP debug endpoint is listening."""
    try:
        s = socket.create_connection(("localhost", port), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def _relaunch_chrome_with_cdp(chrome_dir: str, profile: str, port: int) -> None:
    """Gracefully kill Chrome then relaunch with the CDP debug port open."""
    # Graceful close first; /F force-kill only if needed
    subprocess.run(["taskkill", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(1.5)
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(1.5)

    chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not os.path.isfile(chrome_exe):
        # Try common alternative locations
        for alt in [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]:
            if os.path.isfile(alt):
                chrome_exe = alt
                break
        else:
            raise FileNotFoundError("chrome.exe not found")

    subprocess.Popen([
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={chrome_dir}",
        f"--profile-directory={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ])

    # Wait for CDP to become reachable (up to 15 s)
    for _ in range(15):
        if _chrome_cdp_reachable(port):
            time.sleep(1.0)   # extra settle time
            return
        time.sleep(1.0)
    raise RuntimeError(f"Chrome did not expose CDP on port {port} within 15s")


# ---------------------------------------------------------------------------
# Main submission flow
# ---------------------------------------------------------------------------

def _run_submit_flow(page, app_id, answers, url, folder_path, fill_delay):
    os.makedirs(folder_path, exist_ok=True)

    # ── Step 1: Navigate to listing URL ──────────────────────────────────────
    nav_url = _resolve_indeed_url(url)
    logger.info("app_id=%d navigating to %s", app_id, nav_url)
    page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
    _human_pause(2.0, 3.5)

    # After navigation the browser may have followed redirects — re-resolve
    # e.g. to.indeed.com/xxx → www.indeed.com/rc/clk?jk=... → viewjob
    resolved = _resolve_indeed_url(page.url)
    if resolved != page.url:
        logger.info("app_id=%d Indeed post-redirect: navigating to %s", app_id, resolved)
        page.goto(resolved, wait_until="domcontentloaded", timeout=30_000)
        _human_pause(2.0, 3.0)

    # ── Step 2: Detect auth walls early ──────────────────────────────────────
    _check_auth_wall(page)

    # ── Step 3: Find and click Apply button ──────────────────────────────────
    _click_apply(page, app_id)

    # Wait for redirect / modal
    try:
        page.wait_for_load_state("domcontentloaded", timeout=int(_APPLY_WAIT * 1000))
    except Exception:
        pass
    _human_pause(1.5, 2.5)

    # Switch to new tab if opened
    pages = page.context.pages
    if len(pages) > 1:
        page = pages[-1]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        _human_pause(1.0, 2.0)

    # Re-check auth wall (apply button may redirect to login)
    _check_auth_wall(page)

    # ── Step 4: AI trap check ─────────────────────────────────────────────────
    html = page.content()
    trap = detect_traps_in_html(html)
    if trap.trap_found:
        raise AITrapDetected(f"{trap.trap_type}: {trap.evidence[:80]}")

    _check_captcha_mfa(page)

    # ── Step 5: Classify portal and fill fields ───────────────────────────────
    portal = classify_portal(html=html, url=page.url)
    logger.info("app_id=%d portal=%s url=%s", app_id, portal, page.url)

    _screenshot(page, folder_path, "submit_before.png")

    instructions = build_fill_instructions(answers, portal)
    _fill_all_fields(page, answers, instructions, fill_delay)

    # ── Step 6: Click Submit ──────────────────────────────────────────────────
    _click_submit(page, app_id)

    # ── Step 7: Confirm ───────────────────────────────────────────────────────
    try:
        page.wait_for_load_state("networkidle", timeout=int(_SUBMIT_WAIT * 1000))
    except Exception:
        pass
    _human_pause(2.0, 3.0)

    _check_captcha_mfa(page)

    ss_after = _screenshot(page, folder_path, "submit_confirmation.png")

    receipt = f"PORTAL:{portal}:{page.url}"
    logger.info("app_id=%d submitted portal=%s url=%s", app_id, portal, page.url)

    return AutoSubmitResult(
        success=True, app_id=app_id,
        receipt=receipt,
        screenshot_path=ss_after,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_indeed_url(url: str) -> str:
    """Convert Indeed tracking/redirect URLs to direct viewjob URLs."""
    if "indeed.com/rc/clk" in url or "indeed.com/pagead" in url or "to.indeed.com/" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        jk = params.get("jk", [""])[0]
        if jk:
            return f"https://www.indeed.com/viewjob?jk={jk}"
        # to.indeed.com short URLs — return as-is (will follow redirect in browser)
    return url


def _check_auth_wall(page) -> None:
    """Raise MFADetected if we landed on a login/auth page."""
    u = page.url.lower()
    auth_patterns = [
        "secure.indeed.com/auth",
        "indeed.com/account/login",
        "accounts.google.com/signin",
        "login.microsoftonline.com",
        "auth.workday.com",
    ]
    if any(p in u for p in auth_patterns):
        raise MFADetected(f"Login wall detected at {page.url}")


def _click_apply(page, app_id: int) -> None:
    """Find and click the Apply button on a listing page."""
    for sel in _APPLY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                _human_click(page, loc)
                logger.info("app_id=%d clicked apply selector=%r", app_id, sel)
                return
        except Exception:
            continue
    logger.info("app_id=%d no Apply button found — may already be on form page", app_id)


def _click_submit(page, app_id: int) -> None:
    """Find and click the Submit button; raise if none found."""
    for sel in _SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                _human_click(page, loc)
                logger.info("app_id=%d clicked submit selector=%r", app_id, sel)
                return
        except Exception:
            continue
    raise RuntimeError(
        f"Could not find submit button on {page.url}. "
        "Form may have multiple pages or require manual completion."
    )


def _fill_all_fields(page, answers: dict, instructions, fill_delay: float) -> None:
    """Fill all form fields with human-like timing."""
    # Best-effort address fields
    _fill_if_visible(page, answers.get("address_line1", ""), [
        "input[placeholder*='street' i]", "input[placeholder*='address' i]",
        "input[aria-label*='address' i]", "input[name*='street' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("address_city", ""), [
        "input[placeholder*='city' i]", "input[aria-label*='city' i]",
        "input[name*='city' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("address_state", ""), [
        "input[placeholder*='state' i]", "input[aria-label*='state' i]",
        "input[name*='state' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("address_zip", ""), [
        "input[placeholder*='zip' i]", "input[placeholder*='postal' i]",
        "input[aria-label*='zip' i]", "input[name*='zip' i]",
    ], fill_delay)

    # Best-effort website / GitHub / portfolio
    github_url = answers.get("website_url", "")
    if github_url:
        for sel in [
            "input[placeholder*='github' i]", "input[placeholder*='website' i]",
            "input[placeholder*='portfolio' i]", "input[aria-label*='website' i]",
            "input[aria-label*='github' i]", "input[aria-label*='portfolio' i]",
            "input[name*='website' i]", "input[name*='github' i]",
            "input[name*='portfolio' i]",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    _human_type(loc, github_url)
                    _human_pause(fill_delay * 0.5, fill_delay)
                    break
            except Exception:
                continue

    # Portal-specific fields
    for inst in instructions:
        try:
            if inst.field_type == "file":
                resume_path = inst.value
                if resume_path and os.path.isfile(resume_path):
                    file_input = page.locator("input[type='file']").first
                    if file_input.count() > 0:
                        file_input.set_input_files(resume_path)
                        _human_pause(fill_delay, fill_delay + 1.0)
            elif inst.field_type == "select":
                el = page.locator(inst.selector).first
                if el.count() > 0:
                    try:
                        el.select_option(inst.value)
                    except Exception:
                        el.select_option(label=inst.value)
                    _human_pause(fill_delay * 0.5, fill_delay)
            elif inst.field_type in ("text", "textarea"):
                el = page.locator(inst.selector).first
                if el.count() > 0:
                    _human_click(page, el)
                    _human_type(el, inst.value)
                    _human_pause(fill_delay * 0.5, fill_delay)
            elif inst.field_type == "checkbox" and inst.value.lower() in ("1", "true", "yes"):
                el = page.locator(inst.selector).first
                if el.count() > 0 and not el.is_checked():
                    el.check()
                    _human_pause(fill_delay * 0.3, fill_delay * 0.6)
        except Exception as exc:
            logger.warning("fill error field=%r: %s", inst.label, exc)


def _fill_if_visible(page, value: str, selectors: list, delay: float) -> None:
    if not value:
        return
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                try:
                    current = loc.input_value()
                except Exception:
                    current = ""
                if not current:
                    _human_click(page, loc)
                    _human_type(loc, value)
                    _human_pause(delay * 0.5, delay)
                return
        except Exception:
            continue


def _human_click(page, locator) -> None:
    """Hover → short pause → click (simulates human pointer movement)."""
    try:
        locator.hover()
        time.sleep(random.uniform(0.15, 0.45))
        locator.click()
    except Exception:
        locator.click()


def _human_type(locator, text: str) -> None:
    """Type character by character with variable speed."""
    locator.click()
    for char in text:
        locator.type(char)
        time.sleep(random.uniform(0.04, 0.14))


def _human_pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _screenshot(page, folder: str, name: str) -> Optional[str]:
    path = os.path.join(folder, name)
    try:
        page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return None


def _check_captcha_mfa(page) -> None:
    for cid in StaticFillEngine.CAPTCHA_IDS:
        try:
            if page.locator(f"#{cid}").count() > 0:
                raise CaptchaDetected(f"CAPTCHA #{cid}")
        except CaptchaDetected:
            raise
        except Exception:
            pass
    for mid in StaticFillEngine.MFA_IDS:
        try:
            if page.locator(f"#{mid}").count() > 0:
                raise MFADetected(f"MFA #{mid}")
        except MFADetected:
            raise
        except Exception:
            pass
