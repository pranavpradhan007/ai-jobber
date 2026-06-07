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
from src.browser.human_mouse import reading_pause, page_transition_pause

logger = logging.getLogger(__name__)

_APPLY_WAIT   = 5.0
_SUBMIT_WAIT  = 8.0
FAST_FILL_DELAY = 0.15   # inter-field pause in fast path (replaces 2.0 s)

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
    "[class*='IndeedApplyButton']",
    "[data-tn-element='apply-now']",
    "[data-tn-element='indeedApplyButton']",
    "button[data-id*='apply']",
    "button:has-text('Apply Now')",
    "button:has-text('Apply')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply')",
    ".btn-apply",
    "[data-mapped='true']",
    "span:has-text('Apply Now')",
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

# Indeed SmartApply multi-page form selectors
# Use has-text (partial match) — text-is requires exact match and fails on whitespace differences
_SMARTAPPLY_CONTINUE_SELECTORS = [
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button[data-testid='IndeedApplyButton']",
    "[data-automation-id='continue-button']",
    "button[data-tn-element='continue-button']",
    "button[data-tn-element='apply-now']",
    ".ia-continueButton",
    ".ia-Button--primary",
    "[role='button']:has-text('Continue')",
    "a:has-text('Continue')",
    "button:has-text('Apply')",
    "button:has-text('Start')",
    # Catch-all: any primary/cta button not labelled cancel/back/close
    "button[type='submit']:not([aria-label*='cancel' i]):not([aria-label*='back' i])",
]

_SMARTAPPLY_SUBMIT_SELECTORS = [
    "button:has-text('Submit your application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button[aria-label*='submit' i]",
    "[role='button']:has-text('Submit')",
]

_SMARTAPPLY_HOST = "smartapply.indeed.com"


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


_cached_profile: dict | None = None


def _load_application_answers() -> dict:
    """Load application_answers.json once and cache in memory."""
    global _cached_profile
    if _cached_profile is not None:
        return _cached_profile
    profile_path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "knowledge_base", "profile", "application_answers.json",
    )
    try:
        import json as _json
        with open(profile_path, encoding="utf-8") as fh:
            _cached_profile = _json.load(fh)
    except Exception:
        _cached_profile = {}
    return _cached_profile


