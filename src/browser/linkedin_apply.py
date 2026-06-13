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
        # Check for daily rate limit banner before treating as permanent failure
        try:
            body_text = (page.evaluate("() => document.body.innerText") or "").lower()
            if any(s in body_text for s in (
                "limit daily submission", "apply tomorrow",
                "prevent bots", "we limit daily",
            )):
                raise RuntimeError(
                    f"LinkedIn Easy Apply: daily rate limit reached — retry tomorrow: {job_url}"
                )
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError(f"LinkedIn Easy Apply: modal did not open after click — job may use external apply: {job_url}")

    # Work through modal steps
    max_steps = 15
    _total_save_dialogs = 0
    _last_dialog_pct = -1   # progress % at last dialog
    _dialogs_at_same_pct = 0  # consecutive dialogs without progress
    for step in range(max_steps):
        page_transition_pause()
        _check_auth_wall(page)
        _check_li_captcha(page)
        # Dismiss any "Save this application?" dialog.
        # Some LinkedIn jobs show this dialog on EVERY step transition (not just validation
        # failures). Track progress % to detect real stuckness vs. routine save prompts.
        if _dismiss_save_dialog(page):
            _total_save_dialogs += 1
            time.sleep(0.8)  # wait for dialog to fully close before screenshot
            _screenshot(page, folder_path, f"li_save_dialog_{_total_save_dialogs:02d}.png")
            current_pct = _get_progress_pct(page)
            logger.info("save dialog #%d pct=%d", _total_save_dialogs, current_pct)
            if current_pct == -1:
                # Progress bar unreadable — only abort after many dialogs total
                if _total_save_dialogs >= 10:
                    _screenshot(page, folder_path, "li_stuck_save_dialog.png")
                    raise RuntimeError(
                        f"LinkedIn Easy Apply: stuck — {_total_save_dialogs} save dialogs, "
                        f"progress bar unreadable"
                    )
            else:
                if current_pct == _last_dialog_pct:
                    _dialogs_at_same_pct += 1
                else:
                    _dialogs_at_same_pct = 1
                    _last_dialog_pct = current_pct
                # Only abort if stuck at the SAME progress % 3+ times — progressing is fine
                if _dialogs_at_same_pct >= 3:
                    _screenshot(page, folder_path, "li_stuck_save_dialog.png")
                    raise RuntimeError(
                        f"LinkedIn Easy Apply: stuck — save dialog {_dialogs_at_same_pct}x "
                        f"at same progress {current_pct}% (unfilled required fields)"
                    )
        if step < 8:  # screenshot first 8 steps for debugging
            _screenshot(page, folder_path, f"li_step{step:02d}.png")

        # Fill visible fields on this modal page
        _fill_li_modal_page(page, candidate, resume_pdf_path, answers, fill_delay)

        # Determine next action
        submit_found = any(
            page.locator(sel).count() > 0 for sel in _SUBMIT_SELECTORS
        )
        if submit_found:
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


def _get_progress_pct(page) -> int:
    """Read the Easy Apply modal's progress bar percentage (0-100). Returns -1 on failure."""
    try:
        return int(page.evaluate("""
            (() => {
                // 1. role=progressbar attribute
                const bar = document.querySelector(
                    '[role="progressbar"], .artdeco-completeness-meter-linear__progress-element'
                );
                if (bar) {
                    const now = bar.getAttribute('aria-valuenow') || bar.getAttribute('value');
                    if (now !== null && now !== '') return parseInt(now);
                    const s = bar.getAttribute('style') || '';
                    const m = s.match(/width:\\s*(\\d+)%/);
                    if (m) return parseInt(m[1]);
                }
                // 2. Modal-scoped: find text that looks like "NN%" (with or without trailing text)
                const modal = document.querySelector(
                    '[data-test-modal], [aria-label*="Easy Apply"], .jobs-easy-apply-modal'
                ) || document.body;
                const all = modal.querySelectorAll('span, div, p');
                for (const el of all) {
                    if (el.children.length > 3) continue;  // skip containers
                    const t = el.textContent.trim();
                    const m = t.match(/^(\\d{1,3})%/);
                    if (m) return parseInt(m[1]);
                }
                return -1;
            })()
        """) or -1)
    except Exception:
        return -1


