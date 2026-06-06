"""
Human-like browser interaction utilities.

All interactions mimic real user behaviour:
  - Mouse follows a random cubic Bezier curve to the target
  - Typing speed varies by field type (instant fill for short fields,
    realistic WPM for long text)
  - Pauses are always random ranges, never fixed
  - SPA-friendly: fires input/change events after fill for React/Vue apps

Used by fast_autofill.py, auto_submit.py, and linkedin_apply.py.
"""
from __future__ import annotations
import random
import time
from typing import Optional


# ---------------------------------------------------------------------------
# Mouse movement
# ---------------------------------------------------------------------------

def human_move_and_click(page, locator) -> None:
    """
    Move mouse to element along a Bezier curve, then click.
    Falls back to direct locator.click() on any error.
    """
    try:
        box = locator.bounding_box()
        if box is None:
            locator.click()
            return

        # Aim slightly off-centre — humans don't always hit the exact middle
        tx = box["x"] + box["width"]  * random.uniform(0.25, 0.75)
        ty = box["y"] + box["height"] * random.uniform(0.25, 0.75)

        _bezier_move(page, tx, ty)
        time.sleep(random.uniform(0.05, 0.18))
        page.mouse.click(tx, ty)
    except Exception:
        try:
            locator.click()
        except Exception:
            pass


def _bezier_move(page, end_x: float, end_y: float) -> None:
    """Move mouse from a pseudo-random start to (end_x, end_y) via cubic Bezier."""
    vp = page.viewport_size or {"width": 1280, "height": 800}
    sx = vp["width"]  * random.uniform(0.2, 0.8)
    sy = vp["height"] * random.uniform(0.15, 0.55)

    # Random control points — create a natural curved path
    dx, dy = end_x - sx, end_y - sy
    cp1x = sx + dx * random.uniform(0.1, 0.45) + random.uniform(-60, 60)
    cp1y = sy + dy * random.uniform(0.1, 0.45) + random.uniform(-40, 40)
    cp2x = sx + dx * random.uniform(0.55, 0.9) + random.uniform(-60, 60)
    cp2y = sy + dy * random.uniform(0.55, 0.9) + random.uniform(-40, 40)

    steps = random.randint(16, 28)
    for i in range(steps + 1):
        t = i / steps
        # Ease-in-out so the cursor accelerates then decelerates
        t_e = t * t * (3 - 2 * t)
        x, y = _cubic_bezier(t_e, sx, sy, cp1x, cp1y, cp2x, cp2y, end_x, end_y)
        page.mouse.move(x, y)
        # Slower at the start and end, faster in the middle
        if i < 4 or i > steps - 4:
            time.sleep(random.uniform(0.009, 0.022))
        else:
            time.sleep(random.uniform(0.003, 0.010))


def _cubic_bezier(t, x0, y0, x1, y1, x2, y2, x3, y3):
    mt = 1 - t
    x = mt**3*x0 + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x3
    y = mt**3*y0 + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y3
    return x, y


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

def human_scroll_to(page, locator) -> None:
    """Scroll element into view with a small settle pause."""
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
        time.sleep(random.uniform(0.08, 0.22))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Field filling
# ---------------------------------------------------------------------------

def human_fill(page, locator, value: str, *, long_text: bool = False) -> None:
    """
    Fill a text field with human-like behaviour.

    Short fields (name, email, phone, URL):
        scroll → Bezier move → click → locator.fill() → dispatch events
        Delay after: 0.3-1.0 s

    Long text (cover letter, open-ended screener answer):
        scroll → click → clear → type in randomised chunks at ~60 WPM
        Occasional micro-pause (human "thinking")
        Delay after: 0.5-1.5 s
    """
    if not value:
        return
    try:
        human_scroll_to(page, locator)
        human_move_and_click(page, locator)
        time.sleep(random.uniform(0.06, 0.16))

        if long_text and len(value) > 60:
            # Realistic typing for long answers
            locator.fill("")                     # clear first
            pos = 0
            while pos < len(value):
                chunk_len = random.randint(4, 10)
                chunk = value[pos: pos + chunk_len]
                locator.type(chunk, delay=random.randint(38, 115))
                pos += chunk_len
                if random.random() < 0.12:       # occasional thinking pause
                    time.sleep(random.uniform(0.25, 0.75))
        else:
            locator.fill(value)
            _dispatch_events(page, locator)

        time.sleep(random.uniform(0.3, 1.0) if not long_text else random.uniform(0.5, 1.5))

    except Exception:
        try:
            locator.fill(value)
        except Exception:
            pass


def human_fill_select(page, locator, value: str) -> bool:
    """
    Select a dropdown option with human-like click + option selection.
    Returns True on success.
    """
    try:
        human_scroll_to(page, locator)
        human_move_and_click(page, locator)
        time.sleep(random.uniform(0.15, 0.35))
        try:
            locator.select_option(value)
            return True
        except Exception:
            try:
                locator.select_option(label=value)
                return True
            except Exception:
                return _fuzzy_select(locator, value)
    except Exception:
        return False


def human_click_radio(page, locator) -> None:
    """Click a radio button with mouse movement."""
    try:
        human_scroll_to(page, locator)
        human_move_and_click(page, locator)
        time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        try:
            locator.click()
        except Exception:
            pass


def human_check_checkbox(locator, check: bool = True) -> None:
    """Check or uncheck a checkbox."""
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
        time.sleep(random.uniform(0.1, 0.25))
        if check:
            locator.check()
        else:
            locator.uncheck()
        time.sleep(random.uniform(0.15, 0.4))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pauses (always random ranges)
# ---------------------------------------------------------------------------

def reading_pause() -> None:
    """Simulate human reading a new page (1.5-3.5 s)."""
    time.sleep(random.uniform(1.5, 3.5))


def inter_field_pause() -> None:
    """Natural pause between adjacent fields (0.3-1.1 s)."""
    time.sleep(random.uniform(0.3, 1.1))


def page_transition_pause() -> None:
    """Pause after advancing a multi-step form (0.7-1.8 s)."""
    time.sleep(random.uniform(0.7, 1.8))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dispatch_events(page, locator) -> None:
    """Fire input + change events for React/Vue SPA compatibility."""
    try:
        handle = locator.element_handle(timeout=500)
        if handle:
            page.evaluate(
                "(el) => {"
                "  el.dispatchEvent(new Event('input',  {bubbles: true}));"
                "  el.dispatchEvent(new Event('change', {bubbles: true}));"
                "}",
                handle,
            )
    except Exception:
        pass


def _fuzzy_select(locator, value: str) -> bool:
    """Select option whose text has the highest word-overlap with value."""
    try:
        options = locator.evaluate(
            "(el) => Array.from(el.options).map(o => o.text.trim())"
        )
        if not options:
            return False
        val_words = set(value.lower().split())
        best_opt, best_score = "", 0
        for opt in options:
            score = len(val_words & set(opt.lower().split()))
            if score > best_score:
                best_score, best_opt = score, opt
        if best_opt and best_score > 0:
            locator.select_option(label=best_opt)
            return True
    except Exception:
        pass
    return False
