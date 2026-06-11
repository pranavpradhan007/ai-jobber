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
    # Class-based: LinkedIn's apply button historically uses this class
    "button.jobs-apply-button[aria-label*='Easy Apply']",
    "button.jobs-apply-button",
    # NOTE: bare button[aria-label*='Easy Apply'] removed — on search-results pages
    # LinkedIn's "Easy Apply ×" filter chip ALSO has aria-label*='Easy Apply',
    # clicking it removes the search filter instead of opening the apply modal.
    # Use _js_click_easy_apply() as the fallback instead (it excludes filter chips).
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

    # Click Easy Apply — try class-based selectors first, then JS fallback (filter-chip safe)
    # If not found on direct URL, fall back to search sidebar URL.
    # (LinkedIn SPA sometimes delays rendering the apply button on /jobs/view/ pages.)
    clicked = _click_any(page, _APPLY_BUTTON_SELECTORS, timeout=8000)
    if not clicked:
        clicked = _js_click_easy_apply(page)
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
            # On search results pages, use JS click to avoid the "Easy Apply ×" filter chip
            clicked = _click_any(page, _APPLY_BUTTON_SELECTORS, timeout=5000)
            if not clicked:
                clicked = _js_click_easy_apply(page)
    if not clicked:
        raise RuntimeError(f"LinkedIn Easy Apply button not found on {job_url}")
    _human_pause(1.5, 2.5)
    _screenshot(page, folder_path, "li_02_modal_open.png")
    # Verify the modal actually opened — if not, we clicked a wrong button
    try:
        page.wait_for_selector(
            "div[data-test-modal], div[aria-label*='Easy Apply']",
            timeout=4000, state="visible",
        )
    except Exception:
        raise RuntimeError(f"LinkedIn Easy Apply: modal did not open after click — job may use external apply: {job_url}")

    # Work through modal steps
    max_steps = 15
    _consecutive_save_dialogs = 0  # bail if stuck dismissing save dialogs repeatedly
    for step in range(max_steps):
        page_transition_pause()
        _check_auth_wall(page)
        _check_li_captcha(page)
        # Dismiss any "Save this application?" dialog LinkedIn shows on validation failure
        dismissed = _dismiss_save_dialog(page)
        if dismissed:
            _consecutive_save_dialogs += 1
            if _consecutive_save_dialogs >= 4:
                _screenshot(page, folder_path, "li_stuck_save_dialog.png")
                raise RuntimeError(
                    f"LinkedIn Easy Apply: stuck — save dialog dismissed "
                    f"{_consecutive_save_dialogs}x without progress (unfilled required fields)"
                )
        else:
            _consecutive_save_dialogs = 0
        if step < 2:  # only screenshot early steps to save memory
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

        # Check modal closed BEFORE trying Continue — stray Continue buttons on
        # the LinkedIn background page can fool _click_any and loop 25 times.
        if _modal_closed(page):
            logger.info("app_id=%d linkedin modal closed — application likely sent", app_id)
            return _detect_confirmation(page) or "submitted"

        if _click_any(page, _CONTINUE_SELECTORS, timeout=3000):
            continue

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

    # LinkedIn Profile URL — use get_by_label (robust, handles aria-labelledby etc.)
    _li_url = answers.get("linkedin_url", "https://www.linkedin.com/in/pranav-pradhan-150072236/")
    _fill_by_label(page, "LinkedIn Profile", _li_url)
    # CSS fallback for alternative attribute patterns
    _fill_if_empty(page,
                   "input[placeholder*='linkedin.com/in'], "
                   "input[placeholder*='linkedin.com'], "
                   "input[id*='linkedin']:not([id*='button'])",
                   _li_url, delay)

    # Current job title / position
    _fill_by_label(page, "Position", candidate.get("current_title",
                   answers.get("job_title", "Machine Learning Engineer")))
    _fill_if_empty(page,
                   "input[aria-label*='Position'], input[id*='position'], "
                   "input[aria-label*='Job title'], input[id*='title']:not([id*='page'])",
                   candidate.get("current_title", answers.get("job_title",
                       "Machine Learning Engineer")), delay)

    # Resume upload — only if upload button is visible
    _upload_resume(page, resume_pdf_path)

    # Yes/No radio groups — JS-based (handles any container structure)
    _fill_radio_groups_js(page)
    # CSS fieldset fallback
    _fill_radio_groups(page, answers)

    # Select dropdowns
    _fill_selects(page, answers)

    # Free text questions (textareas)
    _fill_text_questions(page, answers)

    # Salary / number inputs — try label-based first, then CSS
    _salary = str(answers.get("salary_expectation", "110000"))
    for _lbl in ("salary", "Desired salary", "Starting salary", "Expected salary",
                 "Compensation", "Annual salary"):
        _fill_by_label(page, _lbl, _salary)

    # Salary / number inputs that aren't textareas
    _fill_inline_number_inputs(page, answers)


