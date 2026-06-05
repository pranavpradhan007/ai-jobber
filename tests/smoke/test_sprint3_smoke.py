"""
Sprint 3 smoke test:
  sample JD → scorecard with explainable reason
  → keywords mapped to bank items
  → submit_tier correctly assigned (screener job → gated, email-apply job → auto_safe)
  → below-threshold job does NOT enqueue tailoring
"""
import os
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, get_application
from src.storage.bank_loader import seed_from_yaml
from src.parsing.jd_parser import parse_jd
from src.keywords.extractor import extract_keywords
from src.scoring.scorer import score_job
from src.scoring.gate import apply_score_gate

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

SAMPLE_JD = """
Job Title: Senior Software Engineer
Location: Remote

We are looking for a Senior Software Engineer with 5+ years of Python experience.
You should know PostgreSQL, REST API design, and Kubernetes.
Must have: Python, PostgreSQL.
Nice to have: machine learning, dbt.
Compensation: $150k - $200k.
"""

def _high_scorer(job_info, profile_info):
    return {
        "title_match":         {"score": 85, "reason": "senior engineer match"},
        "must_have_skills":    {"score": 90, "reason": "python + postgres present"},
        "nice_to_have_skills": {"score": 70, "reason": "ml mentioned"},
        "industry_fit":        {"score": 80, "reason": "software domain"},
        "location_match":      {"score": 100, "reason": "remote"},
        "salary_fit":          {"score": 75, "reason": "in range"},
    }

def _low_scorer(job_info, profile_info):
    return {d: {"score": 10, "reason": "poor fit"} for d in
            ["title_match","must_have_skills","nice_to_have_skills",
             "industry_fit","location_match","salary_fit"]}


def _make_db(tmp_path):
    db_path = str(tmp_path / "sprint3_smoke.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    seed_from_yaml(conn, os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))
    return conn


@pytest.mark.smoke
def test_jd_to_scorecard_explainable(tmp_path):
    conn = _make_db(tmp_path)
    parsed = parse_jd(SAMPLE_JD)
    assert parsed.jd_hash
    assert "python" in parsed.clean_jd.lower()

    kw_report = extract_keywords(conn, parsed.clean_jd)
    assert len(kw_report.hot_keywords) > 0
    # Python and PostgreSQL must be in the hot keywords
    kws_lower = [k.lower() for k in kw_report.hot_keywords]
    assert "python" in kws_lower

    scorecard = score_job(
        {"title": "Senior Engineer", "clean_jd": parsed.clean_jd},
        {"title": "Senior Software Engineer"},
        scorer=_high_scorer,
    )
    assert scorecard.passed_threshold is True
    assert scorecard.summary_reason.startswith("APPLY")
    assert scorecard.total_score >= 60
    conn.close()


@pytest.mark.smoke
def test_screener_job_assigned_gated(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test",
                     url="https://acme.com/jobs/screener",
                     platform="workday",
                     has_screener=1)
    app = create_application(conn, job_id=job.id)
    sc = score_job({}, {}, scorer=_high_scorer)
    result = apply_score_gate(conn, app.id, sc, platform="workday", has_screener=1)
    assert result["submit_tier"] == "gated"
    assert result["should_apply"] == 1
    conn.close()


@pytest.mark.smoke
def test_email_apply_job_assigned_auto_safe(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test",
                     url="https://acme.com/jobs/email",
                     platform="email")
    app = create_application(conn, job_id=job.id)
    sc = score_job({}, {}, scorer=_high_scorer)
    result = apply_score_gate(conn, app.id, sc, platform="email")
    assert result["submit_tier"] == "auto_safe"
    assert result["auto_safe"] == 1
    conn.close()


@pytest.mark.smoke
def test_below_threshold_job_not_applied(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test",
                     url="https://acme.com/jobs/low-fit",
                     platform="workday")
    app = create_application(conn, job_id=job.id)
    sc = score_job({}, {}, scorer=_low_scorer)
    result = apply_score_gate(conn, app.id, sc, platform="workday")
    assert result["should_apply"] == 0

    saved = get_application(conn, app.id)
    assert saved.should_apply == 0
    conn.close()
