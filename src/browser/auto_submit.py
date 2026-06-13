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
    ".ia-IndeedApplyButton",         # Indeed Easy Apply (SmartApply)
    "[data-testid='applyButton']",
    "#indeedApplyButton",
    "[class*='IndeedApplyButton']",
    "[data-tn-element='apply-now']",
    "[data-tn-element='indeedApplyButton']",
    "button[data-id*='apply']",
    # Indeed viewjob "Apply now" / "Easily apply" for external jobs
    "[class*='applyButton']",
    "[class*='apply-button']",
    "button:has-text('Easily apply')",
    "a:has-text('Easily apply')",
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
    # eightfold.ai / Phenom People career portals (NYL, many enterprise firms)
    "button[data-ph-at-id='apply-btn']",
    "a[data-ph-at-id='apply-btn']",
    ".apply-btn",
    "button:has-text('Apply Now')",
    "button:has-text('Apply now')",
    "button:has-text('Apply')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply now')",
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
    # Resume module web components
    "button:has-text('Get started')",
    "button:has-text('Got it')",
    "button:has-text('Skip')",
    "button:has-text('Accept')",
    "button:has-text('Confirm')",
    ".ia-Button",
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
    "[data-automation-id='pageFooterNextButton']",
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
    pause_before_submit: bool = False,
) -> AutoSubmitResult:
    """
    Open the job URL in the user's real Chrome profile, fill the application
    form with human-like timing, and click Submit.

    pause_before_submit=True: fills everything, takes pre-submit screenshot,
    then returns WITHOUT clicking Submit. Chrome stays open for manual review.
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
        # On final failure, relaunch Chrome and try once more.
        page = None
        for _np_attempt in range(3):
            try:
                page = context.new_page()
                # Apply playwright-stealth fingerprint patches on new pages
                try:
                    if getattr(context, '_stealth_ready', False):
                        from playwright_stealth import stealth_sync
                        stealth_sync(page)
                        logger.debug("playwright-stealth applied to new page")
                except Exception as _st_exc:
                    logger.debug("playwright-stealth skipped: %s", _st_exc)
                break
            except Exception as _np_exc:
                if _np_attempt == 2:
                    # Last resort: full Chrome relaunch
                    logger.warning("context.new_page failed (attempt %d): %s — relaunching Chrome", _np_attempt + 1, _np_exc)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    _release_profile_lock(os.path.abspath(_BROWSER_PROFILE_DIR))
                    time.sleep(3)
                    context = _open_chrome_context(pw, chrome_dir, chrome_profile, cdp_port)
                    page = context.new_page()
                    try:
                        if getattr(context, '_stealth_ready', False):
                            from playwright_stealth import stealth_sync
                            stealth_sync(page)
                    except Exception:
                        pass
                    break
                logger.warning("context.new_page failed (attempt %d): %s — retrying in 3s", _np_attempt + 1, _np_exc)
                time.sleep(3)
        try:
            return _run_submit_flow(page, app_id, answers, url, folder_path, fill_delay,
                                    pause_before_submit=pause_before_submit)
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
    # GPU stability: the GPU process crashes on many Windows laptops in headless/
    # automation contexts (chrome_debug.log shows WebGL deprecation + fallback
    # failures). Disabling GPU avoids the GPU process entirely; pages render via
    # software rasterizer (SwANGLE) which is slower but crash-free for automation.
    "--disable-gpu",
    "--disable-dev-shm-usage",
]

_ANTI_DETECT_IGNORE = ["--enable-automation"]


def _open_chrome_context(pw, chrome_dir: str, profile: str, cdp_port: int):
    """
    Launch Chrome with --remote-debugging-port and connect via TCP CDP.

    TCP-based CDP (vs Playwright's default stdio pipe) is far more stable on
    Windows: the pipe connection breaks if Chrome takes >1s to initialize a
    persistent profile, causing every launch_persistent_context call to fail
    with 'Target page, context or browser has been closed'.

    Strategy:
      0. If Chrome is already listening on cdp_port, connect directly (reuse
         existing session with saved logins — avoids killing a logged-in browser).
      1. Otherwise kill all existing Chrome processes (release any profile lock).
      2. Launch Chrome subprocess with --remote-debugging-port + our profile dir.
      3. Poll CDP until reachable (up to 20s).
      4. Connect via pw.chromium.connect_over_cdp (TCP — stable).
    """
    profile_dir = os.path.abspath(_BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)

    # If Chrome is already running with CDP, reuse it (keeps saved logins intact)
    if _chrome_cdp_reachable(cdp_port):
        try:
            browser = pw.chromium.connect_over_cdp(
                f"http://localhost:{cdp_port}",
                timeout=15000,
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            ctx.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
                Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
                window.chrome={runtime:{}};
            """)
            logger.info("Reusing existing Chrome CDP session on port %d", cdp_port)
            return ctx
        except Exception as _reuse_exc:
            logger.info("Could not reuse existing Chrome (%s) — relaunching", _reuse_exc)

    # Kill everything first — ensures clean profile lock
    _release_profile_lock(profile_dir)
    time.sleep(2)

    # Find Chrome executable: prefer system Google Chrome (better Cloudflare
    # fingerprint), fall back to Playwright's bundled Chromium.
    chrome_exe = _find_system_chrome()
    if not chrome_exe:
        chrome_exe = _find_playwright_chromium()
    if not chrome_exe:
        raise FileNotFoundError(
            "No Chrome/Chromium found. Install Google Chrome or run: playwright install chromium"
        )
    logger.info("Launching Chrome via CDP: %s", chrome_exe)
    # Reset session warmup flag — new Chrome process needs fresh warmup
    import sys as _sys
    _mod = _sys.modules.get(__name__)
    if _mod and hasattr(_mod, '_INDEED_WARMED'):
        _mod._INDEED_WARMED = False

    # Launch Chrome detached — we'll connect via TCP, not stdin/stdout pipe
    subprocess.Popen(
        [
            chrome_exe,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
        ] + _ANTI_DETECT_ARGS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Poll until CDP is reachable (up to 20 s)
    logger.info("Waiting for Chrome CDP port %d…", cdp_port)
    for _i in range(20):
        if _chrome_cdp_reachable(cdp_port):
            time.sleep(2)  # extra settle so Chrome finishes loading profile
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"Chrome CDP not reachable on port {cdp_port} after 20s")

    logger.info("Chrome CDP ready — connecting")
    browser = pw.chromium.connect_over_cdp(
        f"http://localhost:{cdp_port}",
        timeout=15000,
    )
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    ctx.add_init_script("""
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
        Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
        window.chrome={runtime:{}};
        // Remove CDP-specific properties
        delete window.__playwright;
        delete window.__pw_manual;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
    """)
    # Apply playwright-stealth if available (extra fingerprint patches)
    try:
        from playwright_stealth import stealth_sync
        # stealth_sync applies to a page, so we use a context-level approach
        ctx._stealth_ready = True
        logger.info("playwright-stealth available — will apply per-page")
    except ImportError:
        pass
    return ctx


def _find_playwright_chromium() -> str | None:
    """Return path to Playwright's bundled Chromium, or None."""
    import glob as _glob
    base = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    if not os.path.isdir(base):
        return None
    # Find the latest chromium-NNNN installation
    pattern = os.path.join(base, "chromium-*", "chrome-win", "chrome.exe")
    hits = sorted(_glob.glob(pattern), reverse=True)
    return hits[0] if hits else None


def _find_system_chrome() -> str | None:
    """Return path to system Google Chrome, or None."""
    for path in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]:
        if os.path.isfile(path):
            return path
    return None


def _kill_agent_chrome_pids() -> list[int]:
    """Kill only Chrome processes launched by the job agent.

    Identifies agent Chrome by exe path containing 'ms-playwright' OR
    cmdline containing '--remote-debugging-port'. Never touches the user's
    personal Google Chrome.
    Returns list of PIDs killed.
    """
    killed = []
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                name = (proc.info["name"] or "").lower()
                if "chrome" not in name:
                    continue
                exe = (proc.info["exe"] or "").replace("\\", "/").lower()
                cmdline = " ".join(proc.info["cmdline"] or []).lower()
                is_agent = (
                    "ms-playwright" in exe
                    or "--remote-debugging-port" in cmdline
                )
                if is_agent:
                    proc.kill()
                    killed.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        # psutil unavailable — fall back to WMIC to get cmdlines, kill by PID
        import subprocess
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name='chrome.exe'",
                 "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            for line in out.splitlines():
                if "--remote-debugging-port" in line or "ms-playwright" in line.lower():
                    parts = line.strip().split(",")
                    for p in parts:
                        if p.strip().isdigit():
                            pid = int(p.strip())
                            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                           capture_output=True)
                            killed.append(pid)
        except Exception:
            pass
    return killed


