"""
Pre-fill engine.

Fills a portal form to the submit screen and STOPS.
Never clicks submit for gated applications.

Runtime: uses Playwright (sync) with a real authenticated session.
Tests:   uses StaticFillEngine which operates on HTML strings — no browser needed.

CAPTCHA/MFA detection → raises CaptchaDetected / MFADetected (caller transitions
application to WAITING_FOR_CAPTCHA / WAITING_FOR_MFA).

Anti-bot evasion code is PROHIBITED (see rules/safety.md for the full list).
This module contains only standard Playwright fills with rate-limit delays.

Rate limit: FILL_DELAY_SECONDS minimum between each field fill (runtime).
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from src.browser.field_mapper import (
    FillInstruction,
    build_fill_instructions,
    discover_fields_from_html,
    FILL_DELAY_SECONDS,
)
from src.browser.portal import classify_portal
from src.browser.trap_detector import detect_traps_in_html, TrapResult
from src.db.applications import get_application

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CaptchaDetected(Exception):
    """Raised when a CAPTCHA element is found on the page."""


class MFADetected(Exception):
    """Raised when an MFA prompt is detected."""


class UnknownFieldEncountered(Exception):
    """Raised when a required field cannot be mapped."""


class AITrapDetected(Exception):
    """Raised when a honeypot / AI-trap field is found in the form HTML."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PrefillResult:
    success: bool
    portal: str
    fields_filled: int
    screenshot_before: Optional[str] = None
    screenshot_after: Optional[str] = None
    captcha_detected: bool = False
    mfa_detected: bool = False
    ai_trap_detected: bool = False
    stopped_at_submit: bool = True   # always True on success — never submitted
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Static fill engine (used in all tests)
# ---------------------------------------------------------------------------

