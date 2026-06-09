"""
LinkedIn Easy Apply submission via Playwright.

Flow:
  1. Navigate to job URL
  2. Click "Easy Apply"
  3. Fill multi-step modal: contact → phone → resume → questions → review → submit
  4. Handle CAPTCHA → raise CaptchaDetected
  5. Handle MFA/login wall → raise MFARequired
  6. Return receipt URL or submitted=True on success

Coordinates with auto_submit.py's _human_pause, _screenshot helpers.
"""
from __future__ import annotations
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

from src.browser.prefill import CaptchaDetected, MFADetected, AITrapDetected  # noqa: F401
from src.browser.auto_submit import (
    _human_pause,
    _screenshot,
    _check_auth_wall,
)
from src.browser.human_mouse import (
    human_fill,
    human_fill_select,
    inter_field_pause,
    page_transition_pause,
    reading_pause,
)

logger = logging.getLogger(__name__)

_APPLY_BUTTON_SELECTORS = [
    "button.jobs-apply-button[aria-label*='Easy Apply']",
    "button[aria-label*='Easy Apply']",
    "button:has-text('Easy Apply')",
]

_CONTINUE_SELECTORS = [
    "button[aria-label='Continue to next step']",
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "footer button[data-easy-apply-next-button]",
]

_REVIEW_SELECTORS = [
    "button[aria-label='Review your application']",
    "button:has-text('Review')",
]

_SUBMIT_SELECTORS = [
    "button[aria-label='Submit application']",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
]


def linkedin_easy_apply(
    page,
    job_url: str,
    app_id: int,
    *,
    candidate: dict,
    resume_pdf_path: str,
    folder_path: str,
    answers: dict | None = None,
    fill_delay: float = 0.15,
    pause_before_submit: bool = False,
) -> str:
    """
    Attempt LinkedIn Easy Apply for the given job URL.

    candidate keys expected:
      name, email, phone, linkedin_url, city, state, zip_code

    Returns the receipt URL or "submitted" if no receipt URL available.
    Raises CaptchaDetected or MFARequired on blocks.
    """
    import time as _time
    answers = answers or {}

    # Navigate to job page
    page.goto(job_url, timeout=25_000, wait_until="domcontentloaded")
    reading_pause()
    _check_auth_wall(page)
    _screenshot(page, folder_path, "li_01_job_page.png")

    # Click Easy Apply — if not found on direct URL, try search sidebar URL
    # (LinkedIn doesn't render Easy Apply on direct /jobs/view/ pages after SPA changes)
    clicked = _click_any(page, _APPLY_BUTTON_SELECTORS, timeout=5000)
    if not clicked:
        import re as _re
        m = _re.search(r"/jobs/view/(\d+)", job_url)
        if m:
            job_id_str = m.group(1)
            sidebar_url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?currentJobId={job_id_str}&f_AL=true&f_JT=F"
            )
            logger.info("app_id=%d Easy Apply not found on direct URL, trying sidebar URL", app_id)
            page.goto(sidebar_url, timeout=25_000, wait_until="domcontentloaded")
            reading_pause()
            _check_auth_wall(page)
            _screenshot(page, folder_path, "li_01b_sidebar.png")
            clicked = _click_any(page, _APPLY_BUTTON_SELECTORS, timeout=8000)
    if not clicked:
        raise RuntimeError(f"LinkedIn Easy Apply button not found on {job_url}")
    _human_pause(1.5, 2.5)
    _screenshot(page, folder_path, "li_02_modal_open.png")

    # Work through modal steps
    max_steps = 25
    for step in range(max_steps):
        page_transition_pause()
        _check_auth_wall(page)
        _check_li_captcha(page)
        # Dismiss any "Save this application?" dialog that LinkedIn shows
        # when Continue is clicked on a validation-failed page.
        _dismiss_save_dialog(page)
        _screenshot(page, folder_path, f"li_step{step:02d}.png")

        # Fill visible fields on this modal page
        _fill_li_modal_page(page, candidate, resume_pdf_path, answers, fill_delay)

        # Determine next action
        submit_found = any(
            page.locator(sel).count() > 0 for sel in _SUBMIT_SELECTORS
        )
        if submit_found:
            if pause_before_submit:
                _screenshot(page, folder_path, "pre_submit_review.png")
                logger.info("app_id=%d LinkedIn REVIEW MODE — form filled, paused before Submit. "
                            "Review in Chrome and click Submit manually.", app_id)
                print(f"\n{'='*60}")
                print(f"  REVIEW MODE — APP-{app_id} LinkedIn form is ready in Chrome")
                print(f"  Review the form, then manually click Submit in Chrome.")
                print(f"{'='*60}\n")
                try:
                    while True:
                        _time.sleep(10)
                        try:
                            page.evaluate("() => document.title")
                        except Exception:
                            logger.info("app_id=%d Chrome closed — review mode ended", app_id)
                            break
                except KeyboardInterrupt:
                    logger.info("app_id=%d review mode interrupted", app_id)
                return "review_mode_ended"
            if _click_any(page, _SUBMIT_SELECTORS, timeout=3000):
                _human_pause(2.0, 4.0)
                _screenshot(page, folder_path, "li_submitted.png")
                logger.info("app_id=%d linkedin easy apply submitted", app_id)
                return _detect_confirmation(page) or "submitted"

        if _click_any(page, _REVIEW_SELECTORS, timeout=2000):
            continue

        if _click_any(page, _CONTINUE_SELECTORS, timeout=3000):
            continue

        # Check if modal closed (application done without explicit submit button)
        if _modal_closed(page):
            logger.info("app_id=%d linkedin modal closed — application likely sent", app_id)
            return "submitted"

        logger.warning("app_id=%d no actionable button at step=%d", app_id, step)
        _screenshot(page, folder_path, f"li_stuck_step{step:02d}.png")
        break

    raise RuntimeError(f"LinkedIn Easy Apply: could not complete after {max_steps} steps")


