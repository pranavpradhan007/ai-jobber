"""
Unit tests for Sprint 3: JD parser, keyword extractor, scorer, score gate.
All LLM calls mocked.
"""
import os
import json
import pytest

from src.parsing.jd_parser import parse_jd
from src.keywords.extractor import extract_keywords, KeywordReport
from src.scoring.scorer import score_job, Scorecard, SCORE_THRESHOLD
from src.scoring.gate import apply_score_gate, _assign_tier
from src.db.jobs import create_job
from src.db.applications import create_application, get_application
from src.queues.queue import pending_count

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

# ── Mock scorers ─────────────────────────────────────────────────────────────

def scorer_high(job_info, profile_info):
    return {
        "title_match":         {"score": 90, "reason": "exact match"},
        "must_have_skills":    {"score": 85, "reason": "all present"},
        "nice_to_have_skills": {"score": 70, "reason": "mostly present"},
        "industry_fit":        {"score": 80, "reason": "same domain"},
        "location_match":      {"score": 100, "reason": "remote ok"},
        "salary_fit":          {"score": 75, "reason": "within range"},
    }

def scorer_low(job_info, profile_info):
    return {
        "title_match":         {"score": 20, "reason": "wrong level"},
        "must_have_skills":    {"score": 15, "reason": "missing skills"},
        "nice_to_have_skills": {"score": 10, "reason": "none"},
        "industry_fit":        {"score": 25, "reason": "different domain"},
        "location_match":      {"score": 50, "reason": "partial"},
        "salary_fit":          {"score": 30, "reason": "below range"},
    }

def scorer_exactly_threshold(job_info, profile_info):
    # Returns exactly 60.0 total
    # 60 * 0.25 + 60 * 0.30 + 60 * 0.15 + 60 * 0.15 + 60 * 0.10 + 60 * 0.05 = 60
    return {d: {"score": 60, "reason": "at threshold"} for d in
            ["title_match","must_have_skills","nice_to_have_skills",
             "industry_fit","location_match","salary_fit"]}


# ── 1. JD parser ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_jd_produces_clean_and_hash():
    raw = "  <b>Job Title: Senior Python Developer</b>\n  Location: Remote  \n  We need Python & PostgreSQL."
    parsed = parse_jd(raw)
    assert parsed.clean_jd
    assert "<b>" not in parsed.clean_jd
    assert "python" in parsed.clean_jd.lower()
    assert len(parsed.jd_hash) == 64  # SHA-256 hex


@pytest.mark.unit
def test_parse_jd_hash_is_deterministic():
    raw = "We need Python developers."
    a = parse_jd(raw)
    b = parse_jd(raw)
    assert a.jd_hash == b.jd_hash


@pytest.mark.unit
def test_parse_jd_different_text_different_hash():
    a = parse_jd("Python developer needed.")
    b = parse_jd("Java developer needed.")
    assert a.jd_hash != b.jd_hash


@pytest.mark.unit
def test_parse_jd_strips_html_entities():
    raw = "Salary &amp; benefits. We use Python &amp; React."
    parsed = parse_jd(raw)
    assert "&amp;" not in parsed.clean_jd
    assert "&" in parsed.clean_jd


@pytest.mark.unit
def test_parse_jd_extracts_title_hint():
    raw = "Job Title: Staff Machine Learning Engineer\nWe want Python skills."
    parsed = parse_jd(raw)
    assert parsed.title_hint is not None
    assert "Machine Learning" in parsed.title_hint


# ── 2. Keyword extractor ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_keywords_finds_bank_items(seeded_db):
    jd = "Looking for Python and PostgreSQL expertise with REST API design."
    report = extract_keywords(seeded_db, jd)
    kws = [m.keyword for m in report.matches]
    assert "Python" in kws
    assert "PostgreSQL" in kws
    assert "REST API design" in kws


@pytest.mark.unit
def test_extract_keywords_excludes_private(seeded_db):
    jd = "We need a Staff Engineer with Python skills."
    report = extract_keywords(seeded_db, jd)
    kws = [m.keyword for m in report.matches]
    assert "Staff Engineer" not in kws


@pytest.mark.unit
def test_extract_keywords_with_mock_extractor(seeded_db):
    jd = "Must know machine learning and data pipelines."
    extra_extractor = lambda text: ["data pipelines", "machine learning"]
    report = extract_keywords(seeded_db, jd, extractor=extra_extractor)
    kws_lower = [m.keyword.lower() for m in report.matches]
    assert "machine learning" in kws_lower