class StaticFillEngine:
    """
    Simulates field filling against a static HTML string.
    No browser, no network, no Playwright.
    Records what would be filled.
    """

    # CSS id patterns that indicate CAPTCHA presence
    CAPTCHA_IDS = frozenset({
        "recaptcha", "g-recaptcha", "h-captcha", "captcha",
        "cf-turnstile", "challenge",
    })

    # CSS id patterns that indicate MFA
    MFA_IDS = frozenset({
        "mfa-code", "otp", "two-factor", "2fa", "authenticator-code",
    })

    def __init__(self, html: str, fill_delay: float = 0.0):
        self.html = html.lower()
        self.fill_delay = fill_delay
        self.filled: list[dict] = []
        self._check_for_captcha_mfa()

    def _check_for_captcha_mfa(self) -> None:
        for cid in self.CAPTCHA_IDS:
            if f'id="{cid}"' in self.html or f"class=\"{cid}\"" in self.html:
                raise CaptchaDetected(f"CAPTCHA element detected: {cid!r}")
        for mid in self.MFA_IDS:
            if f'id="{mid}"' in self.html or f"name=\"{mid}\"" in self.html:
                raise MFADetected(f"MFA element detected: {mid!r}")

    def fill_field(self, instruction: FillInstruction) -> None:
        """Record a fill operation (no actual DOM interaction)."""
        time.sleep(self.fill_delay)
        self.filled.append({
            "selector": instruction.selector,
            "value":    instruction.value,
            "type":     instruction.field_type,
            "label":    instruction.label,
        })
        logger.debug("fill [%s] %s = %r", instruction.field_type,
                     instruction.selector, instruction.value[:40])

    def take_screenshot(self, path: str) -> str:
        """Write a text placeholder screenshot."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                f"[screenshot placeholder]\n"
                f"fields_filled_so_far={len(self.filled)}\n"
                f"html_length={len(self.html)}\n"
            )
        return path

    @property
    def is_at_submit_screen(self) -> bool:
        """Return True if the HTML contains a submit button."""
        return (
            'type="submit"' in self.html
            or 'id="submitapplication"' in self.html
            or 'id="submit_app_button"' in self.html
            or 'class="btn submit"' in self.html
        )


# ---------------------------------------------------------------------------
# Pre-fill pipeline (test-mode and runtime-mode)
# ---------------------------------------------------------------------------

def prefill_application(
    conn: sqlite3.Connection,
    app_id: int,
    answers: dict,
    *,
    html: Optional[str] = None,        # injected in tests (static HTML)
    url: str = "",
    folder_path: Optional[str] = None,
    fill_delay: float = FILL_DELAY_SECONDS,
) -> PrefillResult:
    """
    Pre-fill a portal form for `app_id`.

    In test mode: pass `html=` to use StaticFillEngine.
    In runtime mode: `html` is None and Playwright is used (lazy-imported).

    Returns PrefillResult. Raises CaptchaDetected / MFADetected on detection.
    Never clicks the submit button.
    """
    app = get_application(conn, app_id)
    if not folder_path:
        folder_path = (app.folder_path or "").replace("/", os.sep) or "."

    if html is not None:
        return _prefill_static(app_id, html, answers, url, folder_path,
                               fill_delay)
    else:
        return _prefill_playwright(app_id, answers, url, folder_path,
                                   fill_delay)


def _prefill_static(
    app_id: int,
    html: str,
    answers: dict,
    url: str,
    folder_path: str,
    fill_delay: float,
) -> PrefillResult:
    """Static-HTML fill path — used in all tests."""
    portal = classify_portal(html=html, url=url)
    logger.info("prefill app_id=%d portal=%s (static mode)", app_id, portal)

    # AI trap check — must run before any fills
    trap = detect_traps_in_html(html)
    if trap.trap_found:
        logger.warning(
            "AI trap in form HTML app_id=%d type=%s — aborting prefill",
            app_id, trap.trap_type,
        )
        return PrefillResult(
            success=False, portal=portal, fields_filled=0,
            ai_trap_detected=True, stopped_at_submit=False,
            errors=[f"AI trap detected: {trap.trap_type}: {trap.evidence[:80]}"],
        )

    try:
        engine = StaticFillEngine(html, fill_delay=fill_delay)
    except CaptchaDetected:
        logger.warning("CAPTCHA detected in static HTML for app_id=%d", app_id)
        return PrefillResult(
            success=False, portal=portal, fields_filled=0,
            captcha_detected=True, stopped_at_submit=False,
        )
    except MFADetected:
        logger.warning("MFA detected in static HTML for app_id=%d", app_id)
        return PrefillResult(
            success=False, portal=portal, fields_filled=0,
            mfa_detected=True, stopped_at_submit=False,
        )

    # Screenshot before
    ss_before = os.path.join(folder_path, "prefill_before.txt")
    engine.take_screenshot(ss_before)

    # Build and execute fill instructions
    instructions = build_fill_instructions(answers, portal)
    errors: list[str] = []
    for inst in instructions:
        try:
            engine.fill_field(inst)
        except Exception as exc:
            msg = f"Failed to fill {inst.label!r}: {exc}"
            logger.error(msg)
            errors.append(msg)

    # Screenshot after
    ss_after = os.path.join(folder_path, "prefill_after.txt")
    engine.take_screenshot(ss_after)

    # Verify we stopped at submit screen (never clicked submit)
    at_submit = engine.is_at_submit_screen
    logger.info(
        "prefill complete app_id=%d fields=%d stopped_at_submit=%s",
        app_id, len(engine.filled), at_submit,
    )

    return PrefillResult(
        success=len(errors) == 0,
        portal=portal,
        fields_filled=len(engine.filled),
        screenshot_before=ss_before,
        screenshot_after=ss_after,
        stopped_at_submit=at_submit,
        errors=errors,
    )


def _prefill_playwright(
    app_id: int,
    answers: dict,
    url: str,
    folder_path: str,
    fill_delay: float,
) -> PrefillResult:
    """
    Runtime Playwright fill path.
    Connects to an existing authenticated browser session.
    Lazy-imported so tests never touch Playwright.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        logger.error("playwright not installed — cannot run live prefill")
        return PrefillResult(
            success=False, portal="unknown", fields_filled=0,
            errors=["playwright not installed"],
        )

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        if url:
            page.goto(url)

        html = page.content()
        portal = classify_portal(html=html, url=url)

        # AI trap check
        trap = detect_traps_in_html(html)
        if trap.trap_found:
            logger.warning(
                "AI trap in form HTML app_id=%d type=%s — aborting prefill",
                app_id, trap.trap_type,
            )
            return PrefillResult(
                success=False, portal=portal, fields_filled=0,
                ai_trap_detected=True, stopped_at_submit=False,
                errors=[f"AI trap detected: {trap.trap_type}: {trap.evidence[:80]}"],
            )

        # CAPTCHA/MFA check
        for cid in StaticFillEngine.CAPTCHA_IDS:
            if page.locator(f"#{cid}").count() > 0:
                raise CaptchaDetected(f"CAPTCHA element #{cid} found")
        for mid in StaticFillEngine.MFA_IDS:
            if page.locator(f"#{mid}").count() > 0:
                raise MFADetected(f"MFA element #{mid} found")

        # Screenshot before
        ss_before = os.path.join(folder_path, "prefill_before.png")
        page.screenshot(path=ss_before)

        instructions = build_fill_instructions(answers, portal)
        errors = []
        for inst in instructions:
            try:
                el = page.locator(inst.selector).first
                if inst.field_type == "select":
                    el.select_option(inst.value)
                elif inst.field_type in ("text", "textarea"):
                    el.fill(inst.value)
                elif inst.field_type == "checkbox" and inst.value.lower() in ("1", "true", "yes"):
                    el.check()
                time.sleep(fill_delay)
            except Exception as exc:
                msg = f"Failed to fill {inst.label!r}: {exc}"
                logger.error(msg)
                errors.append(msg)

        # Screenshot after — parked at submit screen
        ss_after = os.path.join(folder_path, "prefill_after.png")
        page.screenshot(path=ss_after)

        return PrefillResult(
            success=len(errors) == 0,
            portal=portal,
            fields_filled=len(instructions) - len(errors),
            screenshot_before=ss_before,
            screenshot_after=ss_after,
            stopped_at_submit=True,   # we never call page.click(submit)
            errors=errors,
        )
