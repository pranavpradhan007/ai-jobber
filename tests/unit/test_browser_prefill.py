"""
Unit tests for Sprint 7: portal classifier, field mapper, pre-fill engine.
All tests operate on saved fixture HTML — no live portals, no Playwright.
"""
import os
import pytest

from src.browser.portal import classify_portal
from src.browser.field_mapper import (
    build_fill_instructions,
    discover_fields_from_html,
)
from src.browser.prefill import (
    prefill_application,
    StaticFillEngine,
    CaptchaDetected,
    MFADetected,
    PrefillResult,
)
from src.db.jobs import create_job
from src.db.applications import create_application, update_application
from src.db.state_machine import transition

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "portal_html")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


SAMPLE_ANSWERS = {
    "first_name":         "Jane",
    "last_name":          "Doe",
    "email":              "jane.doe@example.com",
    "phone":              "+1-555-0100",
    "linkedin_url":       "https://linkedin.com/in/janedoe",
    "years_experience":   "6-10",
    "work_authorization": "citizen",
    "cover_letter":       "I am excited to apply for this role.",
    "location":           "San Francisco, CA",
}


def _make_gated_app(conn, url="https://portal.test/job/1"):
    job = create_job(conn, source="test", url=url,
                     platform="workday", has_screener=1)
    app = create_application(conn, job_id=job.id)
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING",
               "READY_TO_SUBMIT", "WAITING_FOR_USER_APPROVAL"]:
        transition(conn, app.id, to)
    update_application(conn, app.id, verifier_passed=1)
    return app


# ── 1. Portal classifier ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_classify_workday_by_html():
    html = _read_fixture("workday_form.html")
    assert classify_portal(html=html) == "workday"


@pytest.mark.unit
def test_classify_greenhouse_by_html():
    html = _read_fixture("greenhouse_form.html")
    assert classify_portal(html=html) == "greenhouse"


@pytest.mark.unit
def test_classify_workday_by_url():
    assert classify_portal(url="https://company.wd1.myworkdayjobs.com/apply") == "workday"


@pytest.mark.unit
def test_classify_greenhouse_by_url():
    assert classify_portal(url="https://boards.greenhouse.io/company/jobs/123") == "greenhouse"


@pytest.mark.unit
def test_classify_lever_by_url():
    assert classify_portal(url="https://jobs.lever.co/company/abc") == "lever"


@pytest.mark.unit
def test_classify_ashby_by_url():
    assert classify_portal(url="https://jobs.ashbyhq.com/company/role") == "ashby"


@pytest.mark.unit
def test_classify_unknown_is_custom():
    assert classify_portal(html="<html><body><form></form></body></html>") == "custom"


# ── 2. Field discovery from HTML ─────────────────────────────────────────────

@pytest.mark.unit
def test_discover_fields_workday():
    html = _read_fixture("workday_form.html")
    fields = discover_fields_from_html(html)
    ids = {f["id"] for f in fields}
    assert "firstName" in ids
    assert "lastName" in ids
    assert "email" in ids
    assert "yearsExperience" in ids


@pytest.mark.unit
def test_discover_fields_greenhouse():
    html = _read_fixture("greenhouse_form.html")
    fields = discover_fields_from_html(html)
    ids = {f["id"] for f in fields}
    assert "first_name" in ids
    assert "email" in ids
    assert "cover_letter" in ids


# ── 3. Field mapper ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_build_fill_instructions_workday():
    instructions = build_fill_instructions(SAMPLE_ANSWERS, "workday")
    labels = {i.label for i in instructions}
    assert "first_name" in labels
    assert "email" in labels
    # resume should not appear (handled separately)
    assert "resume" not in labels


@pytest.mark.unit
def test_build_fill_instructions_greenhouse():
    instructions = build_fill_instructions(SAMPLE_ANSWERS, "greenhouse")
    selectors = {i.selector for i in instructions}
    assert "#first_name" in selectors
    assert "#email" in selectors


@pytest.mark.unit
def test_fill_instructions_have_correct_types():
    instructions = build_fill_instructions(SAMPLE_ANSWERS, "workday")
    by_label = {i.label: i for i in instructions}
    assert by_label["first_name"].field_type == "text"
    assert by_label["years_experience"].field_type == "select"
    assert by_label["cover_letter"].field_type == "textarea"


# ── 4. Static fill engine ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_static_engine_fills_fields(tmp_path):
    html = _read_fixture("workday_form.html")
    engine = StaticFillEngine(html, fill_delay=0.0)
    instructions = build_fill_instructions(SAMPLE_ANSWERS, "workday")
    for inst in instructions:
        engine.fill_field(inst)
    assert len(engine.filled) == len(instructions)
    labels = [f["label"] for f in engine.filled]
    assert "first_name" in labels
    assert "email" in labels


