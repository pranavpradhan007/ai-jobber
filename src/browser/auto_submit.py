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
    # eightfold.ai / Phenom People career portals (NYL, many enterprise firms)
    "button[data-ph-at-id='apply-btn']",
    "a[data-ph-at-id='apply-btn']",
    ".apply-btn",
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
    # Exact text matches
    "button:text-is('Submit Application')",
    "button:text-is('Submit')",
    "button:text-is('Send Application')",
    # Partial text matches (Ashby, Comeet, various ATS)
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit your application')",
    "button:has-text('Send application')",
    "button:has-text('Complete Application')",
    # Workday
    "[data-automation-id='bottom-navigation-next-button']",
    # WhiteCarrot profile-builder (various button texts)
    "button:has-text('Submit Application')",
    "button:has-text('Submit Profile')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply now')",
    "button:has-text('Apply for this')",
    "button:has-text('Apply to')",
    "button:has-text('Finish')",
    "button:has-text('Complete')",
    "button:has-text('Save & Apply')",
    # Other portals
    "button:text-is('Apply')",
    "#submit_app_button",
    ".btn-submit",
    "[data-testid='submit-application-button']",   # Ashby
    "[data-testid='apply-button']",
    "button.ashby-application-form-submit-button",
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

# Workday applyManually multi-step navigation
_WORKDAY_NEXT_SELECTORS = [
    "[data-automation-id='bottom-navigation-next-button']",
    "button:has-text('Next')",
    "button[aria-label='Next']",
    "button[aria-label*='next' i]",
    "button:has-text('Save and Continue')",
    "button:has-text('Continue')",
    "button[type='button']:has-text('Next')",
]
_WORKDAY_SUBMIT_SELECTORS = [
    "[data-automation-id='bottom-navigation-submit-button']",
    "button:has-text('Submit')",
    "button[aria-label*='submit' i]",
    "[data-automation-id='bottom-navigation-next-button']",  # Workday reuses this ID on last step
    "button:has-text('Apply')",
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

    # Fallback resume PDF: DB path → RESUME_PDF_PATH env var → hardcoded default
    resume_pdf_fallback = os.environ.get(
        "RESUME_PDF_PATH",
        r"D:\Pranav\Resume\New folder\Pranav ML-AI Resume.pdf",
    )
    effective_resume = resume_path or resume_pdf_fallback

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
        "resume":             effective_resume,
        "resume_path":        effective_resume,
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

    # Fill address/phone fields from profile when env vars were not set
    profile_address_map = {
        "address_line1":  profile.get("address", ""),
        "address_city":   profile.get("city", ""),
        "address_state":  profile.get("state", ""),
        "address_zip":    profile.get("zip_code", ""),
        "address_country": profile.get("country", "United States"),
        "phone":          profile.get("phone", ""),
        "phone_country_code": "+1",
    }
    for k, v in profile_address_map.items():
        if v and not answers.get(k):
            answers[k] = v

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
        # Close stale pages left from previous sessions — they hold memory and
        # can cause ERR_INSUFFICIENT_RESOURCES on the next navigation.
        for stale in list(context.pages):
            try:
                stale.close()
            except Exception:
                pass
        # Retry new_page() — transient "Failed to open a new tab" errors occur
        # when the previous browser session hasn't fully released resources yet.
        page = None
        for _np_attempt in range(3):
            try:
                page = context.new_page()
                break
            except Exception as _np_exc:
                if _np_attempt == 2:
                    raise
                logger.warning("context.new_page failed (attempt %d): %s — retrying in 3s", _np_attempt + 1, _np_exc)
                time.sleep(3)
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
    # Suppress crash-recovery dialogs that pause automation when the previous
    # Chrome session was killed (e.g. by _release_profile_lock between apps).
    "--disable-session-crashed-bubble",
    "--disable-restore-session-state",
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
    # Extra buffer after the kill loop inside _release_profile_lock before launching.
    time.sleep(2)
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
    """Kill ALL Chrome/Chromium processes and verify they are gone before returning.

    Chrome helper processes (GPU, renderer, crashpad) don't carry --user-data-dir
    in their command line, so a profile-specific kill misses them. Any survivor
    causes the next launch to print 'Opening in existing browser session' and exit,
    leaving Playwright with a dead context. We kill all chrome.exe up to 5 times
    and verify via psutil (or tasklist fallback) that no chrome processes remain.
    """
    import subprocess

    # Graceful close first so Chrome can flush its profile to disk cleanly
    subprocess.run(["taskkill", "/IM", "chrome.exe"],
                   capture_output=True, timeout=8)
    time.sleep(1)

    # Force-kill loop — retry up to 5 times until no chrome.exe remains
    for _attempt in range(5):
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       capture_output=True, timeout=10)
        time.sleep(2)
        # Check if any chrome.exe processes survive
        try:
            check = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=8,
            )
            still_running = "chrome.exe" in check.stdout.lower()
        except Exception:
            still_running = False  # assume gone if check fails
        if not still_running:
            logger.info("_release_profile_lock: all chrome processes gone (attempt %d)", _attempt + 1)
            break
        logger.warning("_release_profile_lock: chrome still running after attempt %d — retrying", _attempt + 1)

    # Remove Chrome's SingletonLock file unconditionally
    try:
        lock = os.path.join(profile_dir, "SingletonLock")
        if os.path.exists(lock):
            os.remove(lock)
            logger.info("Removed SingletonLock from profile dir")
    except Exception as exc:
        logger.debug("_release_profile_lock lock file: %s", exc)

    # Also clear any crash-recovery state that would trigger a restore dialog
    try:
        prefs = os.path.join(profile_dir, "Default", "Preferences")
        if os.path.isfile(prefs):
            import json as _json
            with open(prefs, encoding="utf-8") as fh:
                data = _json.load(fh)
            if data.get("profile", {}).get("exit_type") != "Normal":
                data.setdefault("profile", {})["exit_type"] = "Normal"
                data.setdefault("profile", {})["exited_cleanly"] = True
                with open(prefs, "w", encoding="utf-8") as fh:
                    _json.dump(data, fh)
                logger.info("Reset Chrome exit_type to Normal in Preferences")
    except Exception as exc:
        logger.debug("_release_profile_lock prefs reset: %s", exc)


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

    # ── Step 2b: Dismiss cookie / consent modals ──────────────────────────────
    # Cookie banners intercept clicks on Apply buttons (e.g. eightfold.ai portals).
    _dismiss_cookie_modals(page)

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

    # Workday applyManually is a 6-step wizard — dispatch to the multi-step handler
    # which fills each page and clicks Next until it reaches Submit.
    if portal == "workday" and "applymanually" in page.url.lower():
        logger.info("app_id=%d Workday multi-step form — dispatching to page loop", app_id)
        _handle_workday_pages(page, app_id, answers, folder_path, fill_delay)
        ss = _screenshot(page, folder_path, "submit_confirmation.png")
        receipt = f"PORTAL:workday:{page.url}"
        logger.info("app_id=%d submitted portal=workday url=%s", app_id, page.url)
        return AutoSubmitResult(
            success=True, app_id=app_id,
            receipt=receipt, screenshot_path=ss,
        )

    # Fast universal path: JS-detected fields → human-like fill
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form, verify_form_complete

    # Dismiss cookie modals that may have appeared after navigating to the ATS
    _dismiss_cookie_modals(page)

    # Wait extra for JS-rendered forms (Comeet, NYL, WhiteCarrot, other SPAs).
    # 15 s covers slow-loading profile-builder SPAs that show a spinner initially.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # WhiteCarrot: handle email-entry page, then multi-step profile-builder
    if "whitecarrot.io" in page.url:
        _handle_whitecarrot_email_entry(page, answers)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        _human_pause(2.0, 3.0)
        _screenshot(page, folder_path, "whitecarrot_form.png")
        try:
            body_snippet = (page.evaluate("() => document.body.innerText") or "")[:300]
            logger.info("app_id=%d WhiteCarrot post-gate body: %s", app_id, body_snippet)
        except Exception:
            pass
        # WhiteCarrot is multi-step — hand off to dedicated loop handler
        _handle_whitecarrot_multistep(page, answers, folder_path)
        return  # submit handled inside multistep loop

    # Detect fields in main page first, then fall back to iframes (Comeet et al.)
    fill_page = page
    detected = extract_form_fields(page)

    if not detected:
        # Some portals (Comeet) embed the application form inside an <iframe>.
        # page.evaluate() doesn't cross iframe boundaries, so scan each frame.
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            frame_url = frame.url or ""
            if frame_url in ("", "about:blank"):
                continue
            try:
                frame_fields = extract_form_fields(frame)
                if frame_fields:
                    logger.info(
                        "app_id=%d iframe form: %d fields in %s",
                        app_id, len(frame_fields), frame_url,
                    )
                    detected = frame_fields
                    fill_page = frame
                    break
            except Exception as exc:
                logger.debug("app_id=%d iframe scan %s: %s", app_id, frame_url, exc)

    if detected:
        logger.info("app_id=%d fast-fill: %d fields detected", app_id, len(detected))
        ff = fast_fill_form(fill_page, detected, answers)
        logger.info(
            "app_id=%d fast-fill: filled=%d skipped=%d unmatched=%d unmatched_labels=%s",
            app_id, ff.fields_filled, ff.fields_skipped, ff.fields_unmatched,
            ff.unmatched_labels[:5],
        )
        # Fall back to legacy selector-based fill for any remaining unmatched fields
        if ff.fields_unmatched > 3:
            logger.info("app_id=%d unmatched fields > 3, running legacy fill as supplement", app_id)
            instructions = build_fill_instructions(answers, portal)
            _fill_all_fields(fill_page, answers, instructions, FAST_FILL_DELAY)
    else:
        # form_detector returned nothing — use legacy selector-based fill
        logger.info("app_id=%d form_detector found 0 fields, using legacy fill", app_id)
        instructions = build_fill_instructions(answers, portal)
        _fill_all_fields(fill_page, answers, instructions, fill_delay)

    # Greenhouse: location field is a react-select combobox — needs special handling
    if "greenhouse.io" in page.url or "grnh.se" in page.url:
        _fill_greenhouse_location(page, answers)

    # Verify form is complete before attempting submit
    complete, issues = verify_form_complete(fill_page)
    if not complete:
        logger.warning("app_id=%d form completion check: %s", app_id, issues)
        _screenshot(page, folder_path, "submit_incomplete.png")

    # ── Step 7: Click Submit ──────────────────────────────────────────────────
    # Guard: if still on WhiteCarrot email-entry page, the email wasn't filled
    # and we must not click the "Get started" button as a false Submit.
    if "whitecarrot.io" in page.url:
        try:
            body_check = page.evaluate("() => document.body.innerText") or ""
            if "enter your email" in body_check.lower():
                raise RuntimeError(
                    f"app_id={app_id} WhiteCarrot still on email-entry page — "
                    "email fill failed, refusing to false-submit"
                )
        except RuntimeError:
            raise
        except Exception:
            pass
        # WhiteCarrot: scroll to bottom to reveal any Submit/Apply button that
        # only appears after filling mandatory fields.
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            _human_pause(1.0, 2.0)
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        _screenshot(page, folder_path, "whitecarrot_presubmit.png")
    # Try the fill_page frame first (iframe forms), then fall back to main page
    try:
        _click_submit(fill_page, app_id)
    except RuntimeError:
        if fill_page is not page:
            logger.info("app_id=%d submit not found in iframe, trying main page", app_id)
            _click_submit(page, app_id)
        else:
            raise

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
    disabled_candidate = None
    for sel in _SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible():
                continue
            if loc.is_enabled():
                _human_click(page, loc)
                logger.info("app_id=%d clicked submit selector=%r", app_id, sel)
                return
            # Button visible but disabled — track as fallback candidate
            if disabled_candidate is None:
                disabled_candidate = (sel, loc)
        except Exception:
            continue
    # If all selectors found only disabled buttons, try clicking the first one anyway
    # (some portals enable the button via JS on click, or disable check is a false negative)
    if disabled_candidate:
        sel, loc = disabled_candidate
        try:
            _human_click(page, loc)
            logger.info("app_id=%d clicked DISABLED submit selector=%r (trying anyway)", app_id, sel)
            return
        except Exception as e:
            logger.warning("app_id=%d clicking disabled button failed: %s", app_id, e)
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
    # First / Last name — broad selectors cover Comeet and other non-standard portals
    _fill_if_visible(page, answers.get("first_name", ""), [
        "input[id='first_name']", "input[name='first_name']",
        "input[id*='first' i]", "input[name*='first' i]",
        "input[placeholder*='first name' i]", "input[aria-label*='first name' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("last_name", ""), [
        "input[id='last_name']", "input[name='last_name']",
        "input[id*='last' i]", "input[name*='last' i]",
        "input[placeholder*='last name' i]", "input[aria-label*='last name' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("phone", ""), [
        "input[type='tel']", "input[id='phone']", "input[name='phone']",
        "input[id*='phone' i]", "input[name*='phone' i]",
        "input[placeholder*='phone' i]", "input[aria-label*='phone' i]",
    ], fill_delay)

    # Email — use both selector-based and attribute-based approaches
    # (fast_fill may have set this via locator.fill() but React SPAs need keyboard events)
    _fill_if_visible(page, answers.get("email", ""), [
        "input[type='email']",
        "input[id='email']", "input[name='email']",
        "input[id*='email' i]", "input[name*='email' i]",
        "input[aria-label*='email' i]", "input[placeholder*='email' i]",
    ], fill_delay)

    # Location / City — Greenhouse uses "Location (City)" label with id="location"
    city = answers.get("address_city", "") or answers.get("location", "")
    _fill_if_visible(page, city, [
        "#location",
        "input[id='location']", "input[name='location']",
        "input[placeholder*='city' i]", "input[aria-label*='city' i]",
        "input[aria-label*='location' i]", "input[placeholder*='location' i]",
        "input[name*='city' i]",
    ], fill_delay)

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
                if el.count() == 0:
                    continue
                # Detect mis-typed file inputs (e.g. Greenhouse #cover_letter which
                # is field_type="textarea" in our map but is actually a file input).
                # Trying to click a hidden file input causes a 30 s timeout.
                try:
                    el_input_type = el.get_attribute("type") or ""
                except Exception:
                    el_input_type = ""
                if el_input_type == "file":
                    resume = answers.get("resume", "") or answers.get("resume_path", "")
                    if resume and os.path.isfile(resume):
                        el.set_input_files(resume)
                        _human_pause(fill_delay, fill_delay + 1.0)
                    continue
                # Skip fields that fast_fill already populated — avoids doubling names.
                try:
                    current_val = el.input_value()
                except Exception:
                    current_val = ""
                if current_val:
                    continue
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

    # Best-effort resume upload — runs even if no "file" instruction was generated
    # (e.g. Greenhouse where #cover_letter is the resume upload but build_fill_instructions
    # skips it because key=="resume"). Use set_input_files to bypass hidden-input overlays.
    resume_path = answers.get("resume", "") or answers.get("resume_path", "")
    if resume_path and os.path.isfile(resume_path):
        for file_sel in ["input[type='file']", "#cover_letter", "input[accept*='.pdf']"]:
            try:
                fi = page.locator(file_sel).first
                if fi.count() > 0:
                    fi.set_input_files(resume_path)
                    _human_pause(fill_delay, fill_delay + 1.5)
                    logger.info("Uploaded resume via %r", file_sel)
                    break
            except Exception as exc:
                logger.debug("resume upload %r: %s", file_sel, exc)


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