def _dismiss_save_dialog(page) -> bool:
    """
    Dismiss the 'Save this application?' dialog LinkedIn shows when Continue
    is clicked on a validation-failed page. Press Escape to cancel the dialog
    and return to the form (not Discard which deletes the application).
    Returns True if dismissed.
    """
    try:
        # Dialog detection: look for the save/discard button pair
        discard = page.locator("button:has-text('Discard')").first
        if discard.count() > 0 and discard.is_visible():
            page.keyboard.press("Escape")
            time.sleep(0.5)
            logger.info("Dismissed 'Save this application?' dialog via Escape")
            return True
    except Exception as exc:
        logger.debug("dialog dismiss error: %s", exc)
    return False


# ── Page filler ───────────────────────────────────────────────────────────────

def _fill_li_modal_page(page, candidate: dict, resume_pdf_path: str, answers: dict, delay: float):
    """Fill all visible input fields in the Easy Apply modal."""

    # Phone
    _fill_field(page, "input[id*='phoneNumber'], input[aria-label*='Phone'], input[name*='phone']",
                candidate.get("phone", ""), delay)

    # Email (usually pre-filled; skip if already has value)
    _fill_if_empty(page, "input[id*='email'], input[aria-label*='Email']",
                   candidate.get("email", ""), delay)

    # City / location
    _fill_field(page, "input[aria-label*='City'], input[id*='city']",
                candidate.get("city", ""), delay)

    # LinkedIn Position (current job title) — required on some additional-questions pages
    _fill_if_empty(page,
                   "input[aria-label*='Position'], input[id*='position'], "
                   "input[aria-label*='Job title'], input[id*='title']:not([id*='page'])",
                   candidate.get("current_title", answers.get("current_title",
                       answers.get("job_title", "Machine Learning Engineer"))), delay)

    # Resume upload — only if upload button is visible
    _upload_resume(page, resume_pdf_path)

    # Yes/No radio groups — answer "Yes" to authorized to work, "No" to sponsorship
    _fill_radio_groups(page, answers)

    # Select dropdowns
    _fill_selects(page, answers)

    # Free text questions
    _fill_text_questions(page, answers)


def _fill_field(page, selector: str, value: str, delay: float):
    """Fill a text input with human-like interaction (instant fill + events)."""
    if not value:
        return
    try:
        el = page.locator(selector).first
        if el.count() > 0 and el.is_visible():
            human_fill(page, el, value, long_text=False)
    except Exception as exc:
        logger.debug("fill_field selector=%r error: %s", selector, exc)


def _fill_if_empty(page, selector: str, value: str, delay: float):
    """Fill a field only if it currently has no value."""
    if not value:
        return
    try:
        el = page.locator(selector).first
        if el.count() > 0 and el.is_visible():
            current = el.input_value() or ""
            if not current.strip():
                _fill_field(page, selector, value, delay)
    except Exception:
        pass


def _upload_resume(page, resume_pdf_path: str):
    """Upload resume PDF via file input if visible."""
    if not resume_pdf_path or not Path(resume_pdf_path).is_file():
        return
    try:
        file_input = page.locator("input[type='file']").first
        if file_input.count() > 0:
            file_input.set_input_files(resume_pdf_path)
            _human_pause(1.0, 2.0)
            logger.info("linkedin: resume uploaded from %s", resume_pdf_path)
    except Exception as exc:
        logger.warning("linkedin: resume upload failed: %s", exc)


