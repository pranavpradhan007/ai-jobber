"""
Unit tests for Sprint 8: learning note writer, pending-facts flow,
candidate defaults. Key invariant: new personal facts never reach
verified_facts.yaml automatically.
"""
import json
import os
import pytest

from src.db.jobs import create_job
from src.db.applications import create_application, get_application, update_application
from src.db.state_machine import transition
from src.learning.notes import write_learning_note
from src.learning.pending import record_pending_fact, get_pending_facts
from src.learning.defaults import record_question, REPEAT_THRESHOLD

VERIFIED_FACTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "knowledge_base", "profile",
    "verified_facts.yaml"
)


def _make_app(conn, url_suffix=""):
    job = create_job(conn, source="test",
                     url=f"https://learn.test/{url_suffix or id(conn)}",
                     company="LearnCo", title="Engineer", platform="workday")
    app = create_application(conn, job_id=job.id)
    transition(conn, app.id, "SCORED")
    update_application(conn, app.id, score=75.0)
    return get_application(conn, app.id)


# ── 1. Learning note writer ────────────────────────────────────────────────

@pytest.mark.unit
def test_note_written_for_application(tmp_db, tmp_path):
    app = _make_app(tmp_db, "note1")
    path = write_learning_note(
        tmp_db, app.id,
        portal_quirks=["Extra dropdown on page 2"],
        question_patterns=["Why are you leaving your current role?"],
        keyword_mappings=["Python → matched tool", "Rust → not in bank"],
        blockers=[],
        learning_dir=str(tmp_path),
    )
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    assert f"App #{app.id}" in content
    assert "LearnCo" in content
    assert "Engineer" in content


@pytest.mark.unit
def test_note_always_produced_even_empty(tmp_db, tmp_path):
    """A note with no observations still produces the master log entry."""
    app = _make_app(tmp_db, "note2")
    path = write_learning_note(tmp_db, app.id, learning_dir=str(tmp_path))
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    assert f"App #{app.id}" in content


@pytest.mark.unit
def test_portal_quirks_written_to_separate_file(tmp_db, tmp_path):
    app = _make_app(tmp_db, "note3")
    write_learning_note(
        tmp_db, app.id,
        portal_quirks=["Greenhouse form timed out after 5 min"],
        learning_dir=str(tmp_path),
    )
    quirks_file = os.path.join(str(tmp_path), "portal_quirks.md")
    assert os.path.exists(quirks_file)
    with open(quirks_file, encoding="utf-8") as fh:
        assert "timed out" in fh.read()


@pytest.mark.unit
def test_multiple_notes_append(tmp_db, tmp_path):
    """Two notes for two different apps must both appear in the log."""
    app1 = _make_app(tmp_db, "multi1")
    app2 = _make_app(tmp_db, "multi2")
    for app in (app1, app2):
        write_learning_note(tmp_db, app.id, learning_dir=str(tmp_path))
    master = os.path.join(str(tmp_path), "application_log.md")
    with open(master, encoding="utf-8") as fh:
        content = fh.read()
    assert f"App #{app1.id}" in content
    assert f"App #{app2.id}" in content


# ── 2. Pending-facts flow — never reaches verified_facts.yaml ───────────────

@pytest.mark.unit
def test_pending_fact_written_to_pending_file(tmp_path):
    pfile = str(tmp_path / "pending_user_verification.md")
    record_pending_fact(
        "Willing to relocate to NYC",
        "Mentioned in screener Q3",
        application_id=42,
        pending_file=pfile,
    )
    assert os.path.exists(pfile)
    with open(pfile, encoding="utf-8") as fh:
        content = fh.read()
    assert "Willing to relocate to NYC" in content
    assert "PENDING" in content
    assert "App #42" in content


@pytest.mark.unit
def test_pending_fact_never_written_to_verified_facts(tmp_path):
    """
    INVARIANT: record_pending_fact must NEVER touch verified_facts.yaml.
    We read the file before and after and confirm it is unchanged.
    """
    if not os.path.exists(VERIFIED_FACTS_PATH):
        pytest.skip("verified_facts.yaml not found")

    with open(VERIFIED_FACTS_PATH, encoding="utf-8") as fh:
        before = fh.read()

    pfile = str(tmp_path / "pending.md")
    record_pending_fact(
        "Knows Rust",
        "From screener",
        application_id=99,
        pending_file=pfile,
    )

    with open(VERIFIED_FACTS_PATH, encoding="utf-8") as fh:
        after = fh.read()

    assert before == after, (
        "verified_facts.yaml was modified by record_pending_fact — INVARIANT VIOLATED"
    )


@pytest.mark.unit
def test_pending_fact_not_in_source_bank(tmp_db, tmp_path):
    """A pending fact must not appear in source_bank."""
    pfile = str(tmp_path / "pending.md")
    record_pending_fact(
        "Expert in QuantumML",
        "Hypothetical future skill",
        application_id=1,
        pending_file=pfile,
    )
    cur = tmp_db.execute(
        "SELECT COUNT(*) FROM source_bank WHERE LOWER(content) LIKE '%quantuml%'"
    )
    assert cur.fetchone()[0] == 0


@pytest.mark.unit
def test_get_pending_facts_returns_lines(tmp_path):
    pfile = str(tmp_path / "pending.md")
    record_pending_fact("Fact A", "ctx A", pending_file=pfile)
    record_pending_fact("Fact B", "ctx B", pending_file=pfile)
    lines = get_pending_facts(pfile)
    combined = "".join(lines)
    assert "Fact A" in combined
    assert "Fact B" in combined


# ── 3. Candidate defaults — proposed, never auto-applied ─────────────────────

@pytest.mark.unit
def test_default_proposed_after_threshold(tmp_path):
    log = str(tmp_path / "seen.json")
    dfile = str(tmp_path / "defaults.md")
    q = "Are you willing to travel up to 25%?"
    for i in range(REPEAT_THRESHOLD):
        result = record_question(
            q, "Yes", application_id=i + 1,
            log_file=log, defaults_file=dfile,
        )
    # Only the final call (at threshold) should produce a proposal
    assert result == dfile
    with open(dfile, encoding="utf-8") as fh:
        content = fh.read()
    assert q in content
    assert "NOT auto-applied" in content


@pytest.mark.unit
def test_no_proposal_before_threshold(tmp_path):
    log = str(tmp_path / "seen2.json")
    dfile = str(tmp_path / "defaults2.md")
    q = "Preferred work style?"
    for i in range(REPEAT_THRESHOLD - 1):
        result = record_question(
            q, "Remote", application_id=i + 1,
            log_file=log, defaults_file=dfile,
        )
    assert result is None
    assert not os.path.exists(dfile)


@pytest.mark.unit
def test_proposal_not_applied_to_source_bank(tmp_db, tmp_path):
    """A default proposal must never insert anything into source_bank."""
    log = str(tmp_path / "seen3.json")
    dfile = str(tmp_path / "defaults3.md")
    q = "How many years of Rust experience?"
    before = tmp_db.execute("SELECT COUNT(*) FROM source_bank").fetchone()[0]
    for i in range(REPEAT_THRESHOLD):
        record_question(q, "2 years", application_id=i,
                        log_file=log, defaults_file=dfile)
    after = tmp_db.execute("SELECT COUNT(*) FROM source_bank").fetchone()[0]
    assert before == after, "Candidate default wrote to source_bank — INVARIANT VIOLATED"