# ── 3. Fit scorer ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_scorer_high_passes_threshold():
    scorecard = score_job({}, {}, scorer=scorer_high)
    assert scorecard.passed_threshold is True
    assert scorecard.total_score >= SCORE_THRESHOLD
    assert scorecard.summary_reason.startswith("APPLY")


@pytest.mark.unit
def test_scorer_low_fails_threshold():
    scorecard = score_job({}, {}, scorer=scorer_low)
    assert scorecard.passed_threshold is False
    assert scorecard.total_score < SCORE_THRESHOLD
    assert scorecard.summary_reason.startswith("SKIP")


@pytest.mark.unit
def test_scorer_exactly_threshold_passes():
    scorecard = score_job({}, {}, scorer=scorer_exactly_threshold)
    assert scorecard.passed_threshold is True
    assert abs(scorecard.total_score - 60.0) < 0.01


@pytest.mark.unit
def test_scorecard_has_all_dimensions():
    scorecard = score_job({}, {}, scorer=scorer_high)
    dim_names = {d.name for d in scorecard.dimensions}
    expected = {"title_match","must_have_skills","nice_to_have_skills",
                "industry_fit","location_match","salary_fit"}
    assert dim_names == expected


@pytest.mark.unit
def test_scorecard_weights_sum_to_one():
    scorecard = score_job({}, {}, scorer=scorer_high)
    total_weight = sum(d.weight for d in scorecard.dimensions)
    assert abs(total_weight - 1.0) < 1e-9


@pytest.mark.unit
def test_scorecard_is_explainable():
    scorecard = score_job({}, {}, scorer=scorer_high)
    assert scorecard.summary_reason
    for d in scorecard.dimensions:
        assert d.reason  # every dimension has a reason


# ── 4. Score gate ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_score_gate_passes_high_score(seeded_db):
    job = create_job(seeded_db, source="test", url="https://sg.test/1",
                     platform="workday")
    app = create_application(seeded_db, job_id=job.id)
    sc = score_job({}, {}, scorer=scorer_high)
    result = apply_score_gate(seeded_db, app.id, sc,
                              platform="workday", has_screener=0)
    assert result["should_apply"] == 1
    assert result["submit_tier"] == "gated"

    saved = get_application(seeded_db, app.id)
    assert saved.should_apply == 1


@pytest.mark.unit
def test_score_gate_fails_low_score_no_enqueue(seeded_db):
    job = create_job(seeded_db, source="test", url="https://sg.test/2")
    app = create_application(seeded_db, job_id=job.id)
    sc = score_job({}, {}, scorer=scorer_low)
    result = apply_score_gate(seeded_db, app.id, sc, platform="email")
    assert result["should_apply"] == 0

    saved = get_application(seeded_db, app.id)
    assert saved.should_apply == 0


# ── 5. submit_tier assignment ─────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("platform,has_screener,login_req,expected_tier,expected_auto", [
    ("email",      0, 0, "auto_safe", 1),
    ("api",        0, 0, "auto_safe", 1),
    ("email",      1, 0, "auto_safe", 1),   # email overrides screener
    ("workday",    0, 0, "gated",     0),
    ("greenhouse", 0, 0, "gated",     0),
    ("lever",      0, 0, "gated",     0),
    ("ashby",      0, 0, "gated",     0),
    ("custom",     0, 0, "gated",     0),
    ("custom",     1, 0, "gated",     0),
    ("custom",     0, 1, "gated",     0),
    ("unknown",    0, 0, "gated",     0),   # default to gated for safety
])
def test_assign_tier(platform, has_screener, login_req, expected_tier, expected_auto):
    tier, auto = _assign_tier(platform, has_screener, login_req)
    assert tier == expected_tier
    assert auto == expected_auto


@pytest.mark.unit
def test_screener_job_is_gated(seeded_db):
    job = create_job(seeded_db, source="test", url="https://sg.test/3",
                     platform="workday", has_screener=1)
    app = create_application(seeded_db, job_id=job.id)
    sc = score_job({}, {}, scorer=scorer_high)
    result = apply_score_gate(seeded_db, app.id, sc,
                              platform="workday", has_screener=1)
    assert result["submit_tier"] == "gated"
    assert result["auto_safe"] == 0


@pytest.mark.unit
def test_email_apply_is_auto_safe(seeded_db):
    job = create_job(seeded_db, source="test", url="https://sg.test/4",
                     platform="email")
    app = create_application(seeded_db, job_id=job.id)
    sc = score_job({}, {}, scorer=scorer_high)
    result = apply_score_gate(seeded_db, app.id, sc, platform="email")
    assert result["submit_tier"] == "auto_safe"
    assert result["auto_safe"] == 1