def _fill_by_label(page, label_text: str, value: str):
    """Fill a field by matching its visible label text via Playwright's get_by_label.
    Handles aria-label, aria-labelledby, and label[for=id] automatically."""
    if not value:
        return
    try:
        el = page.get_by_label(label_text, exact=False).first
        if el.count() > 0 and el.is_visible():
            cur = ""
            try:
                cur = el.input_value() or ""
            except Exception:
                pass
            if not cur.strip():
                human_fill(page, el, value, long_text=False)
                logger.debug("fill_by_label: filled %r", label_text)
    except Exception as exc:
        logger.debug("fill_by_label label=%r error: %s", label_text, exc)


def _fill_radio_groups_js(page):
    """JS-based radio fill: finds all visible unselected yes/no groups.

    Matches by option LABEL text (Yes/No) not by value attribute, since
    LinkedIn uses arbitrary value strings (not 'Yes'/'No').
    """
    _YES_KEYS = [
        "authorized", "legally", "eligible", "right to work",
        "built", "maintained", "production", "deployed", "trained",
        "machine learning", "deep learning", "neural", "nlp", "llm",
        "python", "tensorflow", "pytorch", "experience with",
        "agree", "confirm", "acknowledge", "background check",
        "drug test", "on-site", "hybrid", "remote", "18 years", "18+ years",
    ]
    _NO_KEYS = ["sponsor", "visa sponsorship", "require sponsorship", "need sponsorship"]
    try:
        groups = page.evaluate("""
            () => {
                const inputs = Array.from(document.querySelectorAll('input[type="radio"]'));
                const map = {};
                inputs.forEach(inp => {
                    if (!inp.offsetParent) return;
                    const name = inp.name || inp.id || '';
                    if (!name) return;
                    if (!map[name]) {
                        let q = '';
                        let el = inp;
                        for (let i = 0; i < 10 && el; i++, el = el.parentElement) {
                            const lg = el.querySelector('legend');
                            if (lg && lg.textContent.trim()) { q = lg.textContent.trim(); break; }
                            const sp = el.querySelector('[class*="label"]:not(input):not(label[for])');
                            if (sp && sp.textContent.trim() && sp.textContent.trim().length > 5) {
                                q = sp.textContent.trim(); break;
                            }
                        }
                        map[name] = { name, question: q.toLowerCase(), opts: [] };
                    }
                    // Capture option label text (from <label for=id> or aria-label) as well as value
                    let labelText = '';
                    if (inp.id) {
                        const lbl = document.querySelector('label[for="' + inp.id + '"]');
                        if (lbl) labelText = lbl.textContent.trim().toLowerCase();
                    }
                    if (!labelText) labelText = (inp.getAttribute('aria-label') || '').toLowerCase();
                    if (!labelText) {
                        // walk up to find sibling/parent label text
                        const p = inp.parentElement;
                        if (p) labelText = p.textContent.trim().toLowerCase();
                    }
                    map[name].opts.push({ value: inp.value, labelText, checked: inp.checked });
                });
                return Object.values(map);
            }
        """)
        for grp in (groups or []):
            name = grp.get("name", "")
            question = grp.get("question", "")
            opts = grp.get("opts", [])
            if not name or not opts:
                continue
            if any(o.get("checked") for o in opts):
                continue  # already answered
            if any(k in question for k in _NO_KEYS):
                target = "no"
            elif any(k in question for k in _YES_KEYS):
                target = "yes"
            else:
                continue
            # Match by label text first, fall back to value attribute
            chosen = None
            for opt in opts:
                lbl = opt.get("labelText", "").lower()
                val = opt.get("value", "").lower()
                if lbl == target or lbl.startswith(target) or val == target:
                    chosen = opt
                    break
            # If no exact match, pick first opt for "yes" / last for "no" as positional fallback
            if chosen is None:
                chosen = opts[0] if target == "yes" else opts[-1]
            try:
                radio = page.locator(
                    f"input[type='radio'][name='{name}'][value='{chosen['value']}']"
                ).first
                if radio.count() > 0 and radio.is_visible() and not radio.is_checked():
                    radio.click()
                    logger.debug("fill_radio_groups_js: clicked %s=%s (label=%r)",
                                 name, chosen["value"], chosen.get("labelText"))
            except Exception:
                pass
    except Exception as exc:
        logger.debug("fill_radio_groups_js error: %s", exc)


