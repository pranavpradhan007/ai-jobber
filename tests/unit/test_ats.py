"""
Unit tests for src/ats/scanner.py
"""
import pytest
from src.ats.scanner import ats_scan, ats_report_text, ATSScanResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resume(bullets: list[str], header: str = "Jane Doe\njane@example.com") -> str:
    parts = [header, ""]
    for b in bullets:
        parts.append(f"- {b}")
    return "\n".join(parts)


SHORT_RESUME = _resume([
    "Built a PyTorch image classifier trained on 50k samples with 94% accuracy.",
    "Deployed ML models to AWS SageMaker; reduced inference latency by 40%.",
    "Trained BERT fine-tuned on 10k NLP documents; improved F1 from 0.71 to 0.85.",
    "Designed a Python data pipeline processing 1M rows/day with 99.9% uptime.",
])

LONG_RESUME = _resume([
    f"Built system {i} that reduced latency by {i*5}% using Python and PyTorch."
    for i in range(1, 65)  # 64 bullets — well over 1 page (55-line budget)
])


# ---------------------------------------------------------------------------
# keyword_coverage
# ---------------------------------------------------------------------------

def test_keyword_coverage_full():
    result = ats_scan(SHORT_RESUME, ["PyTorch", "Python", "BERT"])
    assert result.keyword_coverage == pytest.approx(1.0)


def test_keyword_coverage_partial():
    result = ats_scan(SHORT_RESUME, ["PyTorch", "TensorFlow", "Kubernetes"])
    # Only PyTorch is in the resume
    assert result.keyword_coverage == pytest.approx(1 / 3, abs=0.01)


def test_keyword_coverage_empty_keywords():
    result = ats_scan(SHORT_RESUME, [])
    assert result.keyword_coverage == pytest.approx(1.0)   # edge: 0/0 → 1


def test_gap_keywords():
    result = ats_scan(SHORT_RESUME, ["PyTorch", "TensorFlow", "JAX"])
    assert "TensorFlow" in result.gap_keywords
    assert "JAX" in result.gap_keywords
    assert "PyTorch" not in result.gap_keywords


# ---------------------------------------------------------------------------
# page_count
# ---------------------------------------------------------------------------

def test_page_count_under_limit():
    result = ats_scan(SHORT_RESUME, [])
    assert result.page_count_est <= 1.0


def test_page_count_over_limit():
    result = ats_scan(LONG_RESUME, [])
    assert result.page_count_est > 1.0


def test_page_limit_fails_long_resume():
    result = ats_scan(LONG_RESUME, [])
    assert result.passed is False
    assert any("page" in issue.lower() for issue in result.issues)


# ---------------------------------------------------------------------------
# action_verb_rate
# ---------------------------------------------------------------------------

def test_action_verb_rate_high():
    resume = _resume([
        "Built a classifier with 95% accuracy.",
        "Deployed to production reducing costs by 30%.",
        "Designed the architecture for a streaming system.",
        "Trained a model on 100k samples.",
    ])
    result = ats_scan(resume, [])
    assert result.action_verb_rate >= 0.75


def test_action_verb_rate_low():
    resume = _resume([
        "The model was very accurate.",
        "Responsible for AI work.",
        "Assisted with data tasks.",
    ])
    result = ats_scan(resume, [])
    assert result.action_verb_rate < 0.5


# ---------------------------------------------------------------------------
# quantified_rate
# ---------------------------------------------------------------------------

def test_quantified_rate_high():
    result = ats_scan(SHORT_RESUME, [])
    assert result.quantified_rate >= 0.75   # all 4 bullets have numbers


def test_quantified_rate_low():
    resume = _resume([
        "Built a machine learning model.",
        "Deployed to cloud infrastructure.",
        "Improved model performance.",
    ])
    result = ats_scan(resume, [])
    assert result.quantified_rate == 0.0


# ---------------------------------------------------------------------------
# style issues
# ---------------------------------------------------------------------------

def test_style_no_em_dashes():
    result = ats_scan(SHORT_RESUME, [])
    emdash_issues = [i for i in result.style_issues if "em dash" in i.lower() or "double-dash" in i.lower()]
    assert emdash_issues == []


def test_style_flags_em_dash():
    resume = _resume(["Built a model — reducing latency by 40%."])
    result = ats_scan(resume, [])
    assert any("em dash" in i.lower() or "double-dash" in i.lower() for i in result.style_issues)


def test_style_flags_ai_language():
    resume = _resume(["Leveraged cutting-edge ML to streamline data pipelines."])
    result = ats_scan(resume, [])
    assert any(word in " ".join(result.style_issues).lower()
               for word in ("leverage", "leveraged", "streamline", "cutting-edge"))


# ---------------------------------------------------------------------------
# ats_score + passed
# ---------------------------------------------------------------------------

def test_ats_score_range():
    result = ats_scan(SHORT_RESUME, ["PyTorch", "Python"])
    assert 0 <= result.ats_score <= 100


def test_ats_passed_good_resume():
    result = ats_scan(SHORT_RESUME, ["PyTorch", "Python", "BERT"])
    # Short resume, good keywords, action verbs, numbers — should pass
    assert result.passed is True


def test_ats_fails_long_resume():
    result = ats_scan(LONG_RESUME, [])
    assert result.passed is False


# ---------------------------------------------------------------------------
# ats_report_text
# ---------------------------------------------------------------------------

def test_ats_report_text_contains_score():
    result = ats_scan(SHORT_RESUME, ["PyTorch"])
    report = ats_report_text(result, job_title="ML Engineer")
    assert "ATS Score" in report
    assert "ML Engineer" in report
    assert str(result.ats_score) in report


def test_ats_report_text_lists_gaps():
    result = ats_scan(SHORT_RESUME, ["PyTorch", "TensorFlow"])
    report = ats_report_text(result)
    assert "TensorFlow" in report
