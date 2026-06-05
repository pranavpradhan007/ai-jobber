"""
Sprint 7 smoke:
  - Fill Workday fixture HTML → stopped at submit screen, screenshots captured
  - Fill Greenhouse fixture HTML → fields filled, stopped at submit
  - CAPTCHA fixture → hand-off result, not a solve attempt
  - Verify no anti-bot evasion code exists in src/browser/
"""
import os
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, update_application
from src.db.state_machine import transition
from src.storage.bank_loader import seed_from_yaml
from src.browser.prefill import prefill_application

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "portal_html")
FIXTURE_BANK = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                             "sample_source_bank.yaml")

ANSWERS = {
    "first_name": "Alice", "last_name": "Smith",
    "email": "alice@example.com", "phone": "+1-555-9999",
    "years_experience": "6-10", "work_authorization": "citizen",
    "cover_letter": "Applying for the role.",
    "location": "Remote",
}


def _setup(tmp_path):
    db_path = str(tmp_path / "sprint7.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    seed_from_yaml(conn, FIXTURE_BANK)
    return conn


def _gated_app(conn, url, platform="workday"):
    job = create_job(conn, source="test", url=url, platform=platform,
                     has_screener=1)
    app = create_application(conn, job_id=job.id)
    for to in ["SCORED","TAILORING","RESUME_VERIFIED","PACKAGING",
               "READY_TO_SUBMIT","WAITING_FOR_USER_APPROVAL"]:
        transition(conn, app.id, to)
    update_application(conn, app.id, verifier_passed=1)
    return app


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.smoke
def test_workday_fill_stops_at_submit(tmp_path):
    conn = _setup(tmp_path)
    app = _gated_app(conn, "https://acme.wd1.myworkdayjobs.com/apply/1")
    folder = str(tmp_path / "app_folder")
    os.makedirs(folder, exist_ok=True)

    result = prefill_application(
        conn, app.id, ANSWERS,
        html=_read("workday_form.html"),
        folder_path=folder,
    )

    assert result.success is True
    assert result.portal == "workday"
    assert result.fields_filled > 0
    assert result.stopped_at_submit is True
    assert os.path.exists(result.screenshot_before)
    assert os.path.exists(result.screenshot_after)
    conn.close()


@pytest.mark.smoke
def test_greenhouse_fill_stops_at_submit(tmp_path):
    conn = _setup(tmp_path)
    app = _gated_app(conn, "https://boards.greenhouse.io/co/jobs/2",
                     platform="greenhouse")
    folder = str(tmp_path / "gh_folder")
    os.makedirs(folder, exist_ok=True)

    result = prefill_application(
        conn, app.id, ANSWERS,
        html=_read("greenhouse_form.html"),
        url="https://boards.greenhouse.io/co/jobs/2",
        folder_path=folder,
    )

    assert result.success is True
    assert result.portal == "greenhouse"
    assert result.stopped_at_submit is True
    conn.close()


@pytest.mark.smoke
def test_captcha_triggers_handoff_not_solve(tmp_path):
    conn = _setup(tmp_path)
    app = _gated_app(conn, "https://portal.test/captcha-job")
    folder = str(tmp_path / "cap_folder")
    os.makedirs(folder, exist_ok=True)

    result = prefill_application(
        conn, app.id, ANSWERS,
        html=_read("captcha_form.html"),
        folder_path=folder,
    )

    assert result.captcha_detected is True
    assert result.success is False
    assert result.fields_filled == 0
    conn.close()


@pytest.mark.smoke
def test_no_evasion_code_in_browser_module():
    """Security confirms no anti-bot evasion code in src/browser/."""
    browser_dir = os.path.join(REPO_ROOT, "src", "browser")
    evasion_terms = ["stealth", "jitter", "fingerprint", "webgl",
                     "navigator.webdriver = false", "puppeteer-extra"]
    violations = []
    for fname in os.listdir(browser_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(browser_dir, fname)
        with open(fpath, encoding="utf-8") as fh:
            src = fh.read().lower()
        for term in evasion_terms:
            if term in src:
                violations.append(f"{fname}: {term!r}")
    assert not violations, f"Evasion code found: {violations}"