def _dismiss_save_dialog(page) -> bool:
    """
    Dismiss the 'Save this application?' dialog LinkedIn shows when the modal
    is navigated away or a required field causes validation.
    Click the dialog's own X button to return to the form (not Discard/Save).
    Returns True if dismissed.
    """
    try:
        # Dialog detection: look for the save/discard button pair
        discard = page.locator("button:has-text('Discard')").first
        if discard.count() == 0 or not discard.is_visible():
            return False
        # Try clicking the dismiss X on the dialog overlay itself
        # (not the outer Easy Apply modal's X which would close the whole application)
        dismissed = False
        dismiss_selectors = [
            "button.artdeco-modal__dismiss",
            "[data-test-dialog-header] button",
            "div[role='dialog'] button[aria-label='Dismiss']",
            "div[role='dialog'] button[aria-label*='close' i]",
            "div[role='dialog'] button[aria-label*='Close' i]",
        ]
        for sel in dismiss_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    dismissed = True
                    break
            except Exception:
                pass
        if not dismissed:
            # Fall back to Escape
            page.keyboard.press("Escape")
        time.sleep(0.5)
        logger.info("Dismissed 'Save this application?' dialog (method=%s)",
                    "btn" if dismissed else "Escape")
        return True
    except Exception as exc:
        logger.debug("dialog dismiss error: %s", exc)
    return False


# ── Page filler ───────────────────────────────────────────────────────────────