def build_candidate_answers(
    app_id: int,
    job_title: str = "",
    company: str = "",
    resume_path: str = "",
    *,
    screener_answers: dict | None = None,
) -> dict:
    """
    Build the answers dict from env vars, application_answers.json, and optional
    pre-computed screener answers. Env-var values always win for core contact fields.
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

    answers: dict = {
        "first_name":         "Pranav",
        "last_name":          "Pradhan",
        "full_name":          "Pranav Tushar Pradhan",
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
        "cover_letter_default": cover,
        "resume":             resume_path,
    }

    # Merge static profile data from application_answers.json (lower priority than env)
    profile = _load_application_answers()
    for key in (
        "github_url", "portfolio_url", "salary_expectation", "salary_display",
        "years_experience", "requires_sponsorship", "us_citizen_or_pr", "visa_status",
        "education_degree", "education_school", "graduation_year", "current_title",
        "willing_to_relocate", "remote_preference", "linkedin_headline",
        "full_name", "location",
    ):
        if key in profile and profile[key] and not answers.get(key):
            answers[key] = profile[key]

    # Merge pre-computed screener answers at lowest priority (open-ended Q answers)
    if screener_answers:
        for k, v in screener_answers.items():
            if v and not answers.get(k):
                answers[k] = v

    return answers


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

_BROWSER_PROFILE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "browser_profile"
)

_ANTI_DETECT_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

_ANTI_DETECT_IGNORE = ["--enable-automation"]


def _open_chrome_context(pw, chrome_dir: str, profile: str, cdp_port: int):
    """
    Connect to real Chrome for application submission.

    Attempt 1: CDP — connect to Chrome already running with --remote-debugging-port.
    Attempt 2: Persistent Playwright profile — sessions survive forever on disk;
               user logs in once via scripts/save_session.py, never again.
    """
    # Attempt 1: connect to already-running Chrome with CDP port open
    if _chrome_cdp_reachable(cdp_port):
        try:
            logger.info("Connecting to Chrome on CDP port %d", cdp_port)
            browser = pw.chromium.connect_over_cdp(
                f"http://localhost:{cdp_port}",
                timeout=15000,
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            return ctx
        except Exception as e:
            logger.warning("CDP connect failed: %s", e)

    # Attempt 2: persistent Playwright profile — cookies/localStorage persist on disk
    profile_dir = os.path.abspath(_BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)
    _release_profile_lock(profile_dir)
    logger.info("Using persistent browser profile at %s", profile_dir)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        viewport={"width": 1280, "height": 900},
        accept_downloads=True,
        args=_ANTI_DETECT_ARGS,
        ignore_default_args=_ANTI_DETECT_IGNORE,
    )
    # Inject webdriver flag removal on every page so sites don't detect automation
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return ctx


def _release_profile_lock(profile_dir: str) -> None:
    """Kill any Chrome/Chromium processes holding a lock on this profile dir."""
    import subprocess
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             f"CommandLine like '%{profile_dir}%' and Name like '%chrome%'",
             "get", "ProcessId", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("ProcessId="):
                pid = line.split("=", 1)[1].strip()
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=5)
                    logger.info("Killed stale browser process PID=%s holding profile", pid)
        # Also remove Chrome's SingletonLock file if present
        lock = os.path.join(profile_dir, "SingletonLock")
        if os.path.exists(lock):
            os.remove(lock)
            logger.info("Removed SingletonLock from profile dir")
    except Exception as exc:
        logger.debug("_release_profile_lock: %s", exc)


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

    # ── Step 0: LinkedIn Easy Apply dispatch ─────────────────────────────────
    # Only dispatch to LinkedIn handler for original job-view URLs, not when
    # called recursively from the external-apply fallback path.
    if "linkedin.com/jobs/view" in url or "linkedin.com/jobs/search" in url:
        logger.info("app_id=%d detected LinkedIn job URL — using Easy Apply handler", app_id)
        return _run_linkedin_easy_apply(page, app_id, answers, url, folder_path, fill_delay)

    # ── Step 1: Navigate to listing URL ──────────────────────────────────────
    nav_url = _resolve_indeed_url(url)
    logger.info("app_id=%d navigating to %s", app_id, nav_url)
    page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
    reading_pause()   # 1.5-3.5 s — simulate reading the page

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

    # ── Step 4: Check if Indeed SmartApply (multi-page form) ─────────────────
    _is_smartapply = (
        _SMARTAPPLY_HOST in page.url
        or "indeed.com/apply" in page.url
        or "indeed.com/viewjob" not in page.url and "indeed.com" in page.url and "/apply" in page.url
    )
    if _is_smartapply:
        # Wait up to 5s for SmartApply SPA to finish rendering
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        logger.info("app_id=%d detected SmartApply form, starting page loop", app_id)
        _handle_smartapply_pages(page, app_id, answers, folder_path, fill_delay)
        ss_after = _screenshot(page, folder_path, "submit_confirmation.png")
        receipt = f"PORTAL:indeed_smartapply:{page.url}"
        logger.info("app_id=%d SmartApply submitted url=%s", app_id, page.url)
        return AutoSubmitResult(
            success=True, app_id=app_id,
            receipt=receipt, screenshot_path=ss_after,
        )

    # ── Step 5: AI trap check ─────────────────────────────────────────────────
    html = page.content()
    trap = detect_traps_in_html(html)
    if trap.trap_found:
        raise AITrapDetected(f"{trap.trap_type}: {trap.evidence[:80]}")

    _check_captcha_mfa(page)

    # ── Step 6: Classify portal and fill fields ───────────────────────────────
    portal = classify_portal(html=html, url=page.url)
    logger.info("app_id=%d portal=%s url=%s", app_id, portal, page.url)

    _screenshot(page, folder_path, "submit_before.png")

    # Fast universal path: JS-detected fields → human-like fill
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form, verify_form_complete

    detected = extract_form_fields(page)
    if detected:
        logger.info("app_id=%d fast-fill: %d fields detected", app_id, len(detected))
        ff = fast_fill_form(page, detected, answers)
        logger.info(
            "app_id=%d fast-fill: filled=%d skipped=%d unmatched=%d unmatched_labels=%s",
            app_id, ff.fields_filled, ff.fields_skipped, ff.fields_unmatched,
            ff.unmatched_labels[:5],
        )
        # Fall back to legacy selector-based fill for any remaining unmatched fields
        if ff.fields_unmatched > 3:
            logger.info("app_id=%d unmatched fields > 3, running legacy fill as supplement", app_id)
            instructions = build_fill_instructions(answers, portal)
            _fill_all_fields(page, answers, instructions, FAST_FILL_DELAY)
    else:
        # form_detector returned nothing — use legacy selector-based fill
        logger.info("app_id=%d form_detector found 0 fields, using legacy fill", app_id)
        instructions = build_fill_instructions(answers, portal)
        _fill_all_fields(page, answers, instructions, fill_delay)

    # Verify form is complete before attempting submit
    complete, issues = verify_form_complete(page)
    if not complete:
        logger.warning("app_id=%d form completion check: %s", app_id, issues)
        _screenshot(page, folder_path, "submit_incomplete.png")

    # ── Step 7: Click Submit ──────────────────────────────────────────────────
    _click_submit(page, app_id)

    # ── Step 8: Confirm ───────────────────────────────────────────────────────
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
    """Convert Indeed tracking/redirect URLs to direct viewjob URLs.

    Handles:
    - /rc/clk?jk=...  → viewjob?jk=...
    - to.indeed.com/xxx → browser follows redirect → search page with vjk= param
    - indeed.com/jobs?...&vjk=... → viewjob?jk=... (post-redirect case)
    """
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)

    if "indeed.com/rc/clk" in url or "indeed.com/pagead" in url or "to.indeed.com/" in url:
        jk = params.get("jk", [""])[0]
        if jk:
            return f"https://www.indeed.com/viewjob?jk={jk}"
        # to.indeed.com short URL — return as-is; browser will follow redirect
        return url

    # After following to.indeed.com redirect, browser may land on search page
    # with vjk= parameter pointing to the actual job listing.
    if "indeed.com/jobs" in url:
        vjk = params.get("vjk", [""])[0]
        if vjk:
            return f"https://www.indeed.com/viewjob?jk={vjk}"

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

    # Indeed viewjob fallback: extract jk param and navigate directly to SmartApply
    import re as _re
    jk_m = _re.search(r'[?&]jk=([a-f0-9]+)', page.url)
    if jk_m and "indeed.com/viewjob" in page.url:
        smartapply_url = f"https://www.indeed.com/apply/start?jk={jk_m.group(1)}&from=viewjobDesktop"
        logger.info("app_id=%d Indeed viewjob fallback: navigating to SmartApply %s", app_id, smartapply_url)
        page.goto(smartapply_url, wait_until="domcontentloaded", timeout=30_000)
        _human_pause(2.0, 3.0)
        return

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


def _handle_smartapply_pages(page, app_id: int, answers: dict, folder_path: str, fill_delay: float) -> None:
    """Step through Indeed SmartApply multi-page form until submitted."""
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form, verify_form_complete
    max_steps = 12
    for step in range(max_steps):
        page_transition_pause()
        # Wait for page to be interactive (SmartApply uses SPA routing)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        _check_auth_wall(page)
        # Note: SmartApply embeds an invisible reCAPTCHA on every page.
        # _check_captcha_mfa would false-positive on it. We only check for the
        # visible reCAPTCHA checkbox (which appears on the review page) below.

        url = page.url
        page_name = url.split("/")[-1]
        logger.info("app_id=%d SmartApply step=%d page=%s", app_id, step, page_name)

        # Review module shows a loading spinner — wait for it to finish rendering
        if "review" in page_name:
            try:
                page.wait_for_function(
                    "() => !document.body.innerText.includes('Preparing review')",
                    timeout=20_000,
                )
                _human_pause(1.0, 2.0)
            except Exception:
                pass

        # Fill visible fields: fast universal path first, legacy as supplement
        detected = extract_form_fields(page)
        if detected:
            fast_fill_form(page, detected, answers)
        else:
            _fill_smartapply_page(page, answers, fill_delay)

        # Scroll to bottom so all buttons are in reach
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            _human_pause(0.5, 1.0)
        except Exception:
            pass

        _screenshot(page, folder_path, f"smartapply_step{step:02d}.png")

        # Try Submit first — only on the review page (all SmartApply pages
        # contain "submit" in their JS source, so we gate on the URL instead)
        if "review" in page_name or "submit" in page_name:
            # Click reCAPTCHA checkbox if present — same click a human makes.
            # If Google scores it as high-risk and shows an image challenge,
            # the checkbox stays unchecked and we raise CaptchaDetected below.
            if _smartapply_has_visible_captcha(page):
                _try_click_recaptcha_checkbox(page)
                _human_pause(2.5, 4.0)  # wait for Google's auto-verification
                if _smartapply_has_visible_captcha(page):
                    # Still showing challenge → genuine human solve required
                    raise CaptchaDetected("reCAPTCHA requires manual solve (image challenge appeared)")
            submitted = _smartapply_click_button(page, _SMARTAPPLY_SUBMIT_SELECTORS)
            if submitted:
                logger.info("app_id=%d SmartApply: clicked Submit on step=%d", app_id, step)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=int(_SUBMIT_WAIT * 1000))
                except Exception:
                    pass
                _human_pause(2.0, 3.0)
                return

        # Try Continue/Next to advance to next page
        clicked = _smartapply_click_button(page, _SMARTAPPLY_CONTINUE_SELECTORS)
        if clicked:
            logger.info("app_id=%d SmartApply: clicked Continue on step=%d", app_id, step)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            _human_pause(1.0, 2.0)
        else:
            raise RuntimeError(
                f"SmartApply: no Continue/Submit button on step={step} url={url}. "
                "Form may have a required field that needs manual attention."
            )

    raise RuntimeError(f"SmartApply: exceeded {max_steps} steps without reaching Submit")


def _smartapply_has_visible_captcha(page) -> bool:
    """Return True if a VISIBLE reCAPTCHA challenge iframe is on-screen.

    SmartApply embeds invisible reCAPTCHA on every page — we only want to
    detect the visible checkbox that appears on the final review page.
    """
    for sel in [
        "iframe[title*='recaptcha' i]",
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            pass
    return False


def _try_click_recaptcha_checkbox(page) -> None:
    """Click the reCAPTCHA 'I am not a robot' checkbox.

    This is the same click a human makes. Google's own scoring evaluates
    the click and may auto-verify or present a challenge. If a challenge
    appears the checkbox stays unchecked and the caller raises CaptchaDetected.
    """
    try:
        frame = page.frame_locator("iframe[title*='recaptcha' i]").first
        checkbox = frame.locator(".recaptcha-checkbox-border, #recaptcha-anchor")
        if checkbox.count() > 0:
            checkbox.first.click(timeout=3000)
            logger.info("reCAPTCHA checkbox clicked — waiting for auto-verify")
            return
    except Exception as e:
        logger.warning("reCAPTCHA checkbox click failed: %s", e)
    # Fallback: JS click on the iframe itself
    try:
        page.evaluate("""() => {
            const iframe = document.querySelector("iframe[title*='recaptcha' i]");
            if (iframe) {
                const cb = iframe.contentDocument &&
                    iframe.contentDocument.querySelector('.recaptcha-checkbox-border');
                if (cb) cb.click();
            }
        }""")
    except Exception:
        pass


def _smartapply_click_button(page, selectors: list) -> bool:
    """Try each selector; click via JS if Playwright visibility check fails. Return True on success."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            # Scroll into view, then click — skipping is_visible() which can
            # return False for SmartApply's mosaic web components even when rendered
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                loc.click(timeout=3000)
                return True
            except Exception:
                # Last resort: JS click
                try:
                    page.evaluate("(el) => el.click()", loc.element_handle())
                    return True
                except Exception:
                    pass
        except Exception:
            continue
    # Log all buttons on page to help debug missed selectors
    try:
        btns = page.evaluate("""
            () => [...document.querySelectorAll('button,[role=button],a[href]')].slice(0,15)
                    .map(b => b.tagName + '|' + (b.textContent||'').trim().slice(0,40) + '|' + (b.className||'').slice(0,30))
        """)
        logger.debug("_smartapply_click_button found no match; page buttons: %s", btns)
    except Exception:
        pass
    return False