@pytest.mark.unit
def test_static_engine_detects_submit_button():
    html = _read_fixture("workday_form.html")
    engine = StaticFillEngine(html, fill_delay=0.0)
    assert engine.is_at_submit_screen is True


@pytest.mark.unit
def test_static_engine_raises_on_captcha():
    html = _read_fixture("captcha_form.html")
    with pytest.raises(CaptchaDetected):
        StaticFillEngine(html, fill_delay=0.0)


# ── 5. prefill_application — happy path ──────────────────────────────────────

@pytest.mark.unit
def test_prefill_workday_fills_and_stops(seeded_db, tmp_path):
    app = _make_gated_app(seeded_db, url="https://portal.test/wd/1")
    update_application(seeded_db, app.id, folder_path=str(tmp_path).replace("\\", "/"))

    html = _read_fixture("workday_form.html")
    result = prefill_application(
        seeded_db, app.id, SAMPLE_ANSWERS,
        html=html,
        folder_path=str(tmp_path),
    )

    assert result.success is True
    assert result.portal == "workday"
    assert result.fields_filled > 0
    assert result.stopped_at_submit is True   # parked, never submitted
    assert result.captcha_detected is False
    assert os.path.exists(result.screenshot_before)
    assert os.path.exists(result.screenshot_after)


@pytest.mark.unit
def test_prefill_greenhouse_fills_and_stops(seeded_db, tmp_path):
    app = _make_gated_app(seeded_db, url="https://boards.greenhouse.io/co/1")
    update_application(seeded_db, app.id, folder_path=str(tmp_path).replace("\\", "/"))

    html = _read_fixture("greenhouse_form.html")
    result = prefill_application(
        seeded_db, app.id, SAMPLE_ANSWERS,
        html=html,
        url="https://boards.greenhouse.io/co/1",
        folder_path=str(tmp_path),
    )

    assert result.success is True
    assert result.portal == "greenhouse"
    assert result.fields_filled > 0
    assert result.stopped_at_submit is True


# ── 6. CAPTCHA fixture triggers hand-off, not a solve attempt ────────────────

@pytest.mark.unit
def test_prefill_captcha_returns_handoff_result(seeded_db, tmp_path):
    app = _make_gated_app(seeded_db, url="https://portal.test/captcha/1")
    update_application(seeded_db, app.id, folder_path=str(tmp_path).replace("\\", "/"))

    html = _read_fixture("captcha_form.html")
    result = prefill_application(
        seeded_db, app.id, SAMPLE_ANSWERS,
        html=html,
        folder_path=str(tmp_path),
    )

    assert result.success is False
    assert result.captcha_detected is True
    assert result.fields_filled == 0
    # Must NOT have attempted to submit
    assert result.stopped_at_submit is False


# ── 7. Never auto-submits a gated job ────────────────────────────────────────

@pytest.mark.unit
def test_prefill_never_submits(seeded_db, tmp_path):
    """
    The pre-fill result must always have stopped_at_submit=True on success.
    There must be no code path that clicks the submit button.
    """
    app = _make_gated_app(seeded_db, url="https://portal.test/wd/nosub")
    update_application(seeded_db, app.id, folder_path=str(tmp_path).replace("\\", "/"))

    html = _read_fixture("workday_form.html")
    result = prefill_application(
        seeded_db, app.id, SAMPLE_ANSWERS,
        html=html,
        folder_path=str(tmp_path),
    )
    # Core invariant: we parked at submit, not past it
    assert result.stopped_at_submit is True
    # Verify no submit was recorded in filled fields
    if hasattr(result, '_engine'):
        assert not any(f["selector"] == "#submitApplication"
                       for f in result._engine.filled)


# ── 8. No evasion code exists ─────────────────────────────────────────────────

@pytest.mark.unit
def test_no_evasion_code_in_prefill_module():
    """
    Security check: prefill.py must not contain evasion-related keywords.
    """
    prefill_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "browser", "prefill.py"
    )
    with open(prefill_path, encoding="utf-8") as fh:
        source = fh.read().lower()

    evasion_terms = [
        "stealth", "puppeteer-extra", "random_delay", "jitter",
        "fingerprint", "webgl", "canvas_spoof", "navigator.webdriver = false",
    ]
    found = [t for t in evasion_terms if t in source]
    assert not found, f"Evasion code found in prefill.py: {found}"
