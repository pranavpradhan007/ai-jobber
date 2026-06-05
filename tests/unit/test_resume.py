"""
Unit tests for Sprint 4: bullet retrieval, bounded rephrase, renderer, diff, pipeline.
Forced-hallucination test proves the verifier gate blocks fake claims.
"""
import os
import pytest

from src.db.jobs import create_job
from src.db.applications import create_application, get_application
from src.db.state_machine import transition
from src.resume.retrieval import retrieve_bullets
from src.resume.rephrase import (
    rephrase_bullets,
    mock_rephraser_clean,
    mock_rephraser_with_hallucination,
)
from src.resume.renderer import render_docx, render_pdf
from src.resume.diff import write_resume_diff
from src.resume.pipeline import run_tailoring
from src.verifier.diff_verifier import VerifierGateError, verify_text

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _make_app(conn, url_suffix=""):
    job = create_job(conn, source="test",
                     url=f"https://resume.test/{url_suffix or id(conn)}")
    return create_application(conn, job_id=job.id)


def _walk_to_tailoring(conn, app_id):
    transition(conn, app_id, "SCORED")
    transition(conn, app_id, "TAILORING")


# ── 1. Bullet retrieval ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_retrieve_bullets_returns_relevant_items(seeded_db):
    bullets = retrieve_bullets(seeded_db, ["Python", "PostgreSQL"], surface="resume")
    all_items = [b for blist in bullets.values() for b in blist]
    contents = [b.content for b in all_items]
    assert "Python" in contents
    assert "PostgreSQL" in contents


@pytest.mark.unit
def test_retrieve_bullets_hot_keyword_prioritised(seeded_db):
    bullets = retrieve_bullets(seeded_db, ["Python"], surface="resume")
    tools = bullets.get("tool", [])
    assert tools, "Expected tool bullets"
    assert tools[0].content == "Python"


@pytest.mark.unit
def test_retrieve_bullets_excludes_private(seeded_db):
    bullets = retrieve_bullets(seeded_db, ["Staff Engineer"], surface="resume")
    all_items = [b for blist in bullets.values() for b in blist]
    contents = [b.content for b in all_items]
    assert "Staff Engineer" not in contents


# ── 2. Bounded rephrase (mock) ────────────────────────────────────────────────

@pytest.mark.unit
def test_clean_rephraser_returns_one_per_bullet(seeded_db):
    bullets = retrieve_bullets(seeded_db, ["Python"], surface="resume")
    all_bullets = [b for blist in bullets.values() for b in blist]
    rephrased = rephrase_bullets(all_bullets, ["Python"], rephraser=mock_rephraser_clean)
    assert len(rephrased) == len(all_bullets)
    for r in rephrased:
        assert isinstance(r, str) and len(r) > 0


@pytest.mark.unit
def test_hallucination_rephraser_injects_extra(seeded_db):
    bullets = retrieve_bullets(seeded_db, ["Python"], surface="resume")
    all_bullets = [b for blist in bullets.values() for b in blist]
    rephrased = rephrase_bullets(
        all_bullets, ["Python"], rephraser=mock_rephraser_with_hallucination
    )
    # Hallucination rephraser adds one extra bullet
    assert len(rephrased) > len(all_bullets)
    combined = " ".join(rephrased).lower()
    assert "500%" in combined or "increased revenue" in combined


# ── 3. DOCX rendering ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_render_docx_creates_file(tmp_path):
    out = str(tmp_path / "test_resume.docx")
    bullets = {"metric": ["Reduced latency by 40%."], "tool": ["Used Python."]}
    render_docx(bullets, candidate_name="Alice", job_title="Engineer", out_path=out)
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0


@pytest.mark.unit
def test_render_pdf_creates_file(tmp_path):
    docx = str(tmp_path / "test_resume.docx")
    bullets = {"tool": ["Python"], "metric": ["Improved speed."]}
    render_docx(bullets, out_path=docx)
    pdf = render_pdf(docx, str(tmp_path / "test_resume.pdf"))
    assert os.path.exists(pdf)
    assert os.path.getsize(pdf) > 0


