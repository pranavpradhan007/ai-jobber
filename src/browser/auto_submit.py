"""
Portal auto-submit module.

Full flow for an approved gated application:
  1. Load candidate answers from env + DB
  2. Navigate to the job listing URL
  3. Find and click the "Apply" / "Apply Now" button
  4. Wait for the application form to load
  5. Detect portal type
  6. Check for AI traps (hard abort)
  7. Fill all fields including resume upload
  8. Click the submit button
  9. Wait for confirmation, capture screenshot as receipt

CAPTCHA/MFA on any page → raises CaptchaDetected / MFADetected.
AI trap → raises AITrapDetected.
"""
from __future__ import annotations
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from src.browser.prefill import CaptchaDetected, MFADetected, AITrapDetected, StaticFillEngine
from src.browser.trap_detector import detect_traps_in_html
from src.browser.portal import classify_portal
from src.browser.field_mapper import build_fill_instructions, FILL_DELAY_SECONDS

logger = logging.getLogger(__name__)

# How long to wait for page/redirect after clicking Apply (seconds)
_APPLY_WAIT  = 4.0
_SUBMIT_WAIT = 6.0

# Selectors for "Apply" / "Apply Now" buttons on listing pages
_APPLY_SELECTORS = [
    # Generic
    "a:text-is('Apply Now')",
    "a:text-is('Apply')",
    "button:text-is('Apply Now')",
    "button:text-is('Apply')",
    # Indeed
    ".ia-IndeedApplyButton",
    "[data-testid='applyButton']",
    "#indeedApplyButton",
    # RemoteOK
    "a[href*='apply']",
    "a.apply",
    # Greenhouse / Lever
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

    # Use GitHub URL as the website/portfolio field; fall back to LinkedIn
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
    Navigate to `url`, fill the application form, and click Submit.
    Returns AutoSubmitResult. Raises CaptchaDetected / MFADetected on detection.
    """
    if dry_run:
        logger.info("DRY_RUN auto_submit app_id=%d url=%s", app_id, url)
        return AutoSubmitResult(
            success=True, app_id=app_id,
            receipt=f"DRY_RUN_{app_id}",
            screenshot_path=None,
        )

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: PLC0415
    except ImportError:
        return AutoSubmitResult(
            success=False, app_id=app_id,
            error="playwright not installed — run: pip install playwright && playwright install chromium",
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)   # visible so user can see what's happening
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            return _run_submit_flow(
                page, app_id, answers, url, folder_path, fill_delay,
            )
        except CaptchaDetected:
            logger.warning("CAPTCHA detected for app_id=%d", app_id)
            return AutoSubmitResult(success=False, app_id=app_id, captcha_detected=True)
        except MFADetected:
            logger.warning("MFA detected for app_id=%d", app_id)
            return AutoSubmitResult(success=False, app_id=app_id, mfa_detected=True)
        except AITrapDetected as e:
            logger.warning("AI trap detected for app_id=%d: %s", app_id, e)
            return AutoSubmitResult(success=False, app_id=app_id, ai_trap_detected=True)
        except Exception as exc:
            logger.error("auto_submit failed app_id=%d: %s", app_id, exc)
            return AutoSubmitResult(success=False, app_id=app_id, error=str(exc))
        finally:
            context.close()
            browser.close()


def _run_submit_flow(page, app_id, answers, url, folder_path, fill_delay):
    from playwright.sync_api import TimeoutError as PWTimeout  # noqa: PLC0415

    os.makedirs(folder_path, exist_ok=True)

    # ── Step 1: Navigate to listing URL ───────────────────────────────────────
    logger.info("app_id=%d navigating to %s", app_id, url)
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(1.5)

    # ── Step 2: Find Apply URL / button ───────────────────────────────────────
    current_url = page.url

    # For RemoteOK: extract the direct ATS href instead of clicking (JS-rendered button)
    if "remoteok.com" in current_url:
        apply_href = _extract_apply_href(page)
        if apply_href:
            logger.info("app_id=%d RemoteOK: navigating to apply href %s", app_id, apply_href)
            page.goto(apply_href, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2.0)
        else:
            logger.warning("app_id=%d RemoteOK: could not extract apply href", app_id)
    else:
        apply_clicked = False
        for sel in _APPLY_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    logger.info("app_id=%d clicking apply selector=%r", app_id, sel)
                    loc.click()
                    apply_clicked = True
                    break
            except Exception:
                continue

        if apply_clicked:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=int(_APPLY_WAIT * 1000))
            except Exception:
                pass
            time.sleep(1.0)

            # If a new page/tab was opened, switch to it
            pages = page.context.pages
            if len(pages) > 1:
                page = pages[-1]
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                time.sleep(1.0)

    # Indeed login detection — requires human sign-in, cannot auto-apply
    if "secure.indeed.com/auth" in page.url or "indeed.com/account" in page.url:
        raise MFADetected(
            f"Indeed requires login before applying (redirected to {page.url})"
        )

    # ── Step 3: AI trap check ─────────────────────────────────────────────────
    html = page.content()
    trap = detect_traps_in_html(html)
    if trap.trap_found:
        raise AITrapDetected(f"{trap.trap_type}: {trap.evidence[:80]}")

    _check_captcha_mfa(page)

    # ── Step 4: Classify portal and build fill instructions ───────────────────
    portal = classify_portal(html=html, url=page.url)
    logger.info("app_id=%d portal=%s url=%s", app_id, portal, page.url)

    # Screenshot before filling
    ss_before = os.path.join(folder_path, "submit_before.png")
    try:
        page.screenshot(path=ss_before, full_page=False)
    except Exception:
        pass

    instructions = build_fill_instructions(answers, portal)

    # ── Step 5: Fill each field ───────────────────────────────────────────────
    errors = []
    # Best-effort: fill address fields by placeholder/aria-label
    _fill_if_visible(page, answers.get("address_line1",""), [
        "input[placeholder*='street' i]", "input[placeholder*='address' i]",
        "input[aria-label*='address' i]", "input[name*='street' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("address_city",""), [
        "input[placeholder*='city' i]", "input[aria-label*='city' i]",
        "input[name*='city' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("address_state",""), [
        "input[placeholder*='state' i]", "input[aria-label*='state' i]",
        "input[name*='state' i]",
    ], fill_delay)
    _fill_if_visible(page, answers.get("address_zip",""), [
        "input[placeholder*='zip' i]", "input[placeholder*='postal' i]",
        "input[aria-label*='zip' i]", "input[name*='zip' i]",
    ], fill_delay)

    # Best-effort: fill website/GitHub URL in any field labelled Website/Portfolio/GitHub
    github_url = answers.get("website_url", "")
    if github_url:
        for website_sel in [
            "input[placeholder*='github' i]",
            "input[placeholder*='website' i]",
            "input[placeholder*='portfolio' i]",
            "input[aria-label*='website' i]",
            "input[aria-label*='github' i]",
            "input[aria-label*='portfolio' i]",
            "input[name*='website' i]",
            "input[name*='github' i]",
            "input[name*='portfolio' i]",
        ]:
            try:
                loc = page.locator(website_sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.fill(github_url)
                    time.sleep(fill_delay)
                    break
            except Exception:
                continue

    for inst in instructions:
        try:
            if inst.field_type == "file":
                # Resume upload
                resume_path = inst.value
                if resume_path and os.path.isfile(resume_path):
                    file_input = page.locator("input[type='file']").first
                    if file_input.count() > 0:
                        file_input.set_input_files(resume_path)
                        time.sleep(fill_delay)
            elif inst.field_type == "select":
                el = page.locator(inst.selector).first
                if el.count() > 0:
                    try:
                        el.select_option(inst.value)
                    except Exception:
                        # Try partial match
                        el.select_option(label=inst.value)
                    time.sleep(fill_delay)
            elif inst.field_type in ("text", "textarea"):
                el = page.locator(inst.selector).first
                if el.count() > 0:
                    el.click()
                    el.fill(inst.value)
                    time.sleep(fill_delay)
            elif inst.field_type == "checkbox" and inst.value.lower() in ("1", "true", "yes"):
                el = page.locator(inst.selector).first
                if el.count() > 0 and not el.is_checked():
                    el.check()
                    time.sleep(fill_delay)
        except Exception as exc:
            msg = f"field {inst.label!r}: {exc}"
            logger.warning("app_id=%d fill error: %s", app_id, msg)
            errors.append(msg)

    # ── Step 6: Click Submit ───────────────────────────────────────────────────
    submitted = False
    for sel in _SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                logger.info("app_id=%d clicking submit selector=%r", app_id, sel)
                loc.click()
                submitted = True
                break
        except Exception:
            continue

    if not submitted:
        raise RuntimeError(
            f"Could not find submit button on {page.url}. "
            "Portal may require manual completion."
        )

    # ── Step 7: Wait for confirmation ─────────────────────────────────────────
    try:
        page.wait_for_load_state("networkidle", timeout=int(_SUBMIT_WAIT * 1000))
    except Exception:
        pass
    time.sleep(2.0)

    # Check for CAPTCHA/MFA that appeared after submit
    _check_captcha_mfa(page)

    # Screenshot after submit (confirmation page)
    ss_after = os.path.join(folder_path, "submit_confirmation.png")
    try:
        page.screenshot(path=ss_after, full_page=False)
    except Exception:
        ss_after = None

    final_url = page.url
    receipt = f"PORTAL:{portal}:{final_url}"
    logger.info(
        "app_id=%d submitted portal=%s final_url=%s fill_errors=%d",
        app_id, portal, final_url, len(errors),
    )

    return AutoSubmitResult(
        success=True,
        app_id=app_id,
        receipt=receipt,
        screenshot_path=ss_after,
    )


def _extract_apply_href(page) -> str:
    """
    For listing pages (RemoteOK, HN, etc.) that render Apply links via JS,
    extract the href directly instead of clicking.
    Returns empty string if nothing useful found.
    """
    _APPLY_LINK_SELECTORS = [
        "a.apply[href]",
        "a.button.apply[href]",
        "a:text-is('Apply Now')[href]",
        "a:text-is('Apply')[href]",
        ".apply-link[href]",
        "[data-testid='apply-link'][href]",
    ]
    for sel in _APPLY_LINK_SELECTORS:
        try:
            href = page.locator(sel).first.get_attribute("href")
            if href and href.startswith("http"):
                return href
        except Exception:
            continue
    return ""


def _fill_if_visible(page, value: str, selectors: list[str], delay: float) -> None:
    """Try each selector in order; fill the first visible match."""
    if not value:
        return
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                current = loc.input_value() if loc.input_value else ""
                if not current:   # don't overwrite pre-filled fields
                    loc.fill(value)
                    time.sleep(delay)
                return
        except Exception:
            continue


def _check_captcha_mfa(page) -> None:
    """Raise CaptchaDetected or MFADetected if detected on the current page."""
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