def _fill_inline_number_inputs(page, answers: dict):
    """Fill visible single-line text/number inputs for salary, years-exp etc."""
    try:
        inputs = page.locator("input[type='text']:visible, input[type='number']:visible").all()
        for inp in inputs:
            try:
                inp_id = inp.get_attribute("id") or ""
                aria = (inp.get_attribute("aria-label") or "").lower().strip()
                label_text = ""
                if inp_id:
                    lbl = page.locator(f"label[for='{inp_id}']").first
                    if lbl.count() > 0:
                        label_text = lbl.inner_text().lower().strip()
                key = label_text or aria
                if not key:
                    continue
                # Skip fields already handled by dedicated fillers
                if any(x in key for x in ["phone", "email", "city", "linkedin", "position",
                                           "title", "first name", "last name", "name"]):
                    continue
                cur = ""
                try:
                    cur = inp.input_value() or ""
                except Exception:
                    pass
                if cur.strip():
                    continue
                if "salary" in key or "compensation" in key or "pay" in key:
                    human_fill(page, inp, str(answers.get("salary_expectation", "110000")), long_text=False)
                elif "year" in key and ("experience" in key or "exp" in key):
                    human_fill(page, inp, str(answers.get("years_experience", "3")), long_text=False)
                elif "gpa" in key:
                    human_fill(page, inp, "3.8", long_text=False)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("fill_inline_number_inputs error: %s", exc)


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
    cap = value.capitalize()
    # Try multiple patterns: exact value match (both cases) and label text
    for sel in [
        f"input[type='radio'][value='{value}']",
        f"input[type='radio'][value='{cap}']",
        f"label:has-text('{cap}')",
        f"[role='radio'][aria-label*='{cap}']",
    ]:
        try:
            el = group.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click()
                return
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
    """Fill open-text questions (textareas + text inputs) using screener answers + smart defaults."""
    from src.browser.screener_engine import match_question_to_category

    # Fields already handled upstream — skip them to avoid double-fill
    _SKIP_LABELS = {
        "first name", "last name", "full name", "phone", "email", "city",
        "linkedin profile", "position", "job title", "location", "zip", "postal",
    }

    def _get_label(el, el_id: str) -> str:
        label = ""
        if el_id:
            try:
                lbl = page.locator(f"label[for='{el_id}']").first
                if lbl.count() > 0:
                    label = lbl.inner_text().strip()
            except Exception:
                pass
        if not label:
            try:
                aria = el.get_attribute("aria-label") or ""
                if aria:
                    label = aria.strip()
            except Exception:
                pass
        if not label:
            label = (el.get_attribute("placeholder") or "").strip()
        return label.lower()

    def _fill_one(el, long_text: bool):
        try:
            cur = el.input_value() or ""
        except Exception:
            cur = ""
        if cur.strip():
            return  # already filled
        el_id = el.get_attribute("id") or ""
        key = _get_label(el, el_id)
        if not key or any(sk in key for sk in _SKIP_LABELS):
            return

        # Direct answers dict match
        if key in answers:
            human_fill(page, el, str(answers[key]), long_text=long_text)
            inter_field_pause()
            return

        # screener_engine category match
        match = match_question_to_category(key)
        if match:
            cat, answer_key = match
            val = answers.get(cat) or answers.get(answer_key)
            if val:
                human_fill(page, el, str(val), long_text=long_text)
                inter_field_pause()
                return

        # Smart defaults for common short-text question patterns
        if "cover letter" in key:
            return  # not required on LinkedIn
        if any(w in key for w in ("salary", "compensation", "rate", "pay")):
            human_fill(page, el, str(answers.get("salary_expectation", "110000")),
                       long_text=False)
            inter_field_pause()
            return
        if any(w in key for w in ("start date", "available", "notice period", "start")):
            human_fill(page, el, answers.get("start_date", "2 weeks"), long_text=False)
            inter_field_pause()
            return
        if any(w in key for w in ("years", "experience")):
            human_fill(page, el, str(answers.get("years_experience", "5")), long_text=False)
            inter_field_pause()
            return
        if any(w in key for w in ("github", "portfolio", "website", "url", "link")):
            human_fill(page, el, answers.get("github_url", "https://github.com/pranavpradhan007"),
                       long_text=False)
            inter_field_pause()
            return

    try:
        # Textareas — long-form answers
        for ta in (page.locator("textarea:visible").all() or []):
            try:
                _fill_one(ta, long_text=True)
            except Exception:
                pass
    except Exception:
        pass

    try:
        # Text inputs for additional questions — short-form answers
        # Exclude inputs already handled by _fill_field/_fill_by_label upstream
        for inp in (page.locator(
            "input[type='text']:visible, input[type='number']:visible"
        ).all() or []):
            try:
                el_id = inp.get_attribute("id") or ""
                # Skip if it looks like a standard contact field by ID
                if any(x in el_id.lower() for x in
                       ("phone", "email", "city", "location", "linkedin", "position",
                        "title", "name", "zip", "postal")):
                    continue
                _fill_one(inp, long_text=False)
            except Exception:
                pass
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _js_click_easy_apply(page) -> bool:
    """JS-based Easy Apply click — finds the actual apply button, excludes filter chips.

    On LinkedIn search-results pages the filter bar contains an "Easy Apply" chip
    that also has aria-label matching. We exclude chips by:
      1. Container class (artdeco-pill, search-reusable)
      2. aria-label remove/clear/filter keywords
      3. Pick the LAST candidate (job detail panel is after filter bar in DOM order)
    """
    try:
        clicked = page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const candidates = buttons.filter(b => {
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.innerText || b.textContent || '').trim().toLowerCase();
                    // Must reference Easy Apply
                    if (!label.includes('easy apply') && text !== 'easy apply') return false;
                    // Exclude if aria-label has removal/filter keywords
                    if (label.includes('remove') || label.includes('clear') ||
                        label.includes('dismiss') || label.includes('filter')) return false;
                    // Exclude filter chips by their container class
                    const inPill = !!b.closest(
                        '[class*=\"search-reusable\"], [class*=\"filter-pill\"], ' +
                        '.artdeco-pill--choice, .artdeco-pill--removable, ' +
                        '[data-basic-pill]'
                    );
                    if (inPill) return false;
                    // Exclude if the button itself has pill/chip classes
                    const cls = (b.getAttribute('class') || '').toLowerCase();
                    if (cls.includes('artdeco-pill') || cls.includes('filter-pill')) return false;
                    // Must be visible
                    const rect = b.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    const style = window.getComputedStyle(b);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    return true;
                });
                if (!candidates.length) return false;
                // Pick the LAST match: job detail panel (right col) comes after
                // the filter bar in DOM order, so the actual apply button is last.
                candidates[candidates.length - 1].click();
                return true;
            }
        """)
        return bool(clicked)
    except Exception as exc:
        logger.debug("_js_click_easy_apply error: %s", exc)
        return False


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
    """Return True if the Easy Apply modal is no longer present or not visible."""
    try:
        modal = page.locator("div[data-test-modal], div[aria-label*='Easy Apply']").first
        if modal.count() == 0:
            return True
        # LinkedIn keeps the modal element in DOM after close — check visibility
        return not modal.is_visible()
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