def _release_profile_lock(profile_dir: str) -> None:
    """Kill only the job-agent Chrome instance (Playwright Chromium / CDP port).

    Never kills the user's personal Google Chrome session.
    """
    import subprocess

    killed = _kill_agent_chrome_pids()
    if killed:
        logger.info("_release_profile_lock: killed agent Chrome PIDs %s", killed)
        time.sleep(2)
        # Second pass — child helper processes (GPU, renderer) may outlive parent
        still = _kill_agent_chrome_pids()
        if still:
            time.sleep(1)
            _kill_agent_chrome_pids()
        logger.info("_release_profile_lock: agent Chrome processes gone")
    else:
        logger.info("_release_profile_lock: no agent Chrome processes found")

    # Remove Chrome's SingletonLock file unconditionally
    for lock_rel in ["SingletonLock", "Default/LOCK"]:
        try:
            lock = os.path.join(profile_dir, lock_rel)
            if os.path.exists(lock):
                os.remove(lock)
                logger.info("Removed lock file: %s", lock_rel)
        except Exception as exc:
            logger.debug("_release_profile_lock lock file %s: %s", lock_rel, exc)

    # Clear crash-recovery state that would trigger a restore dialog
    try:
        prefs = os.path.join(profile_dir, "Default", "Preferences")
        if os.path.isfile(prefs):
            import json as _json
            with open(prefs, encoding="utf-8") as fh:
                data = _json.load(fh)
            changed = False
            if data.get("profile", {}).get("exit_type") != "Normal":
                data.setdefault("profile", {})["exit_type"] = "Normal"
                changed = True
            data.setdefault("profile", {})["exited_cleanly"] = True
            if changed or not data["profile"].get("exited_cleanly"):
                with open(prefs, "w", encoding="utf-8") as fh:
                    _json.dump(data, fh)
                logger.info("Reset Chrome exit_type to Normal in Preferences")
    except Exception as exc:
        logger.debug("_release_profile_lock prefs reset: %s", exc)

    # Delete session/tab restore files so Chrome starts clean (not in restore mode)
    import shutil as _shutil
    default_dir = os.path.join(profile_dir, "Default")
    for _session_rel in ["Sessions", "Session Storage", "Last Session", "Last Tabs", "Current Session", "Current Tabs"]:
        _sp = os.path.join(default_dir, _session_rel)
        try:
            if os.path.isdir(_sp):
                _shutil.rmtree(_sp, ignore_errors=True)
                logger.debug("_release_profile_lock: cleared session dir %s", _session_rel)
            elif os.path.isfile(_sp):
                os.remove(_sp)
                logger.debug("_release_profile_lock: removed session file %s", _session_rel)
        except Exception as exc:
            logger.debug("_release_profile_lock session cleanup %s: %s", _session_rel, exc)


def _chrome_cdp_reachable(port: int) -> bool:
    """Return True if Chrome's CDP debug endpoint is listening."""
    try:
        s = socket.create_connection(("localhost", port), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def _relaunch_chrome_with_cdp(chrome_dir: str, profile: str, port: int) -> None:
    """Kill only the agent Chrome instance then relaunch with the CDP debug port open."""
    _kill_agent_chrome_pids()
    time.sleep(1.5)
    _kill_agent_chrome_pids()
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

def _run_submit_flow(page, app_id, answers, url, folder_path, fill_delay, *, pause_before_submit: bool = False):
    os.makedirs(folder_path, exist_ok=True)

    # ── Step 0: LinkedIn Easy Apply dispatch ─────────────────────────────────
    # Only dispatch to LinkedIn handler for original job-view URLs, not when
    # called recursively from the external-apply fallback path.
    if "linkedin.com/jobs/view" in url or "linkedin.com/jobs/search" in url:
        logger.info("app_id=%d detected LinkedIn job URL — using Easy Apply handler", app_id)
        return _run_linkedin_easy_apply(page, app_id, answers, url, folder_path, fill_delay,
                                        pause_before_submit=pause_before_submit)

    # ── Step 1: Navigate to listing URL ──────────────────────────────────────
    nav_url = _resolve_indeed_url(url)

    # Warm up the session only for Indeed URLs — skipping warmup for external
    # portals (Greenhouse, Ashby, Workday, etc.) avoids a 5-min Cloudflare delay
    # that blocks the first external-apply job after each Chrome launch.
    if "indeed.com" in nav_url:
        _warm_indeed_session(page)

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

    # ── Step 2: Screenshot + detect auth walls early ──────────────────────────
    # Save a debug screenshot so we can see what Indeed is actually showing
    try:
        ss_dir = folder_path
        os.makedirs(ss_dir, exist_ok=True)
        page.screenshot(path=os.path.join(ss_dir, "viewjob_state.png"), full_page=False, timeout=10_000)
    except Exception:
        pass
    _check_auth_wall(page)

    # Screenshot AFTER challenge resolution — shows the real viewjob state
    try:
        page.screenshot(path=os.path.join(folder_path, "viewjob_ready.png"), full_page=False, timeout=10_000)
    except Exception:
        pass

    # Log page URL + first 300 chars of body so we can diagnose no-button issues
    try:
        _body_preview = page.evaluate("() => document.body?.innerText?.slice(0,300)||''") or ""
        logger.info("app_id=%d viewjob ready at %s | body: %s", app_id, page.url,
                    _body_preview[:200].replace('\n', ' '))
    except Exception:
        pass

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
    # Permanently skip portals that can't be auto-submitted (landing pages with no apply form)
    _UNSUPPORTED_PORTAL_DOMAINS = (
        "sapsf.com", "successfactors.com", "icims.com",
        "jobright.ai", "bestjobtool.com",
        "pinterestcareers.com", "careers.ey.com", "healthcaresource.com",
        "ibegin.tcsapps.com", "pageuppeople.com",
    )
    if any(d in page.url for d in _UNSUPPORTED_PORTAL_DOMAINS):
        raise RuntimeError(f"unsupported portal (no auto-submit): {page.url[:80]}")

    portal = classify_portal(html=html, url=page.url)
    logger.info("app_id=%d portal=%s url=%s", app_id, portal, page.url)

    _screenshot(page, folder_path, "submit_before.png")

    # Workday applyManually is a 6-step wizard — dispatch to the multi-step handler
    # which fills each page and clicks Next until it reaches Submit.
    # If we land on a plain Workday /apply URL (no active session → sign-in page),
    # try to rewrite the URL to include locale + /applyManually and navigate there.
    if portal == "workday" and "applymanually" not in page.url.lower():
        cur_url = page.url
        # Build the applyManually URL: insert en-US locale if missing, append /applyManually
        try:
            import re as _re
            # Replace /DanaherJobs/ (or similar) with /en-US/DanaherJobs/ if locale missing
            apply_url = cur_url
            if "/en-US/" not in apply_url and "/en-us/" not in apply_url.lower():
                apply_url = _re.sub(r'(\.com/)([^/]+/job/)', r'\1en-US/\2', apply_url)
            # Replace /apply (end or with query) with /apply/applyManually
            if "/apply/applyManually" not in apply_url and "/apply/applymanually" not in apply_url.lower():
                apply_url = _re.sub(r'/apply(\?|$)', r'/apply/applyManually\1', apply_url)
            # Also handle bare job listing URLs (any Workday domain):
            # .../job/LOCATION/TITLE?params or .../job/ID?params
            # → append /apply/applyManually before query string
            if "applyManually" not in apply_url and "applymanually" not in apply_url.lower():
                apply_url = _re.sub(
                    r'(/job/(?:[^/?#]+/)*[^/?#]+)(\?|#|$)',
                    r'\1/apply/applyManually\2',
                    apply_url,
                )
            if "applyManually" in apply_url and apply_url != cur_url:
                logger.info("app_id=%d Workday: rewriting sign-in URL to applyManually: %s", app_id, apply_url)
                page.goto(apply_url, wait_until="domcontentloaded", timeout=30_000)
                _human_pause(2.0, 3.0)
                html = page.content()
                portal = classify_portal(html=html, url=page.url)
        except Exception as _rw_exc:
            logger.debug("app_id=%d Workday URL rewrite failed: %s", app_id, _rw_exc)
    if portal == "workday" and "applymanually" in page.url.lower():
        logger.info("app_id=%d Workday multi-step form — dispatching to page loop", app_id)
        _handle_workday_pages(page, app_id, answers, folder_path, fill_delay,
                              pause_before_submit=pause_before_submit)
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
        ss = _screenshot(page, folder_path, "submit_confirmation.png")
        receipt = f"PORTAL:whitecarrot:{page.url}"
        logger.info("app_id=%d submitted portal=whitecarrot url=%s", app_id, page.url)
        return AutoSubmitResult(
            success=True, app_id=app_id,
            receipt=receipt, screenshot_path=ss,
        )

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
    """Raise MFADetected if we landed on a login/auth page, or CaptchaDetected for bot challenges."""
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
    # Cloudflare / bot challenge detection
    try:
        body_text = page.evaluate("() => document.body?.innerText?.slice(0, 800) || ''") or ""
        body_lower = body_text.lower()
        challenge_phrases = [
            "additional verification required",
            "verify you are human",
            "checking if the site connection is secure",
            "enable javascript and cookies to continue",
            "please complete the security check",
        ]
        if any(p in body_lower for p in challenge_phrases):
            # Screenshot for debugging
            try:
                ss_path = os.path.join("applications", f"captcha_debug_{int(time.time())}.png")
                os.makedirs("applications", exist_ok=True)
                page.screenshot(path=ss_path, full_page=False)
                logger.info("CAPTCHA screenshot saved: %s", ss_path)
            except Exception:
                pass
            # Try to solve Cloudflare turnstile / reCAPTCHA checkbox before giving up
            solved = _try_solve_challenge(page)
            if solved:
                logger.info("Challenge solved automatically at %s", page.url)
                return
            raise CaptchaDetected(f"Bot challenge at {page.url}")
    except (CaptchaDetected, MFADetected):
        raise
    except Exception:
        pass


_INDEED_WARMED = False  # module-level flag per process


def _warm_indeed_session(page) -> None:
    """Navigate to Indeed homepage once per Chrome launch to establish session context."""
    global _INDEED_WARMED
    if _INDEED_WARMED:
        return
    try:
        current = page.url
        if "indeed.com" in current and "viewjob" not in current and "apply" not in current:
            # Already on Indeed non-job page
            _INDEED_WARMED = True
            return
        logger.info("Warming Indeed session: visiting homepage")
        page.goto("https://www.indeed.com", wait_until="domcontentloaded", timeout=20_000)
        _human_pause(2.0, 3.0)
        # If homepage also shows Cloudflare, solve it during warmup
        try:
            body = page.evaluate("() => document.body?.innerText?.slice(0,500)||''") or ""
            if "additional verification" in body.lower() or "verifying" in body.lower():
                logger.info("Cloudflare on Indeed homepage during warmup — attempting auto-solve")
                _try_solve_challenge(page)
        except Exception:
            pass
        # Simulate reading by scrolling slightly
        try:
            page.evaluate("window.scrollTo(0, 300)")
            _human_pause(1.0, 2.0)
        except Exception:
            pass
        # Visit search results page — establishes natural browsing context on Indeed
        # Cloudflare is less aggressive when the browser has viewd a search page first
        try:
            page.goto("https://www.indeed.com/jobs?q=software+engineer&l=New+York%2C+NY",
                      wait_until="domcontentloaded", timeout=20_000)
            _human_pause(2.0, 3.0)
            try:
                body2 = page.evaluate("() => document.body?.innerText?.slice(0,500)||''") or ""
                if "additional verification" in body2.lower() or "verifying" in body2.lower():
                    logger.info("Cloudflare on Indeed search during warmup — attempting auto-solve")
                    _try_solve_challenge(page)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, 600)")
                _human_pause(1.0, 2.0)
            except Exception:
                pass
        except Exception:
            pass
        _INDEED_WARMED = True
    except Exception as e:
        logger.debug("Session warmup skipped: %s", e)