def _fill_li_modal_page(page, candidate: dict, resume_pdf_path: str, answers: dict, delay: float):
    """Fill all visible input fields in the Easy Apply modal."""

    # Phone country code dropdown (required on contact step — must be set before phone number)
    try:
        cc_sel = "select[id*='phoneCountry'], select[name*='phoneCountry'], select[aria-label*='Phone country']"
        cc_loc = page.locator(cc_sel).first
        if cc_loc.count() > 0 and cc_loc.is_visible(timeout=2000):
            cc_loc.select_option(label="United States (+1)")
    except Exception:
        try:
            # fallback: select by value "+1" or "US"
            cc_loc.select_option(value="US")
        except Exception:
            pass

    # Phone — strip country code prefix so the local-number field gets only digits/local format
    # e.g. "+1 (929) 754-5592" → "(929) 754-5592"  (LinkedIn's +1 country-code select covers the prefix)
    _phone_raw = candidate.get("phone", "")
    _phone_local = re.sub(r'^\+?1[\s\-]?', '', _phone_raw).strip()
    _fill_field(page, "input[id*='phoneNumber'], input[aria-label*='Phone'], "
                "input[name*='phone'], input[type='tel']",
                _phone_local or _phone_raw, delay)

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

    # Scroll the modal to the bottom so any below-the-fold required fields are visible,
    # then re-run text/select/radio fills on newly revealed content
    try:
        page.evaluate("""
            (() => {
                const modal = document.querySelector(
                    '.jobs-easy-apply-modal__content, [data-test-modal] .artdeco-modal__content'
                );
                if (modal) modal.scrollTo(0, modal.scrollHeight);
            })()
        """)
        time.sleep(0.4)
        _fill_radio_groups_js(page)
        _fill_selects(page, answers)
        _fill_text_questions(page, answers)
    except Exception:
        pass


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
                function extractQuestion(inp) {
                    let el = inp;
                    for (let i = 0; i < 12 && el; i++, el = el.parentElement) {
                        // 1. fieldset > legend
                        const lg = el.querySelector('legend');
                        if (lg && lg.textContent.trim()) return lg.textContent.trim();
                        // 2. aria-labelledby on current ancestor
                        const lby = el.getAttribute('aria-labelledby');
                        if (lby) {
                            for (const lid of lby.split(' ')) {
                                const le = document.getElementById(lid);
                                if (le && le.textContent.trim().length > 5) return le.textContent.trim();
                            }
                        }
                        // 3. aria-label on group container (role=group / fieldset)
                        const role = el.getAttribute('role');
                        const al = el.getAttribute('aria-label');
                        if (al && al.trim().length > 5 && (role === 'group' || el.tagName === 'FIELDSET'))
                            return al.trim();
                        // 4. class-based label within ancestor
                        const sp = el.querySelector('[class*="label"]:not(input):not(label[for])');
                        if (sp && sp.textContent.trim().length > 5 &&
                            !sp.querySelector('input,select,textarea'))
                            return sp.textContent.trim();
                        // 5. preceding sibling span/p with enough text
                        if (el.previousElementSibling) {
                            const sib = el.previousElementSibling;
                            const txt = sib.textContent.trim();
                            if (txt.length > 8 && !sib.querySelector('input,select,textarea'))
                                return txt;
                        }
                    }
                    return '';
                }
                const inputs = Array.from(document.querySelectorAll('input[type="radio"]'));
                const map = {};
                inputs.forEach(inp => {
                    if (inp.disabled) return;
                    // LinkedIn uses custom-styled radios where the <input> itself may have
                    // near-zero dimensions (the visible circle is a CSS pseudo-element).
                    // Check if the input OR its associated label is visible.
                    const rect = inp.getBoundingClientRect();
                    let visible = rect.width > 0 && rect.height > 0;
                    if (!visible && inp.id) {
                        const lbl = document.querySelector('label[for="' + inp.id + '"]');
                        if (lbl) {
                            const lr = lbl.getBoundingClientRect();
                            visible = lr.width > 0 && lr.height > 0;
                        }
                    }
                    if (!visible) return;
                    const name = inp.name || inp.id || '';
                    if (!name) return;
                    if (!map[name]) {
                        const q = extractQuestion(inp);
                        map[name] = { name, question: q.toLowerCase(), opts: [] };
                    }
                    let labelText = '';
                    let labelFor = '';
                    if (inp.id) {
                        const lbl = document.querySelector('label[for="' + inp.id + '"]');
                        if (lbl) { labelText = lbl.textContent.trim().toLowerCase(); labelFor = inp.id; }
                    }
                    if (!labelText) labelText = (inp.getAttribute('aria-label') || '').toLowerCase();
                    if (!labelText) {
                        const p = inp.parentElement;
                        if (p) labelText = p.textContent.trim().toLowerCase();
                    }
                    map[name].opts.push({ value: inp.value, id: inp.id || '', labelText, labelFor, checked: inp.checked });
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
                # LinkedIn custom-styled radios: <input> may be near-invisible.
                # Click the <label for="id"> first; fall back to check(force=True).
                clicked = False
                label_for = chosen.get("labelFor", "") or chosen.get("id", "")
                if label_for:
                    lbl_el = page.locator(f"label[for='{label_for}']").first
                    if lbl_el.count() > 0 and lbl_el.is_visible():
                        lbl_el.click()
                        clicked = True
                        logger.debug("fill_radio_groups_js: label-click %s (label=%r target=%s)",
                                     name, chosen.get("labelText"), target)
                if not clicked:
                    radio = page.locator(
                        f"input[type='radio'][name='{name}'][value='{chosen['value']}']"
                    ).first
                    if radio.count() > 0:
                        radio.check(force=True)
                        logger.debug("fill_radio_groups_js: force-check %s=%s (label=%r)",
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
                # Fallback: use placeholder if no label or aria-label found
                if not key:
                    ph = (inp.get_attribute("placeholder") or "").lower().strip()
                    if ph:
                        key = ph
                # Fallback: check parent element text (some LinkedIn forms put label in sibling span)
                if not key:
                    try:
                        parent_text = (inp.evaluate("el => el.parentElement ? el.parentElement.textContent : ''") or "").lower().strip()
                        if len(parent_text) > 3 and len(parent_text) < 150:
                            key = parent_text
                    except Exception:
                        pass
                if not key:
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
                elif "year" in key and ("experience" in key or "exp" in key or "work" in key):
                    human_fill(page, inp, str(answers.get("years_experience", "3")), long_text=False)
                elif "how many year" in key or "years of" in key:
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
    """Select existing resume in modal or upload PDF if no radio choices present."""
    try:
        # Only act on the Resume step — detect by presence of PDF resume cards or "Upload resume" btn
        is_resume_step = page.evaluate("""
            (() => {
                // Resume step specific markers
                const hasUploadBtn = !!document.querySelector(
                    '[data-test-upload-resume], [aria-label*="Upload resume"]'
                );
                const hasResumeCard = !!document.querySelector(
                    '[class*="resume-card"], [data-test-resume-icon], .jobs-resume-picker'
                );
                const hasFileInput = !!document.querySelector('input[type="file"]');
                // Extra: a heading that says exactly "Resume"
                const headings = Array.from(document.querySelectorAll('h1,h2,h3'));
                const hasResumeHeading = headings.some(h => h.textContent.trim().toLowerCase() === 'resume');
                return hasUploadBtn || hasResumeCard || hasFileInput || hasResumeHeading;
            })()
        """)
        if not is_resume_step:
            return  # Not the resume step — don't touch radios here

        # Check for checked radio inside the Easy Apply modal (resume-step radios only)
        modal_has_checked = page.evaluate("""
            (() => {
                const modal = document.querySelector(
                    '[data-test-modal], [aria-label*="Easy Apply"], .jobs-easy-apply-modal__content'
                );
                const root = modal || document.body;
                return root.querySelectorAll('input[type="radio"]:checked').length > 0;
            })()
        """)
        if modal_has_checked:
            logger.debug("linkedin: resume radio already selected in modal — skipping upload")
            return

        # If there are unchecked resume radios, select the first one rather than uploading
        modal_has_any_radio = page.evaluate("""
            (() => {
                const modal = document.querySelector(
                    '[data-test-modal], [aria-label*="Easy Apply"], .jobs-easy-apply-modal__content'
                );
                const root = modal || document.body;
                return root.querySelectorAll('input[type="radio"]').length > 0;
            })()
        """)
        if modal_has_any_radio:
            try:
                first_radio = page.locator("input[type='radio']").first
                # Verify this looks like a resume radio (label is NOT just "Yes" or "No")
                radio_id = first_radio.get_attribute("id") or ""
                label_text = ""
                if radio_id:
                    lbl = page.locator(f"label[for='{radio_id}']").first
                    if lbl.count() > 0:
                        label_text = lbl.inner_text().strip().lower()
                if label_text in ("yes", "no", "y", "n"):
                    return  # This is a Yes/No question radio — don't touch it
                first_radio.click(force=True)
                _human_pause(0.5, 1.0)
                logger.debug("linkedin: clicked first resume radio to select it (label=%r)", label_text)
                return
            except Exception:
                pass
    except Exception:
        pass

    # No radio buttons — fall back to file upload
    if not resume_pdf_path or not Path(resume_pdf_path).is_file():
        return
    try:
        file_input = page.locator("input[type='file']").first
        if file_input.count() > 0:
            file_input.set_input_files(resume_pdf_path)
            _human_pause(2.0, 3.0)
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
    """Fill visible select dropdowns using label matching + screener engine + smart defaults."""
    from src.browser.screener_engine import match_question_to_category

    _YEARS_PATTERNS = ("year", "experience", "exp")
    _EDU_PATTERNS = ("degree", "education", "academic", "qualification")
    _SPONSOR_PATTERNS = ("sponsor", "visa", "work authoriz", "authorized to work",
                         "legally authoriz", "require.*sponsor")
    _SALARY_PATTERNS = ("salary", "compensation", "pay", "rate", "desired")

    def _get_select_label(sel_el) -> str:
        sel_id = sel_el.get_attribute("id") or ""
        if sel_id:
            try:
                lbl = page.locator(f"label[for='{sel_id}']").first
                if lbl.count() > 0:
                    text = lbl.inner_text().strip()
                    if text:
                        return text.lower()
            except Exception:
                pass
        aria = (sel_el.get_attribute("aria-label") or "").strip()
        if aria:
            return aria.lower()
        labelledby = (sel_el.get_attribute("aria-labelledby") or "").strip()
        if labelledby:
            try:
                for lid in labelledby.split():
                    lbl_el = page.locator(f"#{lid}").first
                    if lbl_el.count() > 0:
                        text = lbl_el.inner_text().strip()
                        if text:
                            return text.lower()
            except Exception:
                pass
        return ""

    def _select_best_option(sel_el, value: str) -> bool:
        try:
            sel_el.select_option(value=str(value))
            return True
        except Exception:
            pass
        try:
            sel_el.select_option(label=str(value))
            return True
        except Exception:
            pass
        try:
            opts = sel_el.locator("option").all()
            value_lower = str(value).lower()
            for opt in opts:
                opt_text = (opt.inner_text() or "").strip().lower()
                if opt_text and (value_lower in opt_text or opt_text in value_lower):
                    sel_el.select_option(label=opt.inner_text().strip())
                    return True
        except Exception:
            pass
        return False

    def _select_years_option(sel_el, years_str: str) -> bool:
        try:
            opts = sel_el.locator("option").all()
            target = int("".join(filter(str.isdigit, years_str)) or "5")
            best_opt, best_diff = None, float("inf")
            for opt in opts:
                opt_text = (opt.inner_text() or "").strip()
                nums = [int(n) for n in re.findall(r"\d+", opt_text)]
                if nums:
                    diff = min(abs(n - target) for n in nums)
                    if diff < best_diff:
                        best_diff, best_opt = diff, opt_text
            if best_opt:
                sel_el.select_option(label=best_opt)
                return True
        except Exception:
            pass
        return False

    try:
        selects = page.locator("select:visible").all()
        for sel_el in selects:
            try:
                label = _get_select_label(sel_el)
                if not label:
                    continue
                if "country" in label or "phone country" in label:
                    continue

                # 1. Direct answers dict match
                if label in answers:
                    _select_best_option(sel_el, str(answers[label]))
                    continue

                # 2. screener_engine category match
                match = match_question_to_category(label)
                if match:
                    cat, answer_key = match
                    val = answers.get(cat) or answers.get(answer_key)
                    if val:
                        _select_best_option(sel_el, str(val))
                        continue

                # 3. Yes/No option check FIRST — must run before label-pattern heuristics
                # because labels like "Do you have minimum 5 yrs of experience?" contain
                # "experience" and would otherwise be misrouted to _select_years_option.
                try:
                    opt_texts = [
                        (o.inner_text() or "").strip().lower()
                        for o in sel_el.locator("option").all()
                    ]
                    real_opts = [t for t in opt_texts
                                 if t and t not in ("select an option", "select",
                                                    "please select", "--", "-", "")]
                    if set(real_opts) <= {"yes", "no"}:
                        if any(k in label for k in ("sponsor", "require.*visa", "need.*visa",
                                                    "h-1b", "h1b", "opt", "cpt")):
                            _select_best_option(sel_el, "No")
                        else:
                            _select_best_option(sel_el, "Yes")
                        logger.debug("fill_selects: yes/no dropdown label=%r", label[:60])
                        continue
                except Exception:
                    pass

                # 4. Smart defaults for common LinkedIn dropdown patterns
                if any(p in label for p in _YEARS_PATTERNS):
                    _select_years_option(sel_el, str(answers.get("years_experience", "5")))
                elif any(p in label for p in _EDU_PATTERNS):
                    _select_best_option(sel_el, answers.get("education_degree", "Master's Degree"))
                elif any(p in label for p in _SPONSOR_PATTERNS):
                    if "sponsor" in label:
                        _select_best_option(sel_el, "No")
                    else:
                        _select_best_option(sel_el, "Yes")
                elif any(p in label for p in _SALARY_PATTERNS):
                    _select_best_option(sel_el, str(answers.get("salary_expectation", "110000")))
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
        if any(w in key for w in ("notice period", "notice_period")):
            # LinkedIn notice-period fields expect a decimal number, not text like "2 weeks"
            human_fill(page, el, "14", long_text=False)
        elif any(w in key for w in ("start date", "available", "start")):
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
