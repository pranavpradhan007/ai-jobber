"""
Fast universal form filler.

Uses form_detector.DetectedField + answers dict to fill any portal's form
without hardcoded CSS selectors. All interactions go through human_mouse.py
so mouse movement, typing speed, and pauses look like a real user.

Completion checker: verify_form_complete() scans for validation errors
BEFORE the submit click — never submits a form with visible errors.

Timing vs original _fill_all_fields():
  Before: 15 fields × 2.0 s delay = 30 s minimum (plus typing time)
  After:  15 fields × ~0.6 s average (scroll + click + fill + pause) = ~9 s
"""
from __future__ import annotations
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from src.browser.form_detector import DetectedField
from src.browser.human_mouse import (
    human_fill,
    human_fill_select,
    human_click_radio,
    human_check_checkbox,
    human_scroll_to,
    inter_field_pause,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Label alias table — maps display labels to answer-dict keys
# ---------------------------------------------------------------------------
_LABEL_ALIASES: dict[str, str] = {
    "full name":                "full_name",
    "name":                     "full_name",
    "your name":                "full_name",
    "first name":               "first_name",
    "firstname":                "first_name",
    "given name":               "first_name",
    "last name":                "last_name",
    "lastname":                 "last_name",
    "family name":              "last_name",
    "surname":                  "last_name",
    "e-mail":                   "email",
    "email address":            "email",
    "your email":               "email",
    "email id":                 "email",
    "enter your email":         "email",
    "enter email":              "email",
    "mobile":                   "phone",
    "mobile number":            "phone",
    "telephone":                "phone",
    "cell":                     "phone",
    "cell phone":               "phone",
    "phone number":             "phone",
    "contact number":           "phone",
    "phone country code":       "phone_country_code",
    "country code":             "phone_country_code",
    "linkedin profile":         "linkedin_url",
    "linkedin profile url":     "linkedin_url",
    "linkedin url":             "linkedin_url",
    "linkedin":                 "linkedin_url",
    "github profile":           "github_url",
    "github url":               "github_url",
    "github":                   "github_url",
    "website":                  "portfolio_url",
    "personal website":         "portfolio_url",
    "portfolio":                "portfolio_url",
    "portfolio url":            "portfolio_url",
    "city":                     "address_city",
    "state":                    "address_state",
    "state/province":           "address_state",
    "zip":                      "address_zip",
    "zip code":                 "address_zip",
    "postal code":              "address_zip",
    "postcode":                 "address_zip",
    "country":                  "address_country",
    "street address":           "address_line1",
    "address":                  "address_line1",
    "address line 1":           "address_line1",
    "address 1":                "address_line1",
    "country phone code":       "phone_country_code",
    "country code":             "phone_country_code",
    "phone country code":       "phone_country_code",
    "phone extension":          "",         # skip — not needed
    "preferred name":           "first_name",
    "legal full name":          "full_name",
    "desired salary":           "salary_expectation",
    "expected salary":          "salary_expectation",
    "salary expectation":       "salary_expectation",
    "compensation":             "salary_expectation",
    "salary":                   "salary_expectation",
    "years of experience":      "years_experience",
    "years experience":         "years_experience",
    "how many years":           "years_experience",
    "years of ml":              "years_experience",
    "years of production":      "years_experience",
    "years of relevant":        "years_experience",
    "years of professional":    "years_experience",
    "total years":              "years_experience",
    "current ctc":              "salary_expectation",
    "expected ctc":             "salary_expectation",
    "notice period":            "",
    "work authorization":       "work_authorization",
    "authorized to work":       "work_authorization",
    "graduation year":          "graduation_year",
    "degree":                   "education_degree",
    "highest degree":           "education_degree",
    "highest education":        "education_degree",
    "cover letter":             "cover_letter_default",
    "additional comments":      "cover_letter_default",
    "message":                  "cover_letter_default",
    "why are you interested":   "cover_letter_default",
    "why this company":         "cover_letter_default",
    "why this role":            "cover_letter_default",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class FastFillResult:
    fields_filled: int = 0
    fields_skipped: int = 0          # already had current_value
    fields_unmatched: int = 0        # no answer found for this label
    unmatched_labels: list = field(default_factory=list)
    fill_time_seconds: float = 0.0
    errors: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fast_fill_form(
    page,
    detected_fields: list[DetectedField],
    answers: dict,
    *,
    fill_delay: float = 0.15,
    skip_filled: bool = True,
) -> FastFillResult:
    """
    Fill all detected form fields using the answers dict.

    Field matching priority:
      1. Exact key match after normalising label text
      2. Alias table lookup (_LABEL_ALIASES)
      3. screener_engine category match (regex patterns)
      4. Field-type fallback (file → resume, email → email, tel → phone)

    All interactions use human_mouse.py — Bezier moves, natural typing speed,
    variable inter-field pauses. Never teleports, never same-speed typing.
    """
    result = FastFillResult()
    t_start = time.time()

    for f in detected_fields:
        try:
            _fill_single(page, f, answers, result, skip_filled)
        except Exception as exc:
            err = f"field={f.label_text!r} selector={f.selector!r}: {exc}"
            logger.warning("fast_autofill: %s", err)
            result.errors.append(err)

    result.fill_time_seconds = round(time.time() - t_start, 2)
    logger.info(
        "fast_fill_form: filled=%d skipped=%d unmatched=%d in %.1fs",
        result.fields_filled, result.fields_skipped,
        result.fields_unmatched, result.fill_time_seconds,
    )
    return result


def verify_form_complete(page) -> tuple[bool, list[str]]:
    """
    Scan the current page for validation errors before clicking Submit.
    Returns (is_complete, list_of_issue_descriptions).

    Checks:
      - aria-invalid="true" on visible fields
      - Visible error message elements
      - Required visible fields with empty values
    """
    issues = []
    try:
        invalid_count = page.evaluate("""() => {
            const els = document.querySelectorAll('[aria-invalid="true"]');
            return Array.from(els).filter(el => {
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden';
            }).length;
        }""")
        if invalid_count:
            issues.append(f"{invalid_count} field(s) marked aria-invalid=true")
    except Exception:
        pass

    try:
        error_count = page.evaluate("""() => {
            const sels = [
                '.error-message', '.field-error', '.form-error',
                '[role="alert"]', '.validation-error', '.error-text',
                '.input-error', '.help-block.error',
            ];
            let count = 0;
            for (const s of sels) {
                const els = document.querySelectorAll(s);
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && (el.innerText || '').trim()) count++;
                }
            }
            return count;
        }""")
        if error_count:
            issues.append(f"{error_count} visible error message(s) on page")
    except Exception:
        pass

    try:
        empty_required = page.evaluate("""() => {
            const required = document.querySelectorAll(
                'input[required]:not([type=hidden]), textarea[required], select[required]'
            );
            return Array.from(required).filter(el => {
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && !el.value.trim();
            }).length;
        }""")
        if empty_required:
            issues.append(f"{empty_required} required field(s) still empty")
    except Exception:
        pass

    return len(issues) == 0, issues


# ---------------------------------------------------------------------------
# Per-field fill logic
# ---------------------------------------------------------------------------

def _fill_single(page, f: DetectedField, answers: dict, result: FastFillResult, skip_filled: bool) -> None:
    # Skip already-filled fields (e.g. email pre-populated by LinkedIn)
    if skip_filled and f.current_value and f.field_type not in ("file", "radio", "checkbox"):
        result.fields_skipped += 1
        return

    # File inputs (resume) — handle immediately
    if f.field_type == "file":
        resume = answers.get("resume", "") or answers.get("resume_path", "")
        if resume and os.path.isfile(resume):
            try:
                loc = page.locator(f.selector).first
                if loc.count() > 0:
                    human_scroll_to(page, loc)
                    loc.set_input_files(resume)
                    time.sleep(1.0)
                    result.fields_filled += 1
                    logger.debug("uploaded resume via %r", f.selector)
                    return
            except Exception as exc:
                result.errors.append(f"resume upload {f.selector}: {exc}")
        result.fields_unmatched += 1
        return

    # Resolve the answer value
    value = _resolve_answer(f, answers)
    if value is None:
        result.fields_unmatched += 1
        if f.label_text:
            result.unmatched_labels.append(f.label_text)
        return

    # Locate element
    try:
        loc = page.locator(f.selector).first
        if loc.count() == 0:
            result.fields_unmatched += 1
            return
        if not loc.is_visible():
            result.fields_skipped += 1
            return
    except Exception:
        result.fields_unmatched += 1
        return

    # For number inputs, strip range strings like "3-5" → "3" (LinkedIn validates as decimal)
    if f.field_type == "number":
        value = re.sub(r"[^\d.].*", "", value) or value

    # Fill by type
    try:
        if f.field_type == "radio":
            _fill_radio(page, f, value, loc)
        elif f.field_type in ("select",):
            human_fill_select(page, loc, value)
        elif f.field_type == "checkbox":
            truthy = value.lower() in ("yes", "true", "1", "on", "checked")
            human_check_checkbox(loc, truthy)
        elif f.field_type == "textarea":
            is_long = len(value) > 60
            human_fill(page, loc, value, long_text=is_long)
        else:
            human_fill(page, loc, value, long_text=False)

        inter_field_pause()
        result.fields_filled += 1
        logger.debug("filled %s=%r via %r", f.field_type, f.label_text[:40], f.selector)

    except Exception as exc:
        result.errors.append(f"fill {f.field_type} {f.selector!r}: {exc}")


def _fill_radio(page, f: DetectedField, value: str, first_loc) -> None:
    """Click the radio option whose label best matches value."""
    target = value.lower().strip()
    # Try each option by label text
    for opt_label in (f.radio_options or []):
        if opt_label.lower().strip() == target or target in opt_label.lower():
            sel = f"input[type='radio'][name='{f.group_name}'] + label:has-text('{opt_label}')"
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    human_click_radio(page, el)
                    return
            except Exception:
                pass
            # Try clicking the radio input whose value matches
            val_sel = f"input[type='radio'][name='{f.group_name}'][value='{opt_label}']"
            try:
                el = page.locator(val_sel).first
                if el.count() > 0:
                    human_click_radio(page, el)
                    return
            except Exception:
                pass
    # Fallback: first_loc for yes-like values
    if target in ("yes", "true", "1"):
        try:
            human_click_radio(page, first_loc)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Answer resolution (three-tier)
# ---------------------------------------------------------------------------

def _normalize_label(text: str) -> str:
    """'First Name *' → 'first_name'"""
    text = text.lower().strip()
    text = re.sub(r"[*:()\[\]{}]", "", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text


def _resolve_answer(f: DetectedField, answers: dict) -> Optional[str]:
    """Three-tier label → answer resolution. Returns None if no match."""
    label = f.label_text.strip()
    norm  = _normalize_label(label)
    # Strip special chars (*, :, etc.) but keep spaces — used for alias table lookup
    stripped_label = re.sub(r"[*:()\[\]{}]", "", label.lower()).strip()
    placeholder = (f.placeholder or "").strip()

    # ── Tier 1: Direct key or alias match ────────────────────────────────────
    for probe in (norm, label, stripped_label, placeholder, _normalize_label(placeholder)):
        if probe in answers:
            return str(answers[probe])
    for probe in (label, norm, stripped_label):
        if probe in _LABEL_ALIASES:
            key = _LABEL_ALIASES[probe]
            if key in answers:
                return str(answers[key])

    # ── Tier 2: screener_engine category match ────────────────────────────────
    from src.browser.screener_engine import match_question_to_category
    for probe in (label, placeholder):
        m = match_question_to_category(probe)
        if m:
            cat, answer_key = m
            # Category itself may be a direct answer key
            if cat in answers:
                return str(answers[cat])
            # Or the answer_key points into answers
            if answer_key in answers:
                return str(answers[answer_key])
            # Synthetic static value
            from src.browser.screener_engine import _STATIC_SYNTHETIC
            if answer_key in _STATIC_SYNTHETIC:
                return _STATIC_SYNTHETIC[answer_key]

    # ── Tier 3: Field-type fallback ───────────────────────────────────────────
    type_fallback: dict[str, list[str]] = {
        "email": ["email"],
        "tel":   ["phone"],
        "url":   ["linkedin_url", "portfolio_url", "github_url"],
    }
    for key in type_fallback.get(f.field_type, []):
        if key in answers and answers[key]:
            return str(answers[key])

    return None