def _workday_combobox_select(page, automation_id: str, value: str) -> bool:
    """Select a value in a Workday custom combobox (not a standard <select>).

    Workday uses custom React components where select_option() doesn't work.
    Strategy: click the button to open the dropdown, type to filter, click the
    matching list item.  Returns True if a matching item was clicked.
    """
    try:
        # Locate the combobox button by data-automation-id
        btn = page.locator(f"[data-automation-id='{automation_id}']").first
        if btn.count() == 0:
            return False
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=3000)
        _human_pause(0.4, 0.8)
        # Type the value to filter options
        page.keyboard.type(value, delay=60)
        _human_pause(0.5, 1.0)
        # Click the first visible list option that contains the value text
        option = page.locator(
            f"[data-automation-id='promptOption']:has-text('{value}'), "
            f"li:has-text('{value}'), "
            f"[role='option']:has-text('{value}')"
        ).first
        if option.count() > 0 and option.is_visible():
            option.click(timeout=3000)
            _human_pause(0.3, 0.6)
            return True
    except Exception as exc:
        logger.debug("_workday_combobox_select %r=%r: %s", automation_id, value, exc)
    return False


def _workday_dropdown_select(page, automation_id: str, value: str) -> bool:
    """Select a value in a Workday standard dropdown (rendered as a button + list)."""
    try:
        btn = page.locator(f"[data-automation-id='{automation_id}']").first
        if btn.count() == 0:
            return False
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=3000)
        _human_pause(0.4, 0.8)
        option = page.locator(
            f"[data-automation-id='promptOption']:has-text('{value}'), "
            f"li:has-text('{value}'), "
            f"[role='option']:has-text('{value}')"
        ).first
        if option.count() > 0:
            option.click(timeout=3000)
            _human_pause(0.3, 0.6)
            return True
    except Exception as exc:
        logger.debug("_workday_dropdown_select %r=%r: %s", automation_id, value, exc)
    return False