def _fill_smartapply_page(page, answers: dict, fill_delay: float) -> None:
    """Fill visible fields on the current SmartApply page."""
    url = page.url

    # Location page — fill zip/city/address if empty
    if "profile-location" in url or "location" in url or "applybyapplyablejobid" in url:
        _fill_if_visible(page, answers.get("address_zip", ""), [
            "input[name='location-postal-code']",
            "input[id='location-fields-postal-code-input']",
            "input[data-testid='location-fields-postal-code-input']",
            "input[name='zip']", "input[id*='zip' i]",
            "input[placeholder*='zip' i]", "input[aria-label*='zip' i]",
            "input[autocomplete='postal-code']",
        ], fill_delay)
        _fill_if_visible(page, answers.get("address_city", ""), [
            "input[name='location-locality']",
            "input[id='location-fields-locality-input']",
            "input[data-testid='location-fields-locality-input']",
            "input[name='city']", "input[id*='city' i]",
            "input[placeholder*='city' i]", "input[aria-label*='city' i]",
        ], fill_delay)
        _fill_if_visible(page, answers.get("address_line1", ""), [
            "input[name='location-address']",
            "input[id='location-fields-address-input']",
            "input[data-testid='location-fields-address-input']",
            "input[name='address']", "input[id*='address' i]",
            "input[placeholder*='address' i]", "input[aria-label*='address' i]",
        ], fill_delay)

    # Resume page — upload PDF resume
    if "resume" in url:
        resume_path = answers.get("resume", "")
        if resume_path and os.path.isfile(resume_path):
            try:
                file_input = page.locator("input[type='file']").first
                if file_input.count() > 0:
                    file_input.set_input_files(resume_path)
                    _human_pause(fill_delay, fill_delay + 1.5)
                    logger.info("SmartApply: uploaded resume %s", resume_path)
            except Exception as exc:
                logger.warning("SmartApply resume upload failed: %s", exc)

    # Generic text inputs (phone, LinkedIn, etc.) — best-effort
    _fill_if_visible(page, answers.get("phone", ""), [
        "input[name*='phone' i]", "input[type='tel']",
        "input[aria-label*='phone' i]", "input[placeholder*='phone' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("linkedin_url", ""), [
        "input[name*='linkedin' i]", "input[aria-label*='linkedin' i]",
        "input[placeholder*='linkedin' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("website_url", ""), [
        "input[name*='website' i]", "input[name*='github' i]",
        "input[aria-label*='website' i]", "input[placeholder*='website' i]",
        "input[placeholder*='github' i]",
    ], fill_delay)


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