# ── 4. resume_diff.md ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_write_resume_diff_creates_file(seeded_db, tmp_path):
    from src.verifier.diff_verifier import VerifierReport
    job = create_job(seeded_db, source="test", url="https://diff.test/1")
    app = create_application(seeded_db, job_id=job.id)

    bullets = retrieve_bullets(seeded_db, ["Python"], surface="resume")
    all_bullets = [b for blist in bullets.values() for b in blist]

    report = VerifierReport(
        application_id=app.id, surface="resume", total_sentences=1, passed=True
    )
    diff_path = write_resume_diff(
        str(tmp_path), all_bullets, ["Utilised Python."], report, job_title="Engineer"
    )
    assert os.path.exists(diff_path)
    with open(diff_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "Python" in content
    assert "Verifier passed:** YES" in content


# ── 5. Full pipeline — clean path ────────────────────────────────────────────

@pytest.mark.unit
def test_pipeline_clean_advances_to_resume_verified(seeded_db):
    app = _make_app(seeded_db, "pipe1")
    _walk_to_tailoring(seeded_db, app.id)

    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = str(
        __import__("tempfile").mkdtemp()
    )
    try:
        result = run_tailoring(
            seeded_db, app.id,
            hot_keywords=["Python", "PostgreSQL"],
            job_title="Senior Engineer",
            rephraser=mock_rephraser_clean,
            extractor=lambda s: [{"text": "Python", "type": "tool"}],
        )
    finally:
        fm.APPLICATIONS_BASE = orig

    assert result.verifier_passed is True
    assert os.path.exists(result.resume_path)
    assert os.path.exists(result.resume_pdf_path)
    assert os.path.exists(result.resume_diff_path)

    saved = get_application(seeded_db, app.id)
    assert saved.state == "RESUME_VERIFIED"
    assert saved.verifier_passed == 1
    assert saved.resume_path is not None


# ── 6. Pipeline — forced hallucination is blocked ────────────────────────────

@pytest.mark.unit
def test_pipeline_hallucination_blocked(seeded_db):
    """
    When the rephraser injects a fake metric, the verifier must block it
    and the pipeline must raise VerifierGateError (not advance to RESUME_VERIFIED).
    """
    app = _make_app(seeded_db, "pipe2")
    _walk_to_tailoring(seeded_db, app.id)

    import src.storage.folders as fm, tempfile
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = tempfile.mkdtemp()

    def hallucinating_extractor(sentence):
        if "500%" in sentence or "revenue" in sentence.lower():
            return [{"text": "Increased revenue by 500%", "type": "metric"}]
        return [{"text": "Python", "type": "tool"}]

    try:
        with pytest.raises(VerifierGateError):
            run_tailoring(
                seeded_db, app.id,
                hot_keywords=["Python"],
                rephraser=mock_rephraser_with_hallucination,
                extractor=hallucinating_extractor,
            )
    finally:
        fm.APPLICATIONS_BASE = orig

    # State must NOT be RESUME_VERIFIED
    saved = get_application(seeded_db, app.id)
    assert saved.state != "RESUME_VERIFIED"
    assert saved.verifier_passed == 0


# ── 7. Paths recorded on application row ─────────────────────────────────────

@pytest.mark.unit
def test_pipeline_records_paths(seeded_db):
    app = _make_app(seeded_db, "pipe3")
    _walk_to_tailoring(seeded_db, app.id)

    import src.storage.folders as fm, tempfile
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = tempfile.mkdtemp()
    try:
        run_tailoring(
            seeded_db, app.id,
            hot_keywords=["Python"],
            rephraser=mock_rephraser_clean,
            extractor=lambda s: [],
        )
    finally:
        fm.APPLICATIONS_BASE = orig

    saved = get_application(seeded_db, app.id)
    assert saved.resume_path and "resume.docx" in saved.resume_path
    assert saved.resume_pdf_path and "resume.pdf" in saved.resume_pdf_path
    assert saved.resume_diff_path and "resume_diff.md" in saved.resume_diff_path