def _fill_workday_my_info(page, answers: dict) -> None:
    """Fill Workday 'My Information' page fields that fast_fill misses.

    Handles: Phone Device Type (dropdown), Country Phone Code (combobox —
    clears wrong selection like Albania and sets United States), Phone Number
    (10 digits only — country code is separate), and State/Region (combobox).
    Uses type() for React inputs and tries select_option() before click+type.
    """
    import re as _re

    # Diagnostic: log all data-automation-id values so we can see Danaher's IDs
    try:
        all_ids = page.evaluate("""
            () => Array.from(document.querySelectorAll('[data-automation-id]'))
                  .map(el => el.getAttribute('data-automation-id'))
                  .filter(Boolean)
        """) or []
        logger.info("Workday automation IDs on page: %s", all_ids[:60])
    except Exception as _de:
        logger.debug("Workday automation ID scan: %s", _de)

    # ── Phone Device Type ─────────────────────────────────────────────────────
    # Danaher Workday uses formField-phoneType (outer container).
    # The interactive button is inside it — never clickable via the outer div.
    _pdt_set = False
    try:
        # Container check — any variant
        pdt_container = page.locator(
            "[data-automation-id='phoneDeviceType'], "
            "[data-automation-id='phoneType'], "
            "[data-automation-id='formField-phoneType'], "
            "[data-automation-id='phoneDeviceTypeSection']"
        ).first
        if pdt_container.count() > 0:
            current_text = pdt_container.inner_text() or ""
            if "Mobile" in current_text:
                _pdt_set = True  # already Mobile
            else:
                # Try native select
                try:
                    pdt_container.select_option("Mobile", timeout=1500)
                    logger.info("Workday: set Phone Device Type = Mobile (native select)")
                    _pdt_set = True
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("Workday phoneDeviceType container: %s", exc)

    if not _pdt_set:
        # Click the INNER button inside formField-phoneType (Danaher pattern)
        for pdt_btn_sel in [
            "[data-automation-id='formField-phoneType'] button",
            "[data-automation-id='phoneType'] button",
            "[data-automation-id='formField-phoneType'] [role='button']",
            "[data-automation-id='formField-phoneType'] [role='combobox']",
            # Fall back to any button in a group containing "Phone Device Type" text
        ]:
            try:
                btn = page.locator(pdt_btn_sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.scroll_into_view_if_needed(timeout=3000)
                    btn.click(timeout=3000)
                    _human_pause(0.5, 1.0)
                    # Wait for dropdown options
                    opt_sel = (
                        "[data-automation-id='promptOption']:has-text('Mobile'), "
                        "li:has-text('Mobile'), [role='option']:has-text('Mobile')"
                    )
                    try:
                        page.wait_for_selector(opt_sel, state="visible", timeout=3000)
                        opt = page.locator(opt_sel).first
                        opt.click(timeout=3000)
                        logger.info("Workday: set Phone Device Type = Mobile (btn=%r)", pdt_btn_sel)
                        _pdt_set = True
                    except Exception:
                        pass
                    break
            except Exception:
                continue

    if not _pdt_set:
        logger.warning("Workday phoneDeviceType: could not set to Mobile")

    # ── Country Phone Code ────────────────────────────────────────────────────
    # Danaher Workday uses formField-countryPhoneCode (outer div)
    try:
        code_container = page.locator(
            "[data-automation-id='countryPhoneCode'], "
            "[data-automation-id='formField-countryPhoneCode'], "
            "[data-automation-id='phoneCountryCode']"
        ).first
        if code_container.count() > 0:
            current = code_container.inner_text() or ""
            if "United States" not in current:
                # Remove existing selections (× chips)
                for x_btn in page.locator(
                    "[data-automation-id='countryPhoneCode'] [data-automation-id='DELETE_charm'], "
                    "[data-automation-id='countryPhoneCode'] button[aria-label*='emove']"
                ).all():
                    try:
                        x_btn.click(timeout=2000)
                        _human_pause(0.2, 0.3)
                    except Exception:
                        pass
                # Click to open
                code_container.scroll_into_view_if_needed(timeout=3000)
                code_container.click(timeout=3000)
                _human_pause(0.4, 0.7)
                # Type into the inner search input that appears after click
                inner = page.locator(
                    "[data-automation-id='countryPhoneCode'] input, "
                    "[data-automation-id='searchText']"
                ).first
                if inner.count() > 0 and inner.is_visible():
                    inner.fill("")
                    inner.type("United States", delay=55)
                else:
                    page.keyboard.type("United States", delay=55)
                _human_pause(0.7, 1.2)
                try:
                    page.wait_for_selector(
                        "[data-automation-id='promptOption']:has-text('United States'), "
                        "[role='option']:has-text('United States')",
                        state="visible", timeout=4000,
                    )
                    opt = page.locator(
                        "[data-automation-id='promptOption']:has-text('United States (+1)'), "
                        "[data-automation-id='promptOption']:has-text('United States'), "
                        "[role='option']:has-text('United States')"
                    ).first
                    opt.click(timeout=3000)
                    logger.info("Workday: set Country Phone Code = United States")
                except Exception as exc2:
                    logger.warning("Workday countryPhoneCode option click: %s", exc2)
    except Exception as exc:
        logger.warning("Workday countryPhoneCode: %s", exc)

    # ── Phone Number — 10 digits, no country code prefix ─────────────────────
    raw_phone = answers.get("phone", "")
    if raw_phone:
        digits = _re.sub(r"[^\d]", "", raw_phone)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            _phone_filled = False
            for phone_sel in [
                "[data-automation-id='phone'] input",
                "[data-automation-id='phoneNumber'] input",
                "input[data-automation-id='phone']",
                "input[data-automation-id='phoneNumber']",
                "input[type='tel']",
                "input[name*='phone' i]",
                "input[id*='phone' i]",
            ]:
                try:
                    phone_input = page.locator(phone_sel).first
                    if phone_input.count() > 0 and phone_input.is_visible():
                        phone_input.scroll_into_view_if_needed(timeout=3000)
                        phone_input.click(click_count=3, timeout=3000)
                        _human_pause(0.1, 0.2)
                        phone_input.type(digits, delay=40)
                        logger.info("Workday: set Phone = %s (sel=%r)", digits, phone_sel)
                        _phone_filled = True
                        break
                except Exception:
                    continue
            if not _phone_filled:
                logger.warning("Workday phone: no matching input found for selectors tried")

    # ── State / Region ────────────────────────────────────────────────────────
    # Danaher Workday uses formField-countryRegion (outer container).
    # The interactive inner button must be clicked to open the combobox.
    _STATE_ABBR = {
        "NY": "New York", "CA": "California", "TX": "Texas", "FL": "Florida",
        "WA": "Washington", "IL": "Illinois", "MA": "Massachusetts", "NJ": "New Jersey",
        "PA": "Pennsylvania", "OH": "Ohio", "GA": "Georgia", "NC": "North Carolina",
        "VA": "Virginia", "CO": "Colorado", "AZ": "Arizona", "MN": "Minnesota",
    }
    state_raw = answers.get("address_state", "") or "New York"
    state_full = _STATE_ABBR.get(state_raw, state_raw)
    _state_set = False
    try:
        state_container = page.locator(
            "[data-automation-id='formField-countryRegion'], "
            "[data-automation-id='addressSection-stateProvince'], "
            "[data-automation-id='stateProvince'], "
            "[data-automation-id='state']"
        ).first
        if state_container.count() > 0:
            current = state_container.inner_text() or ""
            # Check if already has a valid state value (not empty / not default)
            if state_full in current or state_raw in current:
                _state_set = True
            else:
                # Try native select first
                for sv in [state_raw, state_full]:
                    try:
                        state_container.select_option(sv, timeout=1500)
                        logger.info("Workday: set State = %s (native select)", sv)
                        _state_set = True
                        break
                    except Exception:
                        pass
    except Exception as exc:
        logger.debug("Workday state container: %s", exc)

    if not _state_set:
        # Click the inner button within formField-countryRegion (Danaher pattern)
        for st_btn_sel in [
            "[data-automation-id='formField-countryRegion'] button",
            "[data-automation-id='stateProvince'] button",
            "[data-automation-id='formField-countryRegion'] [role='combobox']",
            "[data-automation-id='formField-countryRegion'] [role='button']",
        ]:
            try:
                btn = page.locator(st_btn_sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.scroll_into_view_if_needed(timeout=3000)
                    btn.click(timeout=3000)
                    _human_pause(0.5, 0.9)
                    # Type into search box that appears after click
                    for search_sel in [
                        "[data-automation-id='monikerSearchBox'] input",
                        "[data-automation-id='searchBox'] input",
                        "[data-automation-id='searchText']",
                        "[data-automation-id='formField-countryRegion'] input",
                    ]:
                        search_inp = page.locator(search_sel).first
                        if search_inp.count() > 0 and search_inp.is_visible():
                            search_inp.fill("")
                            search_inp.type(state_full, delay=55)
                            _human_pause(0.7, 1.2)
                            break
                    else:
                        page.keyboard.type(state_full, delay=55)
                        _human_pause(0.7, 1.2)
                    # Select matching option
                    for sv in [state_full, state_raw]:
                        opt_sel = (
                            f"[data-automation-id='promptOption']:has-text('{sv}'), "
                            f"[role='option']:has-text('{sv}')"
                        )
                        try:
                            page.wait_for_selector(opt_sel, state="visible", timeout=4000)
                            page.locator(opt_sel).first.click(timeout=3000)
                            logger.info("Workday: set State = %s (combobox btn=%r)", sv, st_btn_sel)
                            _state_set = True
                            break
                        except Exception:
                            pass
                    break
            except Exception:
                continue

    if not _state_set:
        logger.warning("Workday state: could not set to %s / %s", state_raw, state_full)


def _screenshot(page, folder: str, name: str) -> Optional[str]:
    path = os.path.join(folder, name)
    try:
        page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return None


def _handle_whitecarrot_email_entry(page, answers: dict) -> None:
    """Handle WhiteCarrot email-entry gate page before the actual application form.

    WhiteCarrot shows "Please enter your email to start or resume your application"
    before showing the real form. We fill the email, click Get started, and wait
    for the form to load so the rest of the fill pipeline sees the actual fields.
    """
    try:
        body = page.evaluate("() => document.body.innerText") or ""
        if "enter your email" not in body.lower():
            return  # Already past the email-entry step
        email = answers.get("email", "")
        if not email:
            logger.warning("WhiteCarrot email-entry: no email in answers")
            return
        # Fill the email input
        email_input = page.locator(
            "input[type='email'], input[placeholder*='email' i]"
        ).first
        if email_input.count() > 0 and email_input.is_visible():
            email_input.click(click_count=3, timeout=3000)
            email_input.type(email, delay=50)
            logger.info("WhiteCarrot: filled email on entry page")
            _human_pause(0.4, 0.7)
        # Click "Get started" (not a submit button — it loads the actual form)
        for btn_text in ("Get started", "Continue", "Start"):
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if btn.count() > 0 and btn.is_visible():
                _human_click(page, btn)
                logger.info("WhiteCarrot: clicked '%s' on entry page", btn_text)
                break
        # Wait for the actual form to load
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        _human_pause(1.5, 2.5)
        logger.info("WhiteCarrot: past email-entry, now on: %s", page.url)
    except Exception as exc:
        logger.warning("WhiteCarrot email-entry handling: %s", exc)


def _fill_whitecarrot_form(page, answers: dict, folder_path: str) -> None:
    """Direct-fill WhiteCarrot profile-builder form using concrete selectors.

    form_detector finds 0 fields on WhiteCarrot because it uses non-standard
    React components. This function fills: First name, Last name, Phone,
    LinkedIn, and uploads the resume via the file input.
    """
    import re as _re
    try:
        _human_pause(0.5, 1.0)

        # ── First Name ────────────────────────────────────────────────────────
        first_name = answers.get("first_name", "") or "Pranav"
        for sel in [
            "input[placeholder*='First name' i]",
            "input[placeholder*='First' i]",
            "input[name*='first' i]",
            "input[id*='first' i]",
        ]:
            inp = page.locator(sel).first
            if inp.count() > 0 and inp.is_visible():
                inp.click(click_count=3, timeout=3000)
                inp.type(first_name, delay=40)
                logger.info("WhiteCarrot: filled First Name = %s", first_name)
                _human_pause(0.2, 0.4)
                break

        # ── Last Name ─────────────────────────────────────────────────────────
        last_name = answers.get("last_name", "") or "Pradhan"
        for sel in [
            "input[placeholder*='Last name' i]",
            "input[placeholder*='Last' i]",
            "input[name*='last' i]",
            "input[id*='last' i]",
        ]:
            inp = page.locator(sel).first
            if inp.count() > 0 and inp.is_visible():
                inp.click(click_count=3, timeout=3000)
                inp.type(last_name, delay=40)
                logger.info("WhiteCarrot: filled Last Name = %s", last_name)
                _human_pause(0.2, 0.4)
                break

        # ── Phone Country Code → switch to US (+1) ───────────────────────────
        # Default country is UAE (+971). The library uses a hidden <select> for
        # the country code (react-phone-number-input pattern).
        try:
            for cc_sel in [
                "select.PhoneInputCountrySelect",
                "select[aria-label*='country' i]",
                "select[class*='country' i]",
                "select[class*='Country' i]",
                "select[class*='phone' i]",
            ]:
                cc = page.locator(cc_sel).first
                if cc.count() > 0:
                    cc.select_option(value="US", timeout=2000)
                    logger.info("WhiteCarrot: set phone country code to US")
                    _human_pause(0.3, 0.6)
                    break
        except Exception as _cce:
            logger.debug("WhiteCarrot phone country: %s", _cce)

        # ── Phone Number ──────────────────────────────────────────────────────
        raw_phone = answers.get("phone", "")
        if raw_phone:
            digits = _re.sub(r"[^\d]", "", raw_phone)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            for sel in [
                "input[type='tel']",
                "input[placeholder*='phone' i]",
                "input[name*='phone' i]",
                "input[id*='phone' i]",
            ]:
                inp = page.locator(sel).first
                if inp.count() > 0 and inp.is_visible():
                    inp.click(click_count=3, timeout=3000)
                    inp.type(digits, delay=40)
                    logger.info("WhiteCarrot: filled Phone = %s", digits)
                    _human_pause(0.2, 0.4)
                    break

        # ── LinkedIn URL ──────────────────────────────────────────────────────
        linkedin_url = answers.get("linkedin_url", "")
        if linkedin_url:
            for sel in [
                "input[placeholder*='linkedin' i]",
                "input[name*='linkedin' i]",
                "input[id*='linkedin' i]",
            ]:
                inp = page.locator(sel).first
                if inp.count() > 0 and inp.is_visible():
                    try:
                        cur = inp.input_value()
                        if cur and "linkedin.com/in/" in cur and len(cur) > 20:
                            break  # already filled with a real profile URL
                    except Exception:
                        pass
                    inp.click(click_count=3, timeout=3000)
                    inp.type(linkedin_url, delay=40)
                    logger.info("WhiteCarrot: filled LinkedIn = %s", linkedin_url)
                    _human_pause(0.2, 0.4)
                    break

        # ── Resume Upload ─────────────────────────────────────────────────────
        resume_path = answers.get("resume_pdf_path", "") or os.environ.get("RESUME_PDF_PATH", "")
        if resume_path and os.path.isfile(resume_path):
            file_inp = page.locator("input[type='file']").first
            if file_inp.count() > 0:
                file_inp.set_input_files(resume_path)
                logger.info("WhiteCarrot: uploaded resume %s", resume_path)
                _human_pause(2.0, 3.0)

        _screenshot(page, folder_path, "whitecarrot_filled.png")
        logger.info("WhiteCarrot: direct-fill complete")
    except Exception as exc:
        logger.warning("_fill_whitecarrot_form: %s", exc)


def _fill_greenhouse_location(page, answers: dict) -> None:
    """Fill the Greenhouse 'Location (City)' react-select combobox.

    Greenhouse uses a custom typeahead (#location input) that shows a dropdown
    of matching city options after typing. fill() and type() alone don't select
    an option — we must click the first dropdown result.
    """
    city = answers.get("address_city", "") or "Brooklyn"
    try:
        loc_input = page.locator(
            "#location, input[id='location'], input[name='location']"
        ).first
        if loc_input.count() == 0 or not loc_input.is_visible():
            return
        # Check current value — skip if already filled
        try:
            cur = loc_input.input_value()
            if cur and len(cur) > 2:
                return
        except Exception:
            pass
        loc_input.scroll_into_view_if_needed(timeout=3000)
        loc_input.click(click_count=3, timeout=3000)
        _human_pause(0.1, 0.2)
        loc_input.type(city, delay=50)
        _human_pause(1.2, 2.0)  # wait for autocomplete dropdown
        # Click the first autocomplete suggestion
        for opt_sel in [
            "[data-qa='select-item']",
            ".select__option",
            "li[role='option']",
            "[role='option']",
            ".dropdown-item",
        ]:
            opt = page.locator(opt_sel).first
            if opt.count() > 0 and opt.is_visible():
                _human_click(page, opt)
                logger.info("Greenhouse: set Location = %s (clicked '%s')", city, opt_sel)
                return
        # No dropdown appeared — the typed text might be accepted directly
        logger.info("Greenhouse: no location dropdown, left text as typed: %s", city)
    except Exception as exc:
        logger.warning("Greenhouse location fill: %s", exc)


def _fill_workday_screener_questions(page, answers: dict) -> int:
    """Fill Workday Application Questions page with custom combobox dropdowns.

    Workday screener questions use Workday's custom React combobox component
    (same as My Information page), NOT native <select> elements.
    Strategy: find buttons with 'Select One' text → match label → click to open
    dropdown → click the matching promptOption.

    Returns number of questions filled.
    """
    _RULES = [
        (["18 years", "18 year", "age or older", "legal age"], "Yes"),
        (["legally authorized", "authorized to work in the united states", "work in the us", "authorized to work"], "Yes"),
        (["sponsorship", "employment visa", "visa sponsorship", "require sponsorship"], "Yes"),
        (["previously employed", "employed by danaher", "affiliate companies", "previously worked for"], "No"),
        (["background check", "background investigation"], "Yes"),
        (["drug test", "drug screen"], "Yes"),
        (["felony", "convicted"], "No"),
        (["non-compete"], "No"),
    ]

    filled = 0
    try:
        # Workday renders unfilled custom dropdowns as buttons with "Select One" text.
        # NOTE: also check for "Select One" inside a div/span — Workday sometimes wraps
        # the button label in a span, so :has-text matches the parent container.
        select_btns = page.locator(
            "button:has-text('Select One'), "
            "[role='combobox']:has-text('Select One'), "
            "[role='button']:has-text('Select One')"
        ).all()
        logger.debug("Workday screener: found %d 'Select One' buttons", len(select_btns))

        for btn in select_btns:
            try:
                if not btn.is_visible(timeout=1000):
                    continue

                # Get question text from surrounding container
                label_text = (btn.evaluate("""el => {
                    let p = el.closest('div[class*="formField"],div[data-automation-id],fieldset,li,section');
                    if (!p) p = el.parentElement?.parentElement;
                    if (!p) return '';
                    let lbl = p.querySelector('label,legend,p,h3,h4,[class*="label"]');
                    if (lbl) return lbl.innerText?.trim() || '';
                    return p.innerText?.split('\\n')[0]?.trim() || '';
                }""") or "").lower().strip()

                if not label_text:
                    continue

                # Match label to rule
                chosen = None
                for keywords, answer in _RULES:
                    if any(kw in label_text for kw in keywords):
                        chosen = answer
                        break
                if chosen is None:
                    logger.debug("Workday screener: no rule for '%s'", label_text[:60])
                    continue

                # Click button to open the Workday custom dropdown
                try:
                    btn.click(timeout=3000)
                    _human_pause(0.5, 1.0)
                except Exception as exc:
                    logger.debug("Workday screener: btn click failed '%s': %s", label_text[:40], exc)
                    continue

                # Wait for dropdown options to appear
                opt_sel = (
                    f"[data-automation-id='promptOption']:has-text('{chosen}'), "
                    f"li:has-text('{chosen}'), "
                    f"[role='option']:has-text('{chosen}')"
                )
                try:
                    page.wait_for_selector(opt_sel, state="visible", timeout=4000)
                    page.locator(opt_sel).first.click(timeout=3000)
                    logger.info("Workday screener: '%s' → %s", label_text[:60], chosen)
                    filled += 1
                    _human_pause(0.3, 0.6)
                except Exception as exc:
                    # Try clicking first option if exact match not found
                    logger.debug("Workday screener: option '%s' not found for '%s': %s", chosen, label_text[:40], exc)
                    # Close the dropdown if open
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("Workday screener btn: %s", exc)
    except Exception as exc:
        logger.debug("_fill_workday_screener_questions: %s", exc)

    # Also attempt native <select> fill (some Workday instances do use native selects)
    try:
        selects = page.locator("select").all()
        for sel_loc in selects:
            try:
                cur = (sel_loc.input_value() or "").strip()
                if cur and cur.lower() not in ("", "select one", "-- select --", "please select"):
                    continue
                label_text = (sel_loc.evaluate(
                    "el => { let id = el.id; if (id) { "
                    "let lbl = document.querySelector(`label[for='${id}']`); "
                    "if (lbl) return lbl.innerText; } "
                    "return (el.closest('div,fieldset')?.querySelector('label,legend,p')?.innerText || ''); }"
                ) or "").lower().strip()
                if not label_text:
                    continue
                chosen = None
                for keywords, answer in _RULES:
                    if any(kw in label_text for kw in keywords):
                        chosen = answer
                        break
                if chosen is None:
                    continue
                try:
                    sel_loc.select_option(label=chosen, timeout=2000)
                    logger.info("Workday screener (native select): '%s' → %s", label_text[:60], chosen)
                    filled += 1
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        pass

    return filled


def _wc_safe_click(page, locator) -> bool:
    """Click a WhiteCarrot button with force-fallback. Returns True on success."""
    try:
        locator.click(timeout=5000)
        return True
    except Exception:
        pass
    try:
        locator.click(force=True, timeout=5000)
        return True
    except Exception:
        pass
    return False


def _wc_dismiss_modals(page) -> None:
    """Dismiss WhiteCarrot CV-parser suggestions modal and similar overlays.

    After resume upload WhiteCarrot shows an autofill-suggestions modal with
    'Ignore' / 'Replace all' / 'Done' buttons that overlays the form.
    """
    for dismiss_sel in [
        "button:has-text('Done')",
        "button:has-text('Ignore')",
        "button:has-text('Close')",
        "button:has-text('Skip')",
        "button:has-text('Cancel')",
        "[aria-label='Close']",
        "[aria-label='Dismiss']",
    ]:
        try:
            loc = page.locator(dismiss_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=3000)
                logger.info("WhiteCarrot: dismissed modal via %r", dismiss_sel)
                _human_pause(0.5, 1.0)
                return
        except Exception:
            continue


def _handle_whitecarrot_multistep(page, answers: dict, folder_path: str) -> None:
    """Loop through WhiteCarrot's multi-step profile-builder until submitted.

    Page 1: basic info (name, phone, LinkedIn, CV) — filled by _fill_whitecarrot_form.
    After CV upload WhiteCarrot may show an autofill-suggestions modal — dismissed
    before clicking Next step.
    Subsequent pages: detected fields filled by fast_fill_form.
    Exits when a Submit/Apply button is clicked or a confirmation page is detected.
    """
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form

    _NEXT_SELS = [
        "button:has-text('Next step')",
        "button:has-text('Next Step')",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Save & Continue')",
    ]
    _SUBMIT_SELS = [
        "button:has-text('Submit application')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "button:has-text('Apply Now')",
        "button:has-text('Apply now')",
        "button:has-text('Finish')",
        "button:has-text('Complete')",
    ]
    _CONFIRM_PHRASES = (
        "application submitted", "thank you", "we'll be in touch",
        "we will be in touch", "successfully submitted", "application received",
    )

    MAX_STEPS = 25
    for step in range(MAX_STEPS):
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        _human_pause(1.5, 2.5)

        # Dismiss any modal/overlay that blocks the form (CV parser, suggestions, etc.)
        _wc_dismiss_modals(page)
        _human_pause(0.5, 1.0)

        _screenshot(page, folder_path, f"wc_step{step:02d}.png")

        # Check for confirmation
        try:
            body = (page.evaluate("() => document.body.innerText") or "").lower()
            if any(p in body for p in _CONFIRM_PHRASES):
                logger.info("WhiteCarrot: confirmed submitted on step=%d", step)
                return
        except Exception:
            pass

        if step == 0:
            # First page: use dedicated direct-fill
            _fill_whitecarrot_form(page, answers, folder_path)
            # Wait for CV parse then dismiss parser modal (up to 12 s)
            for _w in range(12):
                try:
                    b = page.evaluate("() => document.body.innerText") or ""
                    if "CV uploaded successfully" in b or "successfully" in b.lower():
                        break
                except Exception:
                    pass
                _human_pause(0.8, 1.0)
            # Dismiss CV parser suggestions modal that may appear after upload
            _wc_dismiss_modals(page)
            _human_pause(1.0, 1.5)
        else:
            # Later pages: use fast_fill
            detected = extract_form_fields(page)
            if detected:
                fast_fill_form(page, detected, answers)
            # WhiteCarrot question pages: fill any number input with years_experience
            try:
                num_inp = page.locator("input[type='number'], input[placeholder*='number' i], input[placeholder*='year' i]").first
                if num_inp.count() > 0 and num_inp.is_visible(timeout=1000):
                    cur = (num_inp.input_value() or "").strip()
                    if not cur:
                        raw = str(answers.get("years_experience", "3"))
                        # Strip ranges like "3-5" → "3"
                        num_val = raw.split("-")[0].strip()
                        num_inp.click(click_count=3, timeout=2000)
                        num_inp.type(num_val, delay=40)
                        logger.info("WhiteCarrot: filled number question = %s", num_val)
            except Exception as _ne:
                logger.debug("WhiteCarrot number fill: %s", _ne)

        # Scroll to bottom so nav buttons are reachable
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            _human_pause(0.5, 1.0)
        except Exception:
            pass

        # Dismiss any modal that appeared again after filling
        _wc_dismiss_modals(page)

        # Try Submit first
        for sel in _SUBMIT_SELS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    if _wc_safe_click(page, loc):
                        logger.info("WhiteCarrot: clicked Submit step=%d sel=%r", step, sel)
                        try:
                            page.wait_for_load_state("networkidle", timeout=15_000)
                        except Exception:
                            pass
                        _human_pause(2.0, 3.0)
                        _screenshot(page, folder_path, "submit_confirmation.png")
                        return
            except Exception:
                continue

        # Try Next
        clicked = False
        for sel in _NEXT_SELS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    if _wc_safe_click(page, loc):
                        logger.info("WhiteCarrot: clicked Next step=%d sel=%r", step, sel)
                        clicked = True
                        break
            except Exception:
                continue

        if not clicked:
            # Log buttons for diagnosis
            try:
                btns = page.evaluate(
                    "() => [...document.querySelectorAll('button')].slice(0,15)"
                    ".map(b => (b.textContent||'').trim().slice(0,50))"
                )
                logger.warning("WhiteCarrot step=%d: no Next/Submit found; buttons: %s", step, btns)
            except Exception:
                pass
            raise RuntimeError(
                f"Could not find submit button on {page.url}. "
                "Form may have multiple pages or require manual completion."
            )

    raise RuntimeError(f"WhiteCarrot: exceeded {MAX_STEPS} steps without submitting")


def _handle_workday_pages(page, app_id: int, answers: dict, folder_path: str, fill_delay: float, max_steps: int = 60) -> None:
    """Step through Workday applyManually multi-page wizard until submitted.

    Workday's applyManually form has up to 6 named steps:
      My Information → My Experience → Application Questions →
      Voluntary Disclosures → Self Identify → Review/Submit

    After filling each page, click Next. On the Review page click Submit.
    The caller is responsible for transitioning state after this returns.
    """
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form

    for step in range(max_steps):
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        _human_pause(1.0, 2.0)

        url = page.url
        logger.info("app_id=%d Workday step=%d url=%s", app_id, step, url)
        _screenshot(page, folder_path, f"workday_step{step:02d}.png")

        _check_auth_wall(page)

        # Detect and recover from Workday transient "Something went wrong" error page
        try:
            body_check = page.evaluate("() => document.body.innerText") or ""
            if "Something went wrong" in body_check and "refresh" in body_check.lower():
                logger.warning("app_id=%d Workday: 'Something went wrong' at step=%d — refreshing", app_id, step)
                page.reload(timeout=15_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                _human_pause(2.0, 3.0)
        except Exception:
            pass

        # Fill current page fields
        detected = extract_form_fields(page)
        if detected:
            fast_fill_form(page, detected, answers)

        # Fill Workday questionnaire pages (native <select> screener questions)
        sq_filled = _fill_workday_screener_questions(page, answers)
        if sq_filled:
            logger.info("app_id=%d Workday: filled %d screener question(s) step=%d", app_id, sq_filled, step)

        # Fix Workday-specific fields that fast_fill handles incorrectly.
        # Detect My Information page via data-automation-id (standard Workday) OR
        # body text (Danaher and other Workday instances use different automation IDs).
        try:
            _workday_on_my_info = bool(
                page.locator(
                    "[data-automation-id='phoneDeviceType'], "
                    "[data-automation-id='countryPhoneCode']"
                ).count()
            )
            if not _workday_on_my_info:
                body_text = page.evaluate("() => document.body.innerText") or ""
                _workday_on_my_info = (
                    "Phone Device Type" in body_text
                    or "Country Phone Code" in body_text
                    or ("Zip Code" in body_text and "State" in body_text)
                    or ("Phone Number" in body_text and "Address" in body_text)
                )
                if _workday_on_my_info:
                    logger.info("app_id=%d Workday: My Info page detected via body text (step=%d)", app_id, step)
        except Exception:
            _workday_on_my_info = False
        if _workday_on_my_info:
            _fill_workday_my_info(page, answers)

        # Scroll to bottom so all nav buttons are in reach
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            _human_pause(0.5, 1.0)
        except Exception:
            pass

        # Strategy: try Next first. Workday's sidebar always shows "Review" as a
        # future step name, so checking page text for "Review" false-positives on
        # every page. Instead we only attempt Submit when Next is absent.
        clicked_next = False
        for sel in _WORKDAY_NEXT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                if not loc.is_visible():
                    continue
                _human_click(page, loc)
                logger.info("app_id=%d Workday: clicked Next step=%d sel=%r", app_id, step, sel)
                clicked_next = True
                break
            except Exception:
                continue

        if clicked_next:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            # Detect if clicking "Next" actually submitted (Workday reuses the
            # bottom-navigation-next-button ID on the final Review step).
            new_url = page.url.lower()
            if any(x in new_url for x in ("thankyou", "thank-you", "confirmation", "submitted", "success")):
                logger.info("app_id=%d Workday: submission confirmed (url=%s)", app_id, page.url)
                return
            try:
                body = page.evaluate("() => document.body.innerText") or ""
                if any(x in body for x in (
                    "Application Submitted", "Thank you", "Thank You",
                    "We have received your application", "successfully submitted",
                )):
                    logger.info("app_id=%d Workday: submission confirmed via page text", app_id)
                    return
            except Exception:
                pass
            continue  # next iteration of the step loop

        # No Next found — we must be on the final Review/Submit page
        logger.info("app_id=%d Workday: no Next button — trying Submit on step=%d url=%s", app_id, step, url)
        for sel in _WORKDAY_SUBMIT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    _human_click(page, loc)
                    logger.info("app_id=%d Workday: clicked Submit step=%d sel=%r", app_id, step, sel)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=int(_SUBMIT_WAIT * 1000))
                    except Exception:
                        pass
                    _human_pause(2.0, 3.0)
                    return
            except Exception:
                continue

        # Log all buttons to help diagnose selector mismatches
        try:
            btns = page.evaluate("""
                () => [...document.querySelectorAll('button,[role=button]')].slice(0,15)
                        .map(b => (b.textContent||'').trim().slice(0,40) + '|' + (b.getAttribute('data-automation-id')||'') + '|' + (b.getAttribute('aria-label')||''))
            """)
            logger.warning("app_id=%d Workday: no Next or Submit found step=%d; page buttons: %s", app_id, step, btns)
        except Exception:
            pass
        raise RuntimeError(f"Workday: no Next or Submit button found on step={step} url={url}")

    raise RuntimeError(f"Workday: exceeded {max_steps} steps without reaching Submit")


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

    # External apply fallback: find Apply link and follow it.
    # LinkedIn's "Apply on company website" button opens a NEW TAB (target=_blank),
    # so we must capture the new page from context rather than waiting for same-tab navigation.
    # Selectors are SCOPED to the top job card to avoid matching sidebar/suggested-job Apply buttons.
    external_url = None
    apply_selectors = [
        # Scoped to LinkedIn top card (avoids sidebar Apply button cross-contamination)
        ".jobs-unified-top-card button[aria-label*='Apply']",
        ".jobs-unified-top-card a[aria-label*='Apply']",
        ".jobs-details-top-card button[aria-label*='Apply']",
        ".jobs-details-top-card a[aria-label*='Apply']",
        "div.jobs-apply-button--top-card button",
        "div.jobs-apply-button--top-card a",
        # Aria-label approach — more specific than has-text
        "button[aria-label*='Apply on company website']",
        "a[aria-label*='Apply on company website']",
        # Fallback: any aria-label containing Apply (broad but avoids raw text match)
        "button[aria-label*='Apply']",
        "a[aria-label*='Apply']",
        ".jobs-apply-button",
    ]
    for sel in apply_selectors:
        el = page.locator(sel).first
        if el.count() == 0:
            continue
        # If it's an anchor with a non-LinkedIn href, use it directly
        href = el.get_attribute("href") or ""
        if href and "linkedin.com" not in href and href.startswith("http"):
            external_url = href
            logger.info("app_id=%d external apply href: %s", app_id, href)
            break
        # Click and capture new tab (LinkedIn opens in _blank) or same-tab navigation
        try:
            ctx = page.context
            with ctx.expect_page(timeout=12000) as new_page_info:
                el.click()
            new_page = new_page_info.value
            new_page.wait_for_load_state("domcontentloaded", timeout=20000)
            # LinkedIn sometimes uses a relay URL (linkedin.com/apply-redirect/…)
            # that JS-redirects to the external ATS. Wait for the final URL.
            if "linkedin.com" in new_page.url:
                try:
                    new_page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            new_url = new_page.url
            if "linkedin.com" not in new_url:
                external_url = new_url
                # Continue using the new tab as our active page
                page = new_page
                logger.info("app_id=%d external apply new-tab URL: %s", app_id, external_url)
                break
            new_page.close()
        except Exception:
            # No new tab — check if same-tab navigated
            _human_pause(1.5, 2.5)
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


def _dismiss_cookie_modals(page) -> None:
    """Click common cookie consent / privacy-policy dismiss buttons.

    Called before any Apply or form interaction so overlays don't intercept clicks.
    Covers: NYL/eightfold.ai, OneTrust, Cookiebot, TrustArc, custom banners.
    Never raises — failure is silently ignored.
    """
    _consent_selectors = [
        "button:has-text('I Understand')",
        "button:has-text('I understand')",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Accept')",
        "button:has-text('Agree')",
        "button:has-text('Agree and Proceed')",
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "button:has-text('Close')",
        "button#onetrust-accept-btn-handler",
        "button.cc-btn.cc-dismiss",
        "[data-testid='cookie-accept']",
        "[aria-label='Accept cookies']",
        "[aria-label='Close cookie banner']",
        ".cookie-banner button",
        "#cookie-banner button",
    ]
    for sel in _consent_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2000)
                _human_pause(0.5, 1.0)
                logger.info("Dismissed cookie modal via %r", sel)
                return  # one dismissal is usually enough
        except Exception:
            continue


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
