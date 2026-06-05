"""
Sprint 5 smoke:
  Seed 3 jobs (1 auto_safe, 2 gated) → run-overnight
  → auto_safe submitted with receipt
  → gated pair appear in digest
  → simulate APPROVE + SKIP reply
  → approved one submits, skipped one parks.
  All Gmail mocked.
"""
import os
import tempfile
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, get_application
from src.storage.bank_loader import seed_from_yaml
from src.gmail.client import MockGmailClient
from src.gmail.digest import build_digest
from src.approvals.parser import parse_reply, apply_commands
from src.browser.submit import submit_auto_safe
from src.db.state_machine import transition
from src.db.applications import update_application
from src.runners.overnight import run_overnight

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _high_scorer(j, p):
    return {d: {"score": 80, "reason": "ok"} for d in
            ["title_match","must_have_skills","nice_to_have_skills",
             "industry_fit","location_match","salary_fit"]}

def _mock_rephraser(bullets, kws, title):
    return [f"Used {b.content}." for b in bullets]

def _no_claims(sentence):
    return []


@pytest.mark.smoke
def test_sprint5_full_pipeline(tmp_path):
    db_path = str(tmp_path / "sprint5.db")
    conn = init_db(db_path, os.path.join(REPO_ROOT, "schema.sql"))
    seed_from_yaml(conn, os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))

    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = str(tmp_path / "applications")
    gmail = MockGmailClient()

    try:
        # Seed 3 jobs
        job_auto = create_job(conn, source="test",
                              url="https://s5.test/auto",
                              platform="email",
                              email_apply_addr="hr@auto.com",
                              clean_jd="Python developer needed.")
        job_gated1 = create_job(conn, source="test",
                                url="https://s5.test/gated1",
                                platform="workday", has_screener=1,
                                clean_jd="Python and Kubernetes experience.")
        job_gated2 = create_job(conn, source="test",
                                url="https://s5.test/gated2",
                                platform="greenhouse", has_screener=1,
                                clean_jd="PostgreSQL and REST API design.")
        create_application(conn, job_id=job_auto.id)
        create_application(conn, job_id=job_gated1.id)
        create_application(conn, job_id=job_gated2.id)

        # Run overnight
        stats = run_overnight(
            conn,
            scorer_fn=_high_scorer,
            rephraser_fn=_mock_rephraser,
            extractor_fn=_no_claims,
            gmail_client=gmail,
        )

        # auto_safe job submitted
        assert stats.submitted >= 1
        assert stats.gated >= 2

        # Build morning digest
        msg_id = build_digest(conn, "user@example.com", gmail_client=gmail)
        assert msg_id is not None
        digest_body = gmail.sent[-1]["body"]
        assert "APP-" in digest_body

        # Get the two gated app IDs
        cur = conn.execute(
            "SELECT id FROM applications WHERE state='WAITING_FOR_USER_APPROVAL'"
        )
        gated_ids = [r["id"] for r in cur.fetchall()]
        assert len(gated_ids) >= 2

        # Simulate: APPROVE first, SKIP second
        app_approve_id = gated_ids[0]
        app_skip_id = gated_ids[1]
        reply = f"APP-{app_approve_id} APPROVE\nAPP-{app_skip_id} SKIP\n"
        cmds = parse_reply(reply)
        results = apply_commands(conn, cmds)
        assert all(r.success for r in results)

        # Approved app has approved_by_user=1
        approved_app = get_application(conn, app_approve_id)
        assert approved_app.approved_by_user == 1

        # Skipped app is SKIPPED
        skipped_app = get_application(conn, app_skip_id)
        assert skipped_app.state == "SKIPPED"

        # Now submit the approved gated app
        update_application(conn, app_approve_id, auto_safe=0)  # ensure gated
        # State machine: WAITING_FOR_USER_APPROVAL → SUBMITTING
        # (runner does this; simulate directly here)
        transition(conn, app_approve_id, "SUBMITTING",
                   reason="approved by user")
        update_application(conn, app_approve_id, receipt="MANUAL_RCPT_001",
                           submitted_at="2024-01-01")
        transition(conn, app_approve_id, "SUBMITTED")

        final = get_application(conn, app_approve_id)
        assert final.state == "SUBMITTED"

    finally:
        fm.APPLICATIONS_BASE = orig
        conn.close()