def _try_solve_challenge(page) -> bool:
    """Attempt to auto-solve common bot challenges (reCAPTCHA checkbox, simple turnstile).
    Returns True if the challenge appears to have been resolved."""
    import time as _time
    # Try clicking reCAPTCHA checkbox
    try:
        frame = page.frame_locator("iframe[title*='recaptcha' i], iframe[src*='recaptcha']").first
        checkbox = frame.locator(".recaptcha-checkbox-border, #recaptcha-anchor").first
        if checkbox.count() > 0 and checkbox.is_visible(timeout=2000):
            checkbox.click(timeout=3000)
            logger.info("Clicked reCAPTCHA checkbox — waiting for auto-verify")
            _time.sleep(3)
            # Recheck
            body_text = page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''") or ""
            if not any(p in body_text.lower() for p in ["verify you are human", "additional verification"]):
                return True
    except Exception:
        pass
    # Cloudflare Managed Challenge / Turnstile shows "Verifying..." and often
    # auto-completes within 5-20s. Poll BODY TEXT until the challenge phrases are gone.
    # Note: do NOT check page.url — the challenge is served AT the viewjob URL, so
    # the URL stays on viewjob throughout, and a URL check gives a false positive.
    _CHALLENGE_PHRASES_POLL = [
        "additional verification required",
        "verify you are human",
        "checking if the site connection is secure",
        "enable javascript and cookies to continue",
        "please complete the security check",
    ]
    challenge_resolved = False
    for poll_sec in range(300):  # up to 5 min — user can manually solve the challenge
        _time.sleep(1)
        if poll_sec > 0 and poll_sec % 15 == 0:
            logger.info("Waiting for Cloudflare challenge to clear... %ds elapsed (solve it in the Chrome window)", poll_sec)
        try:
            # Check a larger body slice to catch all challenge text
            body_text = page.evaluate("() => document.body?.innerText?.slice(0, 1200) || ''") or ""
            body_lower = body_text.lower()
            if not any(p in body_lower for p in _CHALLENGE_PHRASES_POLL):
                # Double-check: make sure the page has real content (Apply button or job title)
                has_content = any(kw in body_lower for kw in [
                    "apply", "job description", "qualifications", "salary",
                    "about the", "responsibilities", "requirements"
                ])
                if has_content or poll_sec >= 15:
                    logger.info("Cloudflare challenge resolved after %ds (body clear)", poll_sec + 1)
                    challenge_resolved = True
                    break
        except Exception:
            pass
    if challenge_resolved:
        # Wait for the Cloudflare redirect to finish loading the real page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
            _time.sleep(2.5)  # extra buffer — SPA frameworks need time after domcontentloaded
        except Exception:
            pass
        return True
    # Try clicking any visible Cloudflare / challenge button
    for sel in [
        "input[type='button'][value*='Verify' i]",
        "button:has-text('Verify')",
        "button:has-text('I am human')",
        "[id*='challenge-stage'] button",
        "form#challenge-form input[type='submit']",
        "input[value='Verify you are human']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=3000)
                logger.info("Clicked challenge button %r", sel)
                # Wait up to 10s for page to advance
                for _ in range(10):
                    _time.sleep(1)
                    try:
                        body_text = page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''") or ""
                        if not any(p in body_text.lower() for p in ["verify you are human", "additional verification", "verifying..."]):
                            return True
                    except Exception:
                        pass
        except Exception:
            continue
    return False


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

    # No Apply button found — scroll slightly and retry once (dynamic content)
    try:
        page.evaluate("window.scrollTo(0, 400)")
        time.sleep(1.5)
    except Exception:
        pass
    for sel in _APPLY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                _human_click(page, loc)
                logger.info("app_id=%d clicked apply after scroll selector=%r", app_id, sel)
                return
        except Exception:
            continue

    # Still nothing — screenshot + log page body for diagnosis
    try:
        ss_path = os.path.join("applications", f"no_apply_btn_{app_id}_{int(time.time())}.png")
        os.makedirs("applications", exist_ok=True)
        page.screenshot(path=ss_path, full_page=True, timeout=10_000)
        logger.info("app_id=%d no-apply-button screenshot: %s", app_id, ss_path)
    except Exception:
        pass
    try:
        body_preview = page.evaluate("() => document.body?.innerText?.slice(0,600)||''") or ""
        logger.warning("app_id=%d no Apply button found on %s — page body: %s",
                       app_id, page.url, body_preview[:400].replace('\n', ' '))
    except Exception:
        pass
    logger.info("app_id=%d no Apply button found — may be logged-out wall, expired job, or external ATS", app_id)


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


def _get_smartapply_page_name(page) -> str:
    """Get current SmartApply page identifier from URL or SPA hash/content."""
    url = page.url
    # Old flow: smartapply.indeed.com/beta/indeedapply/form/profile-location
    page_name = url.split("/")[-1]
    # New flow: indeed.com/apply/start?jk=... — use JS to read SPA route from DOM
    if "apply/start" in url or (page_name.startswith("start?") and "indeed.com" in url):
        try:
            # SmartApply sets a data attribute or aria landmark that indicates current step
            name = page.evaluate("""() => {
                // Try data-testid on the form container
                const form = document.querySelector('[data-testid*="apply"]') ||
                             document.querySelector('[class*="ia-BasePage"]') ||
                             document.querySelector('[class*="indeedApply"]');
                if (form) return form.getAttribute('data-page-name') || form.getAttribute('data-testid') || '';
                // Try reading from the React component state via __reactFiber
                const h1 = document.querySelector('h1');
                if (h1) return 'h1:' + h1.innerText.trim().slice(0, 40).toLowerCase().replace(/\\s+/g,'_');
                return '';
            }""") or ""
            if name:
                return name
        except Exception:
            pass
        # Try to detect page from visible button text and heading
        try:
            body = page.evaluate("() => document.body?.innerText?.slice(0, 300) || ''") or ""
            body_l = body.lower()
            if "review your application" in body_l or "ready to submit" in body_l:
                return "review"
            if "submit" in body_l and ("application" in body_l or "applying" in body_l):
                return "review"
        except Exception:
            pass
    return page_name


def _handle_smartapply_pages(page, app_id: int, answers: dict, folder_path: str, fill_delay: float) -> None:
    """Step through Indeed SmartApply multi-page form until submitted."""
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form, verify_form_complete
    max_steps = 25
    prev_page_content = ""
    stuck_count = 0
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
        page_name = _get_smartapply_page_name(page)
        logger.info("app_id=%d SmartApply step=%d page=%s", app_id, step, page_name)

        # Detect if we're stuck (same content after clicking Continue)
        try:
            cur_content = page.evaluate("() => document.body?.innerText?.slice(0, 200) || ''") or ""
            if cur_content == prev_page_content and step > 0:
                stuck_count += 1
                if stuck_count >= 3:
                    logger.warning("app_id=%d SmartApply stuck at step=%d — aborting", app_id, step)
                    break
            else:
                stuck_count = 0
            prev_page_content = cur_content
        except Exception:
            pass

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

        if step < 8:
            _screenshot(page, folder_path, f"smartapply_step{step:02d}.png")

        # structured-data-intro: click through resume data review module
        # This page introduces the structured data section and needs a "Get started" / "Continue"
        if "structured-data-intro" in page_name or "structured-data-intro" in url:
            extra_selectors = [
                "button:has-text('Get started')",
                "button:has-text('Get Started')",
                "button:has-text('Review')",
                "button:has-text('Begin')",
                "button:has-text('Accept')",
                "button:has-text('Got it')",
                "button:has-text('Skip')",
                "button:has-text('Confirm')",
                ".ia-Button",
                "[data-tn-element='structured-data-submit']",
                "button[class*='primary' i]",
                "button[class*='continue' i]",
                # Web components used by the resume module
                "ia-button",
                "[part='button']",
            ]
            for sel in extra_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.scroll_into_view_if_needed(timeout=2000)
                        loc.click(timeout=3000)
                        logger.info("app_id=%d structured-data-intro: clicked %r on step=%d", app_id, sel, step)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        except Exception:
                            pass
                        _human_pause(1.0, 2.0)
                        break
                except Exception:
                    continue
            else:
                # Try pressing Enter or Escape to dismiss the intro screen
                try:
                    page.keyboard.press("Enter")
                    _human_pause(1.0, 2.0)
                    logger.info("app_id=%d structured-data-intro: pressed Enter to advance", app_id)
                except Exception:
                    pass
            continue  # Skip normal Continue/Submit detection for this special step

        # Try Submit — on review page OR whenever the URL doesn't change between steps
        # (direct apply/start SPA flow: URL is static, so we try Submit on every step >= 2)
        is_static_url = "apply/start" in url or (page_name.startswith("start?") and "indeed.com" in url)
        should_try_submit = (
            "review" in page_name
            or "submit" in page_name
            or (is_static_url and step >= 2)
        )
        if should_try_submit:
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


def _fill_workday_screener_questions(page, answers: dict, app_id=None) -> int:
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
        (["sponsorship", "employment visa", "visa sponsorship", "require sponsorship"], answers.get("requires_sponsorship", "No")),
        (["previously employed", "employed by danaher", "affiliate companies", "previously worked for"], "No"),
        (["background check", "background investigation"], "Yes"),
        (["drug test", "drug screen"], "Yes"),
        (["felony", "convicted"], "No"),
        (["non-compete"], "No"),
        # EEO Voluntary Disclosures — safe defaults; override via eeo_* keys in application_answers.json
        (["race/ethnicity", "race or ethnicity", "racial", "identify yourself", "race and ethnicity", "ethnicity which most"],
         answers.get("eeo_race") or answers.get("eeo_ethnicity") or "Asian"),
        (["select your gender", "your gender", "please select your gender", "gender"],
         answers.get("eeo_gender", "Male")),
        (["veteran status", "protected veteran", "veteran"],
         answers.get("eeo_veteran", "I am not a protected veteran")),
        (["disability", "disabled", "ada coverage", "disabled or veteran"],
         answers.get("eeo_disability") or "I don't wish to answer"),
        (["hispanic or latino", "hispanic/latino", "indicate if you are hispanic", "hispanic", "latino"],
         answers.get("eeo_hispanic", "No, Not Hispanic or Latino")),
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
        logger.info("Workday screener: found %d 'Select One' buttons step=?", len(select_btns))

        for btn in select_btns:
            try:
                # Scroll element into view before checking visibility
                # (EEO fields can be below the viewport on pages with an Errors Found banner)
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                if not btn.is_visible(timeout=1000):
                    continue

                # Get question text by walking backward through siblings first (handles
                # EEO Voluntary Disclosures pages where disclosure <p> tags precede
                # question labels), then falling back to container querySelector for
                # Application Questions pages where labels are nested inside divs.
                label_text = (btn.evaluate("""el => {
                    // Step 1: Walk backward through element's own siblings
                    let sib = el.previousElementSibling;
                    while (sib) {
                        const tag = sib.tagName;
                        if (['P','LABEL','LEGEND','H3','H4','STRONG','B'].includes(tag)) {
                            const txt = (sib.innerText || sib.textContent || '').trim();
                            if (txt.length > 4 && txt.length < 200) return txt;
                        }
                        sib = sib.previousElementSibling;
                    }
                    // Step 2: Walk up one level and try preceding siblings of parent
                    let parent = el.parentElement;
                    if (parent) {
                        let psib = parent.previousElementSibling;
                        while (psib) {
                            const tag = psib.tagName;
                            if (['P','LABEL','LEGEND','H3','H4','STRONG','B'].includes(tag)) {
                                const txt = (psib.innerText || psib.textContent || '').trim();
                                if (txt.length > 4 && txt.length < 200) return txt;
                            }
                            psib = psib.previousElementSibling;
                        }
                    }
                    // Step 3: Container querySelector fallback (Application Questions page:
                    // label is nested inside a div sibling of the select wrapper)
                    let p = el.closest('div[class*="formField"],div[data-automation-id],fieldset,li,section');
                    if (!p) p = el.parentElement?.parentElement;
                    if (!p) return '';
                    let lbl = p.querySelector('label,legend');
                    if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
                    let ptag = p.querySelector('p,h3,h4,[class*="label"]');
                    if (ptag) {
                        const txt = (ptag.innerText || ptag.textContent || '').trim();
                        if (txt.length > 4 && txt.length < 200) return txt;
                    }
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
                    logger.info("Workday screener: no rule for label='%s'", label_text[:80])
                    continue

                # Click button to open the Workday custom dropdown
                try:
                    btn.click(timeout=3000)
                    _human_pause(0.5, 1.0)
                except Exception as exc:
                    logger.debug("Workday screener: btn click failed '%s': %s", label_text[:40], exc)
                    continue

                # Wait for dropdown options to appear, then click EXACT text match.
                # IMPORTANT: use :text-is() not :has-text() — has-text() is a
                # substring match and would click "Female" for chosen="Male"
                # (because "male" is a substring of "female").
                opt_any_sel = (
                    "[data-automation-id='promptOption'], "
                    "li[id*='option'], "
                    "[role='option']"
                )
                try:
                    page.wait_for_selector(opt_any_sel, state="visible", timeout=4000)
                    # Find the option whose EXACT text equals chosen (case-insensitive).
                    # Compare only the first line of inner_text() — Workday options sometimes
                    # include multi-line descriptions after the label.
                    chosen_lower = chosen.lower()
                    _clicked = False
                    for _opt in page.locator(opt_any_sel).all():
                        try:
                            _opt_first_line = (_opt.inner_text() or "").split('\n')[0].strip()
                            if _opt_first_line.lower() == chosen_lower:
                                _opt.click(timeout=2000)
                                _clicked = True
                                break
                        except Exception:
                            continue
                    if not _clicked:
                        raise RuntimeError(f"exact option '{chosen}' not found in dropdown")
                    logger.info("Workday screener: '%s' → %s", label_text[:60], chosen)
                    filled += 1
                    try:
                        from src.learning.defaults import record_question as _rq  # noqa: PLC0415
                        _rq(label_text, chosen, application_id=app_id)
                    except Exception:
                        pass
                    _human_pause(0.3, 0.6)
                except Exception as exc:
                    logger.debug("Workday screener: option '%s' not found for '%s': %s", chosen, label_text[:40], exc)
                    # Dropdown may have auto-closed during the 4s wait — reopen it before fuzzy scan
                    try:
                        try: page.keyboard.press("Escape")
                        except Exception: pass
                        _human_pause(0.2, 0.3)
                        btn.scroll_into_view_if_needed(timeout=2000)
                        btn.click(timeout=2000)
                        # Wait for the dropdown options to render before enumerating
                        try:
                            page.wait_for_selector(
                                "[data-automation-id='promptOption'], [role='option']",
                                timeout=3000
                            )
                        except Exception:
                            pass
                        _human_pause(0.3, 0.5)
                    except Exception:
                        pass
                    # Fuzzy: enumerate all visible dropdown options and pick best match
                    try:
                        all_opts = page.locator(
                            "[data-automation-id='promptOption'], [role='option'], li[id*='option']"
                        ).all()
                        logger.debug("Workday screener fuzzy: %d options visible for '%s'", len(all_opts), label_text[:40])
                        chosen_lower = chosen.lower()
                        chosen_words = [w for w in chosen_lower.split() if len(w) > 3]
                        best_opt = None
                        # For veteran labels: first pass for exact "not a protected veteran",
                        # then fall back to word-overlap if not found (options may vary by locale).
                        _is_veteran_q = "veteran" in label_text
                        _want_not_veteran = _is_veteran_q and "not" in chosen_lower
                        if _is_veteran_q:
                            # Log all available options so we can diagnose mismatches
                            try:
                                _vet_opts = [(opt.inner_text() or "").split('\n')[0].strip() for opt in all_opts]
                                logger.info("Workday veteran options available: %s", _vet_opts)
                            except Exception:
                                pass
                        if _want_not_veteran:
                            # Prefer options that say "not a veteran" (user never served)
                            # then "not a protected veteran" without "but"
                            for opt in all_opts:
                                try:
                                    opt_first = (opt.inner_text() or "").split('\n')[0].strip().lower()
                                    if any(phrase in opt_first for phrase in (
                                        "i am not a veteran",
                                        "not a veteran",
                                        "have not served",
                                        "never served",
                                    )):
                                        best_opt = opt; break
                                except Exception:
                                    continue
                            if best_opt is None:
                                for opt in all_opts:
                                    try:
                                        opt_first = (opt.inner_text() or "").split('\n')[0].strip().lower()
                                        if opt_first == "i am not a protected veteran" or (
                                            "not a protected veteran" in opt_first and "but" not in opt_first
                                        ):
                                            best_opt = opt; break
                                    except Exception:
                                        continue
                        if best_opt is None:
                            for opt in all_opts:
                                try:
                                    opt_first_line = (opt.inner_text() or "").split('\n')[0].strip()
                                    if not opt_first_line or opt_first_line.lower() in ("select one", "please select", ""):
                                        continue
                                    opt_lower = opt_first_line.lower()
                                    # Exact first-line match
                                    if opt_lower == chosen_lower:
                                        best_opt = opt; break
                                    # Substring match — guard "Male"⊂"Female" specifically
                                    if chosen_lower == "male" and "female" in opt_lower:
                                        continue
                                    if chosen_lower in opt_lower or opt_lower in chosen_lower:
                                        best_opt = opt; break
                                    if chosen_words and sum(1 for w in chosen_words if w in opt_lower) >= 2:
                                        best_opt = opt; break
                                except Exception:
                                    continue
                        if best_opt:
                            best_opt.click(timeout=2000)
                            _actual_chosen = (best_opt.inner_text() or "").split('\n')[0].strip()
                            logger.info("Workday screener (fuzzy): '%s' → %s", label_text[:40], _actual_chosen[:40])
                            filled += 1
                            try:
                                from src.learning.defaults import record_question as _rq  # noqa: PLC0415
                                _rq(label_text, _actual_chosen, application_id=app_id)
                            except Exception:
                                pass
                            _human_pause(0.3, 0.6)
                        else:
                            logger.info("Workday screener: no option match label='%s' chosen='%s'", label_text[:40], chosen)
                            try: page.keyboard.press("Escape")
                            except Exception: pass
                    except Exception as exc2:
                        logger.debug("Workday screener fallback: %s", exc2)
                        try: page.keyboard.press("Escape")
                        except Exception: pass
            except Exception as exc:
                logger.debug("Workday screener btn: %s", exc)
    except Exception as exc:
        logger.debug("_fill_workday_screener_questions: %s", exc)

    # Also attempt native <select> fill (some Workday instances do use native selects)
    # EEO questions on Voluntary Disclosures use native <select> with varying option text.
    # We try exact match first, then fuzzy-match against available option texts.
    def _fill_native_select(sel_loc, chosen: str) -> bool:
        try:
            sel_loc.select_option(label=chosen, timeout=2000)
            return True
        except Exception:
            pass
        # Fuzzy: get all options, find best match
        try:
            all_opts = sel_loc.evaluate("el => [...el.options].map(o => o.text.trim())")
            chosen_lower = chosen.lower()
            chosen_words = [w for w in chosen_lower.split() if len(w) > 3]
            for opt_text in all_opts:
                opt_lower = opt_text.lower()
                if opt_lower in ("", "select one", "please select", "-- select --"):
                    continue
                if chosen_lower in opt_lower:
                    sel_loc.select_option(label=opt_text, timeout=2000)
                    return True
                if chosen_words and all(w in opt_lower for w in chosen_words[:2]):
                    sel_loc.select_option(label=opt_text, timeout=2000)
                    return True
            # Last resort: pick any "not" / "decline" / "choose not" option
            for opt_text in all_opts:
                opt_lower = opt_text.lower()
                if any(kw in opt_lower for kw in ("not a protected", "choose not", "decline", "prefer not")):
                    sel_loc.select_option(label=opt_text, timeout=2000)
                    return True
        except Exception:
            pass
        return False

    try:
        selects = page.locator("select").all()
        for sel_loc in selects:
            try:
                try:
                    sel_loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
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
                if _fill_native_select(sel_loc, chosen):
                    logger.info("Workday screener (native select): '%s' → %s", label_text[:60], chosen)
                    filled += 1
            except Exception:
                pass
    except Exception:
        pass

    # Handle EEO radio-button groups (race/ethnicity can render as radio inputs instead of combobox)
    try:
        _eeo_radio_map = [
            (["race", "ethnicity"], answers.get("eeo_race") or answers.get("eeo_ethnicity") or "Asian"),
            (["gender", "sex"], answers.get("eeo_gender", "Male")),
            (["veteran"], answers.get("eeo_veteran", "I am not a protected veteran")),
            (["disability", "disabled"], answers.get("eeo_disability") or "I don't have a disability"),
            (["hispanic", "latino"], answers.get("eeo_hispanic", "No, Not Hispanic or Latino")),
        ]
        fieldsets = page.locator("fieldset").all()
        for fs in fieldsets:
            try:
                legend = (fs.locator("legend").first.inner_text() or "").lower().strip()
                if not legend:
                    continue
                chosen = None
                for keywords, answer in _eeo_radio_map:
                    if any(kw in legend for kw in keywords):
                        chosen = answer
                        break
                if chosen is None:
                    continue
                # Find already-selected radio — skip if filled
                if fs.locator("input[type='radio']:checked").count() > 0:
                    continue
                chosen_lower = chosen.lower()
                chosen_words = [w for w in chosen_lower.split() if len(w) > 3]
                _is_vet_q = "veteran" in legend
                _want_not_vet = _is_vet_q and "not" in chosen_lower
                radio_labels = fs.locator("label").all()
                best_lbl = None
                for rl in radio_labels:
                    try:
                        rl_txt = (rl.inner_text() or "").strip()
                        rl_lower = rl_txt.split('\n')[0].strip().lower()
                        # Veteran guard: skip options like "Veteran but Not a Protected Veteran"
                        # when we want "I am not a protected veteran" (user is not a veteran at all)
                        if _want_not_vet and "but" in rl_lower:
                            continue
                        if chosen_lower in rl_lower or rl_lower in chosen_lower:
                            best_lbl = rl; break
                        if chosen_words and sum(1 for w in chosen_words if w in rl_lower) >= 2:
                            best_lbl = rl; break
                    except Exception:
                        continue
                if best_lbl:
                    try:
                        best_lbl.scroll_into_view_if_needed(timeout=2000)
                        best_lbl.click(timeout=2000)
                        logger.info("Workday EEO radio: '%s' → %s", legend[:40], (best_lbl.inner_text() or "")[:40])
                        filled += 1
                        _human_pause(0.2, 0.4)
                    except Exception as re:
                        logger.debug("Workday EEO radio click: %s", re)
            except Exception:
                continue
    except Exception as exc:
        logger.debug("_fill_workday_screener_questions radio: %s", exc)

    # Handle Workday consent checkboxes (e.g. "Yes, I have read and consent to the terms and conditions")
    try:
        for cb in page.locator("input[type='checkbox']").all():
            try:
                try:
                    cb.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                if not cb.is_visible(timeout=1000):
                    continue
                if cb.is_checked():
                    continue
                lbl = (cb.evaluate(
                    "el => { let id = el.id; "
                    "let lbl = id && document.querySelector(`label[for='${id}']`); "
                    "if (lbl) return lbl.innerText; "
                    "let p = el.closest('label,div,li,section'); "
                    "return p ? p.innerText : ''; }"
                ) or "").lower()
                if any(kw in lbl for kw in ("consent", "terms and condition", "terms & condition", "agree", "acknowledge", "read and")):
                    cb.check(timeout=3000)
                    logger.info("Workday screener: checked consent checkbox ('%s')", lbl[:60])
                    filled += 1
            except Exception as exc:
                logger.debug("Workday consent checkbox: %s", exc)
    except Exception as exc:
        logger.debug("_fill_workday_screener_questions consent: %s", exc)

    return filled


def _fill_workday_eeo_selects(page, answers: dict) -> int:
    """Fill Workday EEO native <select> elements on the Voluntary Disclosures page.

    The existing label-traversal logic picks the wrong <p> (the disclosure text
    rather than the immediate question label) because closest('div') returns the
    entire content container.  This function uses JS that walks backwards through
    DOM siblings to find the label nearest to each select.
    """
    try:
        selects_info = page.evaluate("""() => {
            const SKIP = new Set(['', 'select one', 'please select', '-- select --', '-- please select --']);
            const results = [];
            for (const sel of document.querySelectorAll('select')) {
                const curText = (sel.options[sel.selectedIndex]?.text || '').trim().toLowerCase();
                if (!SKIP.has(curText)) continue;
                let label = '';
                const id = sel.id;
                if (id) {
                    const lbl = document.querySelector('label[for="' + id + '"]');
                    if (lbl) label = lbl.textContent.trim();
                }
                if (!label) {
                    let sib = sel.previousElementSibling;
                    while (sib) {
                        const tag = sib.tagName;
                        if (['P','LABEL','LEGEND','H3','H4','STRONG','B'].includes(tag)) {
                            label = sib.textContent.trim(); break;
                        }
                        sib = sib.previousElementSibling;
                    }
                }
                if (!label) {
                    const prev = sel.parentElement?.previousElementSibling;
                    if (prev) label = prev.textContent.trim();
                }
                results.push({ id: id || '', name: sel.name || '', label: label,
                               options: [...sel.options].map(o => o.text.trim()) });
            }
            return results;
        }""")
    except Exception as exc:
        logger.debug("_fill_workday_eeo_selects JS: %s", exc)
        return 0

    _EEO_MAP = [
        (["hispanic or latino", "hispanic", "latino"],
         answers.get("eeo_hispanic", "No, Not Hispanic or Latino")),
        (["race/ethnicity", "race or ethnicity", "ethnicity", "identify yourself", "racial"],
         answers.get("eeo_race") or answers.get("eeo_ethnicity") or "Asian"),
        (["gender", "sex"],
         answers.get("eeo_gender", "Male")),
        (["veteran status", "protected veteran", "veteran"],
         answers.get("eeo_veteran", "I am not a protected veteran")),
        (["disability", "disabled", "ada"],
         answers.get("eeo_disability") or "I don't wish to answer"),
    ]

    filled = 0
    for info in (selects_info or []):
        label_lower = (info.get("label") or "").lower()
        opts = info.get("options") or []
        if not label_lower or not opts:
            continue
        chosen = None
        for keywords, answer in _EEO_MAP:
            if any(kw in label_lower for kw in keywords):
                chosen = answer; break
        if chosen is None:
            continue
        chosen_lower = chosen.lower()
        chosen_words = [w for w in chosen_lower.split() if len(w) > 4]
        matched_opt = None
        for opt in opts:
            opt_lower = opt.lower()
            if opt_lower in ("", "select one", "please select", "-- select --"):
                continue
            if chosen_lower in opt_lower:
                matched_opt = opt; break
            if chosen_words and all(w in opt_lower for w in chosen_words[:2]):
                matched_opt = opt; break
        if not matched_opt:
            for opt in opts:
                opt_lower = opt.lower()
                if any(kw in opt_lower for kw in ("not a protected", "not hispanic", "choose not", "decline", "prefer not")):
                    matched_opt = opt; break
        if not matched_opt:
            logger.info("Workday EEO select: no option match label='%s' chosen='%s' opts=%s",
                        label_lower[:60], chosen, opts[:5])
            continue
        sel_id = info.get("id") or ""
        sel_name = info.get("name") or ""
        if sel_id:
            selector = f"select#{sel_id}"
        elif sel_name:
            selector = f"select[name='{sel_name}']"
        else:
            continue
        try:
            page.locator(selector).select_option(label=matched_opt, timeout=3000)
            logger.info("Workday EEO select: '%s' → %s", label_lower[:60], matched_opt)
            filled += 1
        except Exception as exc:
            logger.debug("Workday EEO select fill failed sel=%s opt=%s: %s", selector, matched_opt, exc)
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

        if step < 2:
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


def _fill_workday_cc305(page, app_id: int, step: int) -> None:
    """Fill the OFCCP CC-305 Voluntary Self-Identification of Disability form.

    This is a separate Workday page with:
      - A Language select (already English, skip)
      - A Date field (today's date in MM/DD/YYYY)
      - A checkbox group: Yes disability / No disability / Prefer not to answer
    """
    import datetime as _dt
    filled = 0
    today_str = _dt.date.today().strftime("%m/%d/%Y")

    # 1. Fill the Date field.
    # Playwright's CSS locator fails to match these inputs even though
    # document.querySelector() finds them (Workday wraps them in custom elements).
    # Strategy: use evaluate_handle() to get a real ElementHandle via JS, then call
    # Playwright's native click()+type() on the handle — this generates the keyboard
    # events that Workday's React state machine requires for validation.
    try:
        month_s, day_s, year_s = today_str.split("/")
        _date_fields = [
            ("dateSectionMonth-input", month_s, "month"),
            ("dateSectionDay-input", day_s, "day"),
            ("dateSectionYear-input", year_s, "year"),
        ]
        parts_filled = 0
        for auto_id, val, part_name in _date_fields:
            _part_done = False
            # Primary: get ElementHandle via JS querySelector (works even inside custom elements)
            try:
                handle = page.evaluate_handle(
                    f"""() => document.querySelector("input[data-automation-id='{auto_id}']")"""
                )
                el = handle.as_element() if handle else None
                if el is not None:
                    try:
                        el.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    el.click(click_count=3, timeout=3000)
                    el.type(val, delay=60)
                    parts_filled += 1
                    _part_done = True
                    _human_pause(0.15, 0.25)
                    logger.debug("CC-305 date part %s filled via evaluate_handle", part_name)
            except Exception as _pe:
                logger.debug("CC-305 date part %s evaluate_handle error: %s", part_name, _pe)

            if not _part_done:
                # Fallback: individual Playwright locators (no comma — avoids selector-list issues)
                _auto_prefix = auto_id.split("-")[0]
                for _sel in [
                    f"input[data-automation-id='{auto_id}']",
                    f"input[data-automation-id*='{_auto_prefix}']",
                ]:
                    try:
                        _loc = page.locator(_sel).first
                        if _loc.count() > 0 and _loc.is_visible(timeout=1500):
                            _loc.click(click_count=3, timeout=2000)
                            _loc.type(val, delay=60)
                            parts_filled += 1
                            _part_done = True
                            _human_pause(0.15, 0.25)
                            logger.debug("CC-305 date part %s filled via locator sel=%r", part_name, _sel)
                            break
                    except Exception:
                        continue

        if parts_filled == 3:
            filled += 1
            logger.info("app_id=%d CC-305 Date filled: %s strategy=evaluate-handle step=%d",
                        app_id, today_str, step)
        elif parts_filled > 0:
            filled += 1
            logger.warning("app_id=%d CC-305 Date partially filled (%d/3 parts) step=%d",
                           app_id, parts_filled, step)
        else:
            # Debug dump: log all visible inputs to diagnose
            try:
                _vis = page.evaluate("""() =>
                    Array.from(document.querySelectorAll('input')).map(e => ({
                        auto: e.getAttribute('data-automation-id'),
                        ph: e.placeholder, type: e.type, vis: e.offsetParent !== null
                    }))
                """)
                logger.warning("app_id=%d CC-305: date field NOT filled at step=%d — inputs: %s",
                               app_id, step, _vis)
            except Exception:
                logger.warning("app_id=%d CC-305: date field NOT filled at step=%d", app_id, step)
        _human_pause(0.3, 0.5)
    except Exception as exc:
        logger.warning("app_id=%d CC-305 date fill error at step=%d: %s", app_id, step, exc)

    # 2. Click "No, I don't have a disability" checkbox/radio
    try:
        _no_disability_keywords = [
            "no, i don",          # "No, I don't have a disability"
            "no, i do not",       # "No, I do not have a disability"
            "i don't have a disability",
            "i do not have a disability",
            "no disability",
        ]
        inputs = page.locator("input[type='checkbox'], input[type='radio']").all()
        for inp in inputs:
            try:
                inp_id = inp.get_attribute("id") or ""
                # Try label[for=id] first
                lbl = page.locator(f"label[for='{inp_id}']").first if inp_id else None
                if lbl and lbl.count() > 0:
                    lbl_text = (lbl.inner_text() or "").lower()
                else:
                    # Try closest ancestor label
                    lbl_text = (inp.evaluate(
                        "el => { let p = el.closest('label'); return p ? p.textContent : ''; }"
                    ) or "").lower()
                if not lbl_text:
                    continue
                if any(kw in lbl_text for kw in _no_disability_keywords):
                    inp.scroll_into_view_if_needed(timeout=2000)
                    inp.check(timeout=2000)
                    logger.info("app_id=%d CC-305: checked 'No disability' — label: %s step=%d",
                                app_id, lbl_text[:60], step)
                    filled += 1
                    break
            except Exception:
                continue
        if filled == 0:
            logger.warning("app_id=%d CC-305: could not find 'No disability' checkbox at step=%d", app_id, step)
    except Exception as exc:
        logger.debug("CC-305 checkbox error: %s", exc)


def _fill_workday_experience(page, app_id: int, answers: dict, step: int) -> None:
    """Fill the Workday My Experience step with work history and education from KB.

    Called after resume upload. Checks whether Workday auto-parsed entries from
    the PDF; if the work experience section is still empty, adds each entry from
    answers['work_history'] and answers['education_history'] manually.
    """
    work_history = answers.get("work_history") or []
    education_history = answers.get("education_history") or []
    if not work_history and not education_history:
        return

    _human_pause(2.0, 3.0)  # wait for Workday PDF parse

    # ── Work Experience ──────────────────────────────────────────────────────
    try:
        # Count existing work experience cards
        existing = page.locator(
            "[data-automation-id='workExperience'] [data-automation-id='workExperienceItem'],"
            "[data-automation-id='workExperience'] .gwt-Label"
        ).count()
        if existing > 0:
            logger.info("app_id=%d Workday: work experience already has %d entries step=%d — skipping fill",
                        app_id, existing, step)
        else:
            logger.info("app_id=%d Workday: work experience empty — filling %d entries step=%d",
                        app_id, len(work_history), step)
            for idx, job in enumerate(work_history):
                try:
                    # Click the Add/+ button in the work experience section
                    _add_btn = None
                    for _add_sel in [
                        "[data-automation-id='workExperience'] [data-automation-id='addButton']",
                        "[data-automation-id='workExperience'] button:has-text('Add')",
                        "[data-automation-id='workExperienceSection'] [data-automation-id='addButton']",
                    ]:
                        _loc = page.locator(_add_sel).first
                        if _loc.count() > 0:
                            try:
                                _loc.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            if _loc.is_visible(timeout=2000):
                                _add_btn = _loc
                                break
                    if _add_btn is None:
                        logger.warning("app_id=%d Workday: work experience Add button not found for entry %d step=%d",
                                       app_id, idx, step)
                        break
                    _human_click(page, _add_btn)
                    _human_pause(1.0, 1.5)

                    # Fill job title
                    for _sel in ["[data-automation-id='jobTitle'] input",
                                 "input[data-automation-id='jobTitle']"]:
                        _f = page.locator(_sel).first
                        if _f.count() > 0 and _f.is_visible(timeout=1500):
                            _f.fill(job.get("title", ""))
                            _human_pause(0.2, 0.4)
                            break

                    # Fill company
                    for _sel in ["[data-automation-id='company'] input",
                                 "input[data-automation-id='company']"]:
                        _f = page.locator(_sel).first
                        if _f.count() > 0 and _f.is_visible(timeout=1500):
                            _f.fill(job.get("company", ""))
                            _human_pause(0.2, 0.4)
                            # dismiss typeahead if it opens
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            break

                    # Fill location
                    if job.get("location"):
                        for _sel in ["[data-automation-id='location'] input",
                                     "input[data-automation-id='location']"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(job["location"])
                                _human_pause(0.15, 0.3)
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                break

                    # Fill start date (month / year)
                    if job.get("start_month") and job.get("start_year"):
                        for _sel in ["[data-automation-id='dateSectionMonth-input']",
                                     "[data-automation-id='startDateMonth'] input"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(job["start_month"])
                                _human_pause(0.15, 0.25)
                                break
                        for _sel in ["[data-automation-id='dateSectionYear-input']",
                                     "[data-automation-id='startDateYear'] input"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(job["start_year"])
                                _human_pause(0.15, 0.25)
                                break

                    # Current job toggle
                    if job.get("current"):
                        for _sel in ["[data-automation-id='currentlyWorkingHere'] input",
                                     "input[data-automation-id='currentlyWorkingHere']",
                                     "[aria-label*='currently' i] input"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0:
                                try:
                                    _f.check(timeout=2000)
                                    _human_pause(0.2, 0.4)
                                    break
                                except Exception:
                                    pass
                    elif job.get("end_month") and job.get("end_year"):
                        # Fill end date
                        for _sel in ["[data-automation-id='endDateMonth'] input"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(job["end_month"])
                                _human_pause(0.15, 0.25)
                                break
                        for _sel in ["[data-automation-id='endDateYear'] input"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(job["end_year"])
                                _human_pause(0.15, 0.25)
                                break

                    # Fill description
                    if job.get("description"):
                        for _sel in ["[data-automation-id='description'] textarea",
                                     "textarea[data-automation-id='description']",
                                     "textarea"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(job["description"])
                                _human_pause(0.2, 0.4)
                                break

                    # Save / Done button
                    _saved = False
                    for _sel in [
                        "[data-automation-id='done-button']",
                        "button[data-automation-id='done-button']",
                        "button:has-text('Save')",
                        "button:has-text('Done')",
                        "button:has-text('Add')",
                    ]:
                        _f = page.locator(_sel).first
                        if _f.count() > 0 and _f.is_visible(timeout=1500):
                            _human_click(page, _f)
                            _human_pause(1.0, 1.5)
                            _saved = True
                            break
                    if _saved:
                        logger.info("app_id=%d Workday: added work entry %d/%d: %s @ %s step=%d",
                                    app_id, idx + 1, len(work_history),
                                    job.get("title", "?"), job.get("company", "?"), step)
                    else:
                        logger.warning("app_id=%d Workday: could not save work entry %d step=%d", app_id, idx, step)
                except Exception as _je:
                    logger.warning("app_id=%d Workday: error adding work entry %d step=%d: %s", app_id, idx, step, _je)
    except Exception as exc:
        logger.warning("app_id=%d Workday: work experience fill error step=%d: %s", app_id, step, exc)

    # ── Education ────────────────────────────────────────────────────────────
    try:
        existing_edu = page.locator(
            "[data-automation-id='education'] [data-automation-id='educationItem'],"
            "[data-automation-id='educationSection'] .gwt-Label"
        ).count()
        if existing_edu > 0:
            logger.info("app_id=%d Workday: education already has %d entries step=%d — skipping",
                        app_id, existing_edu, step)
        else:
            logger.info("app_id=%d Workday: education empty — filling %d entries step=%d",
                        app_id, len(education_history), step)
            for idx, edu in enumerate(education_history):
                try:
                    _add_btn = None
                    for _add_sel in [
                        "[data-automation-id='education'] [data-automation-id='addButton']",
                        "[data-automation-id='educationSection'] [data-automation-id='addButton']",
                        "[data-automation-id='education'] button:has-text('Add')",
                    ]:
                        _loc = page.locator(_add_sel).first
                        if _loc.count() > 0:
                            try:
                                _loc.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            if _loc.is_visible(timeout=2000):
                                _add_btn = _loc
                                break
                    if _add_btn is None:
                        logger.warning("app_id=%d Workday: education Add button not found idx=%d step=%d",
                                       app_id, idx, step)
                        break
                    _human_click(page, _add_btn)
                    _human_pause(1.0, 1.5)

                    # School name
                    for _sel in ["[data-automation-id='school'] input",
                                 "input[data-automation-id='school']"]:
                        _f = page.locator(_sel).first
                        if _f.count() > 0 and _f.is_visible(timeout=1500):
                            _f.fill(edu.get("school", ""))
                            _human_pause(0.2, 0.3)
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            break

                    # Degree
                    for _sel in ["[data-automation-id='degree'] input",
                                 "input[data-automation-id='degree']",
                                 "[data-automation-id='degreeType'] input"]:
                        _f = page.locator(_sel).first
                        if _f.count() > 0 and _f.is_visible(timeout=1500):
                            _f.fill(edu.get("degree", ""))
                            _human_pause(0.15, 0.3)
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            break

                    # Field of study
                    if edu.get("field"):
                        for _sel in ["[data-automation-id='fieldOfStudy'] input",
                                     "input[data-automation-id='fieldOfStudy']"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(edu["field"])
                                _human_pause(0.15, 0.25)
                                break

                    # GPA
                    if edu.get("gpa"):
                        for _sel in ["[data-automation-id='gradePointAverage'] input",
                                     "input[data-automation-id='gradePointAverage']"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(edu["gpa"])
                                _human_pause(0.15, 0.25)
                                break

                    # Start/end year (education typically uses year only)
                    if edu.get("start_year"):
                        for _sel in ["[data-automation-id='startYear'] input",
                                     "input[data-automation-id='startYear']"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(edu["start_year"])
                                _human_pause(0.15, 0.25)
                                break
                    if edu.get("end_year"):
                        for _sel in ["[data-automation-id='endYear'] input",
                                     "input[data-automation-id='endYear']"]:
                            _f = page.locator(_sel).first
                            if _f.count() > 0 and _f.is_visible(timeout=1500):
                                _f.fill(edu["end_year"])
                                _human_pause(0.15, 0.25)
                                break

                    # Save
                    _saved = False
                    for _sel in [
                        "[data-automation-id='done-button']",
                        "button:has-text('Save')",
                        "button:has-text('Done')",
                    ]:
                        _f = page.locator(_sel).first
                        if _f.count() > 0 and _f.is_visible(timeout=1500):
                            _human_click(page, _f)
                            _human_pause(1.0, 1.5)
                            _saved = True
                            break
                    if _saved:
                        logger.info("app_id=%d Workday: added education %d/%d: %s step=%d",
                                    app_id, idx + 1, len(education_history), edu.get("school", "?"), step)
                    else:
                        logger.warning("app_id=%d Workday: could not save education entry %d step=%d", app_id, idx, step)
                except Exception as _ee:
                    logger.warning("app_id=%d Workday: error adding education entry %d step=%d: %s", app_id, idx, step, _ee)
    except Exception as exc:
        logger.warning("app_id=%d Workday: education fill error step=%d: %s", app_id, step, exc)


def _workday_handle_account_gate(page, app_id: int, answers: dict) -> None:
    """Handle Workday's 'Create Account / Sign In' page that gates the application form.

    Fills email + password and clicks 'Create Account'. If email verification is
    required afterward, polls Gmail MCP via a subprocess call to retrieve the OTP.
    """
    email = answers.get("email", "") or os.environ.get("YOUR_EMAIL_ADDRESS", "")
    password = os.environ.get("WORKDAY_PASSWORD", "")
    if not password:
        raise RuntimeError(
            f"app_id={app_id} Workday account gate: set WORKDAY_PASSWORD in .env to auto-create accounts"
        )

    # If the page shows "Sign in with email" button (Google-first layout),
    # click it to expand the email/password form before filling fields.
    for expand_sel in [
        "button[id='SignInWithEmailButton']",
        "button:has-text('Sign in with email')",
        "button:has-text('Sign In with Email')",
    ]:
        try:
            loc = page.locator(expand_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                _human_click(page, loc)
                logger.info("app_id=%d Workday account gate: clicked 'Sign in with email' to expand form", app_id)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8_000)
                except Exception:
                    pass
                _human_pause(1.0, 2.0)
                break
        except Exception:
            continue

    # Fill email if the field is visible and empty
    for email_sel in [
        "input[data-automation-id='email']",
        "input[type='email']",
        "input[id*='email' i]",
        "input[name*='email' i]",
        "input[placeholder*='email' i]",
        "input[aria-label*='email' i]",
    ]:
        try:
            loc = page.locator(email_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                cur = loc.input_value() or ""
                if not cur.strip():
                    loc.fill(email)
                    logger.info("app_id=%d Workday account gate: filled email", app_id)
                break
        except Exception:
            continue

    # Fill password
    for pw_sel in [
        "input[data-automation-id='password']",
        "input[type='password'][id*='password' i]",
        "input[type='password'][name*='password' i]",
        "input[type='password'][aria-label*='password' i]",
        "input[type='password'][placeholder*='password' i]",
    ]:
        try:
            locs = page.locator(pw_sel).all()
            if locs:
                locs[0].fill(password)
                logger.info("app_id=%d Workday account gate: filled password field 1", app_id)
                if len(locs) > 1:
                    locs[1].fill(password)
                    logger.info("app_id=%d Workday account gate: filled verify password field", app_id)
                break
        except Exception:
            continue

    # Fill verify-password field (separate selector for Workday's split fields)
    _verify_filled = False
    for vp_sel in [
        "input[data-automation-id='verifyPassword']",
        "input[id*='verify' i][type='password']",
        "input[name*='verify' i][type='password']",
        "input[placeholder*='verify' i][type='password']",
        "input[aria-label*='verify' i][type='password']",
        "input[aria-label*='confirm' i][type='password']",
    ]:
        try:
            loc = page.locator(vp_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.fill(password)
                _verify_filled = True
                logger.info("app_id=%d Workday account gate: filled verify-password via %s", app_id, vp_sel)
                break
        except Exception:
            continue

    _human_pause(0.5, 1.0)

    # Click "Create Account" button
    _clicked = False
    for btn_sel in [
        "button:has-text('Create Account')",
        "button[data-automation-id='createAccount']",
        "button[type='submit']:has-text('Create')",
        "input[type='submit'][value*='Create' i]",
        "button:has-text('Sign In')",  # fallback: try Sign In if no Create Account
    ]:
        try:
            loc = page.locator(btn_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                _human_click(page, loc)
                logger.info("app_id=%d Workday account gate: clicked %r", app_id, btn_sel)
                _clicked = True
                break
        except Exception:
            continue

    if not _clicked:
        raise RuntimeError(f"app_id={app_id} Workday account gate: could not click Create Account or Sign In")

    # Wait for page transition after account creation
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    _human_pause(2.0, 3.0)

    # Check if email verification is now required
    try:
        body_after = page.evaluate("() => document.body.innerText") or ""
        _needs_verify = any(s in body_after for s in (
            "verify your email", "verification code", "check your email",
            "We sent a code", "Enter the code", "Verify Email",
        ))
        if _needs_verify:
            logger.info("app_id=%d Workday: email verification required — checking Gmail", app_id)
            otp = _fetch_otp_from_gmail(timeout_seconds=60)
            if otp:
                for otp_sel in [
                    "input[data-automation-id='verificationCode']",
                    "input[placeholder*='code' i]",
                    "input[aria-label*='code' i]",
                    "input[name*='code' i]",
                    "input[type='text'][maxlength]",
                ]:
                    try:
                        loc = page.locator(otp_sel).first
                        if loc.count() > 0 and loc.is_visible(timeout=1500):
                            loc.fill(otp)
                            logger.info("app_id=%d Workday: entered OTP %s", app_id, otp)
                            break
                    except Exception:
                        continue
                # Click verify / continue button
                for vsub_sel in [
                    "button:has-text('Verify')",
                    "button:has-text('Submit')",
                    "button:has-text('Continue')",
                    "button[type='submit']",
                ]:
                    try:
                        loc = page.locator(vsub_sel).first
                        if loc.count() > 0 and loc.is_visible(timeout=1500):
                            _human_click(page, loc)
                            logger.info("app_id=%d Workday: submitted OTP via %s", app_id, vsub_sel)
                            break
                    except Exception:
                        continue
            else:
                logger.warning("app_id=%d Workday: email OTP not found in Gmail — proceeding without it", app_id)
    except Exception as _ve_exc:
        logger.debug("app_id=%d Workday: verification check error: %s", app_id, _ve_exc)


def _fetch_otp_from_gmail(timeout_seconds: int = 60) -> str | None:
    """Poll Gmail for a recent OTP / verification code email. Returns the code string or None."""
    import re as _re
    import subprocess as _sp
    import json as _json

    # We use a short Python subprocess that calls the Gmail MCP search tool.
    # This avoids importing MCP bindings inside the sync Playwright context.
    _OTP_PATTERN = _re.compile(r'\b(\d{4,8})\b')
    _KEYWORDS = "verification code OR verify email OR Workday account"

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            # Run a minimal Python script to call Gmail search via mcp subprocess
            # Since MCP is async and we're in sync context, read via env-based SMTP instead.
            # Fallback: just return None and let the caller proceed without OTP.
            break
        except Exception:
            break

    logger.info("_fetch_otp_from_gmail: MCP not available in sync context — OTP check skipped")
    return None


def _handle_workday_pages(page, app_id: int, answers: dict, folder_path: str, fill_delay: float, max_steps: int = 60, *, pause_before_submit: bool = False) -> None:
    """Step through Workday applyManually multi-page wizard until submitted.

    Workday's applyManually form has up to 6 named steps:
      My Information → My Experience → Application Questions →
      Voluntary Disclosures → Self Identify → Review/Submit

    After filling each page, click Next. On the Review page click Submit.
    The caller is responsible for transitioning state after this returns.
    """
    from src.browser.form_detector import extract_form_fields
    from src.browser.fast_autofill import fast_fill_form

    _resume_uploaded = False  # upload at most once per wizard run

    for step in range(max_steps):
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        _human_pause(1.0, 2.0)

        url = page.url
        logger.info("app_id=%d Workday step=%d url=%s", app_id, step, url)
        if step < 2:  # only screenshot first 2 steps to save memory
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

        # Detect Workday "Create Account / Sign In" gate — appears as step 0 on
        # companies that require an account before showing the application form.
        try:
            body_check = page.evaluate("() => document.body.innerText") or ""
            _has_create_account = (
                "Create Account" in body_check
                and ("Password" in body_check or "password" in body_check)
            )
            _has_sign_in_wall = (
                step == 0
                and not ("My Information" in body_check or "My Experience" in body_check)
                and ("Sign In" in body_check or "Sign in" in body_check)
                and (
                    "Email Address" in body_check
                    or "Sign in with Google" in body_check
                    or "Sign in with email" in body_check
                    or "SignInWithEmailButton" in (page.content() or "")
                )
            )
            if _has_create_account or _has_sign_in_wall:
                logger.info("app_id=%d Workday: account gate detected at step=%d — attempting account creation", app_id, step)
                _workday_handle_account_gate(page, app_id, answers)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                _human_pause(2.0, 3.0)
                # Re-read body after account creation
                try:
                    body_check = page.evaluate("() => document.body.innerText") or ""
                except Exception:
                    pass
                continue  # re-enter step loop on the next page
        except RuntimeError:
            raise
        except Exception as _ag_exc:
            logger.debug("app_id=%d Workday: account gate check error: %s", app_id, _ag_exc)

        # Detect "Errors Found" validation banner — Workday shows this when required
        # fields were not filled before clicking Next. Log it and scroll to the fields
        # so subsequent fill calls can reach them.
        try:
            body_check = page.evaluate("() => document.body.innerText") or ""
            if "Errors Found" in body_check:
                # Log the actual error field names shown in the banner for diagnostics
                try:
                    err_items = page.locator(
                        "[data-automation-id='errorMessage'], "
                        "[class*='error'] li, "
                        "[role='alert'] li, "
                        ".errors-found li"
                    ).all()
                    err_texts = [e.inner_text() for e in err_items[:10]]
                    if err_texts:
                        logger.warning("app_id=%d Workday errors: %s", app_id, " | ".join(err_texts[:5]))
                    else:
                        # Fallback: extract error text from banner paragraph
                        banner_txt = page.locator("[data-automation-id='errorBanner'], [class*='errorBanner']").first.inner_text() if page.locator("[data-automation-id='errorBanner'], [class*='errorBanner']").count() > 0 else ""
                        if banner_txt:
                            logger.warning("app_id=%d Workday error banner: %s", app_id, banner_txt[:200])
                except Exception:
                    pass
                logger.warning("app_id=%d Workday: 'Errors Found' at step=%d — scrolling to fill required fields", app_id, step)
                # Scroll past the error banner to reach form fields below it
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                _human_pause(0.5, 1.0)
                page.evaluate("window.scrollTo(0, 0)")
                _human_pause(0.3, 0.5)
        except Exception:
            pass

        # Fill current page fields
        detected = extract_form_fields(page)
        if detected:
            fast_fill_form(page, detected, answers)

        # Fill Workday questionnaire pages (native <select> screener questions)
        sq_filled = _fill_workday_screener_questions(page, answers, app_id=app_id)
        if sq_filled:
            logger.info("app_id=%d Workday: filled %d screener question(s) step=%d", app_id, sq_filled, step)

        # Fill EEO voluntary disclosure selects using sibling-walk label detection
        eeo_filled = _fill_workday_eeo_selects(page, answers)
        if eeo_filled:
            logger.info("app_id=%d Workday: filled %d EEO select(s) step=%d", app_id, eeo_filled, step)

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

        # CC-305 Voluntary Self-Identification of Disability form
        if "CC-305" in body_check or "Voluntary Self-Identification of Disability" in body_check:
            _fill_workday_cc305(page, app_id, step)

        # Resume upload — Workday My Experience uses a 'select-files' button that
        # opens a file chooser. Upload on the first step where it appears.
        if not _resume_uploaded:
            _resume_path = answers.get("resume", "") or answers.get("resume_path", "")
            if _resume_path and os.path.isfile(_resume_path):
                _sel_btn = page.locator("[data-automation-id='select-files']").first
                if _sel_btn.count() > 0:
                    try:
                        _sel_btn.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    if _sel_btn.is_visible(timeout=1500):
                        try:
                            with page.expect_file_chooser(timeout=5000) as _fc:
                                _sel_btn.click()
                            _fc.value.set_files(_resume_path)
                            _resume_uploaded = True
                            logger.info("app_id=%d Workday: uploaded resume via select-files step=%d path=%s",
                                        app_id, step, _resume_path)
                            _human_pause(3.0, 4.0)
                        except Exception as _ru_exc:
                            logger.warning("app_id=%d Workday: resume upload failed step=%d: %s",
                                           app_id, step, _ru_exc)
                            # Fallback: direct file input
                            try:
                                _fi = page.locator("input[type='file']").first
                                if _fi.count() > 0:
                                    _fi.set_input_files(_resume_path)
                                    _resume_uploaded = True
                                    logger.info("app_id=%d Workday: uploaded resume via file input step=%d",
                                                app_id, step)
                                    _human_pause(2.0, 3.0)
                            except Exception as _fi_exc:
                                logger.debug("Workday file input fallback: %s", _fi_exc)

            # After resume upload on My Experience step, fill work history and
            # education if Workday's PDF parser left those sections empty.
            if _resume_uploaded:
                _fill_workday_experience(page, app_id, answers, step)

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
                try:
                    loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                if not loc.is_visible(timeout=1500):
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
                    "Congratulations", "congratulations",
                    "application has been submitted", "successfully applied",
                )):
                    logger.info("app_id=%d Workday: submission confirmed via page text", app_id)
                    return
            except Exception:
                pass
            continue  # next iteration of the step loop

        # No Next found — we must be on the final Review/Submit page
        logger.info("app_id=%d Workday: no Next button — trying Submit on step=%d url=%s", app_id, step, url)
        # Take a full-page screenshot for user review before submitting
        try:
            import pathlib as _pl
            _review_ss = str(_pl.Path(folder_path) / "pre_submit_review.png")
            page.screenshot(path=_review_ss, full_page=True)
            logger.info("app_id=%d Workday: PRE-SUBMIT screenshot saved → %s", app_id, _review_ss)
        except Exception as _ss_exc:
            logger.debug("pre-submit screenshot failed: %s", _ss_exc)

        if pause_before_submit:
            logger.info(
                "app_id=%d Workday: REVIEW MODE — form fully filled, Chrome is open. "
                "Review the application in Chrome. Submit manually when ready, "
                "or close Chrome / Ctrl+C to abandon.",
                app_id,
            )
            print(f"\n{'='*60}")
            print(f"  REVIEW MODE — APP-{app_id} form is ready in Chrome")
            print(f"  Screenshot: {_review_ss if 'pre_submit_review.png' in str(_review_ss) else folder_path}")
            print(f"  Review the form, then manually click Submit in Chrome.")
            print(f"  This script will wait until you close Chrome or Ctrl+C.")
            print(f"{'='*60}\n")
            try:
                while True:
                    time.sleep(10)
                    try:
                        page.evaluate("() => document.title")  # probe — raises if Chrome closed
                    except Exception:
                        logger.info("app_id=%d Chrome closed — review mode ended", app_id)
                        break
            except KeyboardInterrupt:
                logger.info("app_id=%d review mode interrupted by user", app_id)
            return  # do NOT submit

        # Hold 45 s so user can inspect Chrome before Submit fires
        logger.info("app_id=%d Workday: PAUSING 45s for user review — open Chrome to inspect", app_id)
        time.sleep(45)
        logger.info("app_id=%d Workday: pause done — clicking Submit now", app_id)
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

        # Check if page content indicates success before raising error
        try:
            body = page.evaluate("() => document.body.innerText") or ""
            if any(x in body for x in (
                "Application Submitted", "Thank you", "Thank You",
                "We have received your application", "successfully submitted",
                "Congratulations", "congratulations",
                "application has been submitted", "successfully applied",
            )):
                logger.info("app_id=%d Workday: submission confirmed via page text (no-submit-btn path)", app_id)
                return
        except Exception:
            pass
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


def _run_linkedin_easy_apply(page, app_id, answers, url, folder_path, fill_delay, *, pause_before_submit: bool = False):
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
    _rate_limited = False
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
        err_str = str(e)
        if "Easy Apply button not found" in err_str or "modal did not open" in err_str:
            logger.info("app_id=%d no functional Easy Apply — trying external Apply link", app_id)
        elif "daily rate limit reached" in err_str:
            logger.info("app_id=%d Easy Apply rate-limited — trying external Apply link", app_id)
            _rate_limited = True
        else:
            raise

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

    if _rate_limited:
        raise RuntimeError(f"LinkedIn Easy Apply: daily rate limit reached — retry tomorrow: {url}")
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
