"""
Sprint 2 smoke test:
  - Clean rephrase (passes verifier)
  - Rephrase injecting a fake metric + fake tool (both blocked, needs_approval)
  - private_never_submit item is refused for a resume surface
"""
import os
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application
from src.storage.bank_loader import seed_from_yaml
from src.verifier.diff_verifier import verify_text, VerifierGateError, verifier_hard_gate

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _make_db(tmp_path):
    db_path = str(tmp_path / "sprint2_smoke.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    seed_from_yaml(conn, os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))
    return conn


@pytest.mark.smoke
def test_clean_rephrase_passes(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test", url="https://s2.test/1")
    app = create_application(conn, job_id=job.id)

    report = verify_text(
        conn, app.id,
        "Engineered Python microservices with PostgreSQL, achieving Reduced API latency by 40%.",
        surface="resume",
        extractor=lambda s: [
            {"text": "Python", "type": "tool"},
            {"text": "PostgreSQL", "type": "tool"},
            {"text": "Reduced API latency by 40%", "type": "metric"},
        ],
    )
    assert report.passed is True, f"Expected pass, blocked: {report.blocked_claims}"
    conn.close()


@pytest.mark.smoke
def test_injected_fake_metric_and_tool_blocked(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test", url="https://s2.test/2")
    app = create_application(conn, job_id=job.id)

    def extractor_with_injection(sentence):
        return [
            {"text": "Python", "type": "tool"},               # valid
            {"text": "Increased profit by 900%", "type": "metric"},  # FAKE metric
            {"text": "SuperProprietary", "type": "tool"},     # FAKE tool
        ]

    report = verify_text(
        conn, app.id,
        "Used Python and SuperProprietary to increase profit by 900%.",
        surface="resume",
        extractor=extractor_with_injection,
    )
    assert report.passed is False
    blocked_texts = {c.claim_text for c in report.blocked_claims}
    assert "Increased profit by 900%" in blocked_texts
    assert "SuperProprietary" in blocked_texts
    # Both must be marked needs_approval
    assert all(c.needs_approval for c in report.blocked_claims)
    conn.close()


@pytest.mark.smoke
def test_private_never_submit_refused_for_resume(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test", url="https://s2.test/3")
    app = create_application(conn, job_id=job.id)

    report = verify_text(
        conn, app.id,
        "Served as Staff Engineer at a top-tier company.",
        surface="resume",
        extractor=lambda s: [{"text": "Staff Engineer", "type": "title"}],
    )
    assert report.passed is False
    blocked = {c.claim_text for c in report.blocked_claims}
    assert "Staff Engineer" in blocked
    conn.close()


@pytest.mark.smoke
def test_hard_gate_blocks_submitting_when_claims_unsupported(tmp_path):
    conn = _make_db(tmp_path)
    job = create_job(conn, source="test", url="https://s2.test/4")
    app = create_application(conn, job_id=job.id)

    verify_text(
        conn, app.id,
        "Expert in FakeLibrary.",
        surface="resume",
        extractor=lambda s: [{"text": "FakeLibrary", "type": "tool"}],
    )
    with pytest.raises(VerifierGateError):
        verifier_hard_gate(conn, app.id)
    conn.close()