def _run_linkedin_easy_apply(page, app_id, answers, url, folder_path, fill_delay):
    """
    Attempt LinkedIn Easy Apply. If Easy Apply button is not found (external apply
    job), follow the external Apply link and fall through to the generic form flow.
    """
    from src.browser.linkedin_apply import linkedin_easy_apply
    from src.db.connection import get_connection

    conn = get_connection()
    candidate_row = None
    try:
        candidate_row = conn.execute(
            "SELECT name, email, phone, city, state, zip_code FROM candidates LIMIT 1"
        ).fetchone()
    except Exception:
        pass
    conn.close()

    candidate = {}
    if candidate_row:
        candidate = dict(candidate_row)
    else:
        candidate = {
            "name":  os.environ.get("CANDIDATE_NAME", "Pranav Tushar Pradhan"),
            "email": os.environ.get("YOUR_EMAIL_ADDRESS", ""),
            "phone": os.environ.get("CANDIDATE_PHONE", ""),
            "city":  os.environ.get("CANDIDATE_CITY",  "New York"),
            "state": os.environ.get("CANDIDATE_STATE", "NY"),
            "zip_code": os.environ.get("CANDIDATE_ZIP", "10001"),
        }

    resume_pdf = os.environ.get("RESUME_PDF_PATH", r"D:\Pranav\Resume\New folder\Pranav ML-AI Resume.pdf")

    # Navigate to job page first so we can inspect it
    page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    reading_pause()
    _screenshot(page, folder_path, "li_job_page.png")

    # Check if job is closed
    page_text = page.evaluate("() => document.body.innerText") or ""
    if "No longer accepting applications" in page_text:
        raise RuntimeError(f"LinkedIn job is closed (no longer accepting applications): {url}")

    # Try Easy Apply first
    try:
        receipt = linkedin_easy_apply(
            page, url, app_id,
            candidate=candidate,
            resume_pdf_path=resume_pdf,
            folder_path=folder_path,
            answers=answers,
            fill_delay=fill_delay,
        )
        ss = _screenshot(page, folder_path, "li_confirmation.png")
        return AutoSubmitResult(
            success=True, app_id=app_id,
            receipt=f"LINKEDIN:{receipt}",
            screenshot_path=ss,
        )
    except RuntimeError as e:
        if "Easy Apply button not found" not in str(e):
            raise
        logger.info("app_id=%d no Easy Apply — looking for external Apply link", app_id)

    # External apply fallback: find Apply link and follow it
    external_url = None
    for sel in [
        "a[aria-label*='Apply']",
        "a:has-text('Apply')",
        "button[aria-label*='Apply']",
        ".jobs-apply-button",
    ]:
        el = page.locator(sel).first
        if el.count() > 0:
            href = el.get_attribute("href") or ""
            if href and "linkedin.com" not in href:
                external_url = href
                break
            # Try clicking to get the redirect target
            try:
                with page.expect_navigation(timeout=8000):
                    el.click()
                if "linkedin.com" not in page.url:
                    external_url = page.url
                break
            except Exception:
                if "linkedin.com" not in page.url:
                    external_url = page.url
                    break

    if not external_url and "linkedin.com" not in page.url:
        external_url = page.url

    if external_url:
        logger.info("app_id=%d LinkedIn external apply: navigating to %s", app_id, external_url)
        if page.url != external_url:
            page.goto(external_url, wait_until="domcontentloaded", timeout=30_000)
            _human_pause(2.0, 3.0)
        # Hand off to generic form flow
        return _run_submit_flow(page, app_id, answers, external_url, folder_path, fill_delay)

    raise RuntimeError(f"LinkedIn: no Easy Apply and no external Apply link found on {url}")


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