def _fill_radio_groups(page, answers: dict):
    """
    Answer yes/no radios using heuristics:
    - "authorized to work" / "legally authorized" → Yes
    - "require sponsorship" / "visa sponsorship" → No
    """
    try:
        groups = page.locator("fieldset").all()
        for grp in groups:
            label_text = ""
            try:
                label_el = grp.locator("legend, label").first
                label_text = label_el.inner_text().lower() if label_el.count() > 0 else ""
            except Exception:
                pass

            if not label_text:
                continue

            if any(k in label_text for k in ["authorized", "legally", "eligible", "right to work"]):
                _click_radio(grp, "yes")
            elif any(k in label_text for k in ["require sponsorship", "need sponsorship", "visa sponsorship",
                                                "require work authorization", "work permit"]):
                _click_radio(grp, "no")
            elif any(k in label_text for k in ["sponsor", "visa"]):
                _click_radio(grp, "no")
            # Affirmative answers for ML/AI experience, skills, agreements
            elif any(k in label_text for k in [
                "built", "maintained", "production", "deployed", "trained",
                "machine learning", "deep learning", "neural", "nlp", "llm",
                "python", "tensorflow", "pytorch", "experience with",
                "agree", "confirm", "acknowledge", "18 years", "18+ years", "background check",
                "drug test", "on-site", "hybrid", "remote",
            ]):
                _click_radio(grp, "yes")
            elif label_text in answers:
                _click_radio(grp, answers[label_text])
    except Exception as exc:
        logger.debug("radio fill error: %s", exc)


def _click_radio(group, value: str):
    """Click a radio button matching 'yes' or 'no' in a fieldset."""
    sel = f"input[type='radio'][value='{value}'], label:has-text('{value.capitalize()}')"
    try:
        el = group.locator(sel).first
        if el.count() > 0 and el.is_visible():
            el.click()
    except Exception:
        pass


def _fill_selects(page, answers: dict):
    """Fill visible select dropdowns from answers dict."""
    try:
        selects = page.locator("select:visible").all()
        for sel_el in selects:
            try:
                label = ""
                sel_id = sel_el.get_attribute("id") or ""
                if sel_id:
                    lbl = page.locator(f"label[for='{sel_id}']").first
                    if lbl.count() > 0:
                        label = lbl.inner_text().lower().strip()
                if label in answers:
                    sel_el.select_option(label=answers[label])
            except Exception:
                pass
    except Exception:
        pass


def _fill_text_questions(page, answers: dict):
    """Fill open-text questions using pre-computed screener answers + smart defaults."""
    from src.browser.screener_engine import match_question_to_category
    try:
        textareas = page.locator("textarea:visible").all()
        for ta in textareas:
            try:
                placeholder = (ta.get_attribute("placeholder") or "").lower()
                ta_id = ta.get_attribute("id") or ""
                label_text = ""
                if ta_id:
                    lbl = page.locator(f"label[for='{ta_id}']").first
                    if lbl.count() > 0:
                        label_text = lbl.inner_text().lower().strip()

                key = label_text or placeholder
                if not key:
                    continue

                # Direct key match in answers
                if key in answers:
                    human_fill(page, ta, str(answers[key]), long_text=True)
                    inter_field_pause()
                    continue

                # screener_engine category match
                match = match_question_to_category(key)
                if match:
                    cat, answer_key = match
                    val = answers.get(cat) or answers.get(answer_key)
                    if val:
                        human_fill(page, ta, str(val), long_text=True)
                        inter_field_pause()
                        continue

                # Skip cover letters (not required on LinkedIn)
                if "cover letter" in key:
                    continue

                # Salary fallback
                if "salary" in key:
                    human_fill(page, ta, answers.get("salary_expectation", "80000"), long_text=False)
                    inter_field_pause()

            except Exception:
                pass
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _click_any(page, selectors: list[str], timeout: int = 3000) -> bool:
    """Try each selector; click first visible match. Returns True if clicked."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() == 0:
                continue
            try:
                el.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                el.click(timeout=timeout)
                return True
            except Exception:
                try:
                    page.evaluate("(e) => e.click()", el.element_handle())
                    return True
                except Exception:
                    pass
        except Exception:
            continue
    return False


def _check_li_captcha(page):
    """Raise CaptchaDetected if LinkedIn CAPTCHA challenge visible."""
    captcha_sels = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "#captcha-internal",
        "div[class*='challenge']",
    ]
    for sel in captcha_sels:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                raise CaptchaDetected(f"LinkedIn CAPTCHA detected ({sel})")
        except CaptchaDetected:
            raise
        except Exception:
            pass


def _modal_closed(page) -> bool:
    """Return True if the Easy Apply modal is no longer present."""
    try:
        modal = page.locator("div[data-test-modal], div[aria-label*='Easy Apply']").first
        return modal.count() == 0
    except Exception:
        return False


def _detect_confirmation(page) -> str:
    """Try to find an application confirmation URL or message."""
    try:
        if "confirmation" in page.url or "apply/confirmation" in page.url:
            return page.url
        msg = page.locator("h3:has-text('Your application was sent'), div:has-text('application sent')").first
        if msg.count() > 0:
            return "submitted_confirmed"
    except Exception:
        pass
    return ""
