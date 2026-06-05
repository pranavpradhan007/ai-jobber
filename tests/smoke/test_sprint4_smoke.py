"""
Sprint 4 smoke:
  scored job → tailored DOCX+PDF in folder → resume_diff.md
  → verifier report passes → application reaches RESUME_VERIFIED and paths recorded.
  Also proves a forced hallucination is caught.
"""
import os
import tempfile
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, get_application
from src.db.state_machine import transition
from src.storage.bank_loader import seed_from_yaml
from src.resume.pipeline import run_tailoring
from src.resume.rephrase import mock_rephraser_clean, mock_rephraser_with_hallucination
from src.verifier.diff_verifier import VerifierGateError

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _setup(tmp_path):
    db_path = str(tmp_path / "sprint4.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    seed_from_yaml(conn, os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))
    return conn


def _make_tailoring_app(conn):
    job = create_job(conn, source="test", url=f"https://s4.test/{id(conn)}")
    app = create_application(conn, job_id=job.id)
    transition(conn, app.id, "SCORED")
    transition(conn, app.id, "TAILORING")
    return app


@pytest.mark.smoke
def test_tailoring_happy_path(tmp_path):
    conn = _setup(tmp_path)
    app = _make_tailoring_app(conn)

    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = str(tmp_path / "applications")
    try:
        result = run_tailoring(
            conn, app.id,
            hot_keywords=["Python", "PostgreSQL", "Reduced API latency by 40%"],
            job_title="Senior Software Engineer",
            candidate_name="Jane Doe",
            rephraser=mock_rephraser_clean,
            extractor=lambda s: [{"text": "Python", "type": "tool"}],
        )
    finally:
        fm.APPLICATIONS_BASE = orig

    assert result.verifier_passed is True
    assert os.path.exists(result.resume_path)
    assert os.path.exists(result.resume_pdf_path)
    assert os.path.exists(result.resume_diff_path)

    saved = get_application(conn, app.id)
    assert saved.state == "RESUME_VERIFIED"
    assert saved.verifier_passed == 1
    conn.close()


@pytest.mark.smoke
def test_tailoring_hallucination_blocked(tmp_path):
    conn = _setup(tmp_path)
    app = _make_tailoring_app(conn)

    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = str(tmp_path / "applications2")

    def extractor_catches_hallucination(sentence):
        if "500%" in sentence:
            return [{"text": "Increased revenue by 500%", "type": "metric"}]
        return []

    try:
        with pytest.raises(VerifierGateError):
            run_tailoring(
                conn, app.id,
                hot_keywords=["Python"],
                rephraser=mock_rephraser_with_hallucination,
                extractor=extractor_catches_hallucination,
            )
    finally:
        fm.APPLICATIONS_BASE = orig

    saved = get_application(conn, app.id)
    assert saved.state != "RESUME_VERIFIED"
    assert saved.verifier_passed == 0
    conn.close()
