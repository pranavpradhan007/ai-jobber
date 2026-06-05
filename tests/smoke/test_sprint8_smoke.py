"""
Sprint 8 smoke — Final V1 smoke:
  Run application end-to-end → learning note written → discovered new personal
  fact lands in pending (not verified) → repeated benign question is proposed
  as a default, not auto-applied.
"""
import json
import os
import tempfile
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, get_application, update_application
from src.db.state_machine import transition
from src.storage.bank_loader import seed_from_yaml
from src.learning.notes import write_learning_note
from src.learning.pending import record_pending_fact
from src.learning.defaults import record_question, REPEAT_THRESHOLD

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")
VERIFIED_FACTS = os.path.join(REPO_ROOT, "knowledge_base", "profile", "verified_facts.yaml")


def _setup(tmp_path):
    db_path = str(tmp_path / "sprint8.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    seed_from_yaml(conn, os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))
    return conn


@pytest.mark.smoke
def test_learning_note_produced_per_application(tmp_path):
    """Every completed application produces a learning note."""
    conn = _setup(tmp_path)
    job = create_job(conn, source="test",
                     url="https://s8.test/job1",
                     company="FinalCo", title="Engineer",
                     platform="greenhouse")
    app = create_application(conn, job_id=job.id)
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING",
               "READY_TO_SUBMIT", "SUBMITTING", "SUBMITTED"]:
        if to == "SUBMITTING":
            update_application(conn, app.id, verifier_passed=1, auto_safe=1)
        transition(conn, app.id, to)

    learn_dir = str(tmp_path / "learning")
    master = write_learning_note(
        conn, app.id,
        portal_quirks=["Extra phone verify step"],
        question_patterns=["Expected salary?"],
        learning_dir=learn_dir,
    )
    assert os.path.exists(master)
    with open(master, encoding="utf-8") as fh:
        txt = fh.read()
    assert "FinalCo" in txt
    assert f"App #{app.id}" in txt
    conn.close()


@pytest.mark.smoke
def test_new_personal_fact_lands_in_pending_not_verified(tmp_path):
    """A discovered personal fact must go to pending/, never verified_facts.yaml."""
    pfile = str(tmp_path / "pending.md")

    if os.path.exists(VERIFIED_FACTS):
        with open(VERIFIED_FACTS, encoding="utf-8") as fh:
            before = fh.read()

    record_pending_fact(
        "Open to relocation to Seattle",
        "Discovered from screener: Q5 'Are you willing to relocate?'",
        application_id=101,
        pending_file=pfile,
    )

    assert os.path.exists(pfile)
    with open(pfile, encoding="utf-8") as fh:
        pending_content = fh.read()
    assert "relocation to Seattle" in pending_content
    assert "PENDING" in pending_content

    # verified_facts.yaml must be unchanged
    if os.path.exists(VERIFIED_FACTS):
        with open(VERIFIED_FACTS, encoding="utf-8") as fh:
            after = fh.read()
        assert before == after, "verified_facts.yaml was modified — CRITICAL VIOLATION"


@pytest.mark.smoke
def test_repeated_question_proposed_not_auto_applied(tmp_path):
    """
    A question seen REPEAT_THRESHOLD times gets a proposal entry in
    candidate_defaults.md but is NOT auto-applied to source_bank or answers.
    """
    log = str(tmp_path / "qlog.json")
    dfile = str(tmp_path / "defaults.md")
    conn = _setup(tmp_path)

    question = "How many years of Python experience do you have?"
    for i in range(REPEAT_THRESHOLD):
        record_question(
            question, "5+ years", application_id=i + 1,
            log_file=log, defaults_file=dfile,
        )

    # Proposal file exists
    assert os.path.exists(dfile)
    with open(dfile, encoding="utf-8") as fh:
        proposal = fh.read()
    assert question in proposal
    assert "NOT auto-applied" in proposal

    # source_bank is unchanged
    cur = conn.execute(
        "SELECT COUNT(*) FROM source_bank WHERE LOWER(content) LIKE '%python experience%'"
    )
    assert cur.fetchone()[0] == 0, "Candidate default leaked into source_bank"
    conn.close()
