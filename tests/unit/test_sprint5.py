"""
Unit tests for Sprint 5: submit, digest, approval parser, overnight runner.
All Gmail mocked. SUBMITTING invariant verified on every submit path.
"""
import os
import tempfile
import pytest

from src.db.jobs import create_job
from src.db.applications import create_application, get_application, update_application
from src.db.state_machine import transition, SubmittingInvariantError
from src.gmail.client import MockGmailClient, GmailMessage
from src.gmail.digest import build_digest
from src.approvals.parser import parse_reply, apply_commands
from src.browser.submit import submit_auto_safe, AutoSafeSubmitError
from src.runners.overnight import run_overnight

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _high_scorer(j, p):
    return {d: {"score": 80, "reason": "ok"} for d in
            ["title_match","must_have_skills","nice_to_have_skills",
             "industry_fit","location_match","salary_fit"]}

def _low_scorer(j, p):
    return {d: {"score": 10, "reason": "poor"} for d in
            ["title_match","must_have_skills","nice_to_have_skills",
             "industry_fit","location_match","salary_fit"]}

def _mock_rephraser(bullets, kws, title):
    return [f"Used {b.content}." for b in bullets]

def _no_claims_extractor(sentence):
    return []


def _make_auto_safe_app(conn, url_suffix=""):
    """Create a job + application ready for auto_safe submit."""
    job = create_job(conn, source="test",
                     url=f"https://as.test/{url_suffix or id(conn)}",
                     platform="email",
                     email_apply_addr="recruiter@company.com")
    app = create_application(conn, job_id=job.id)
    # Walk to READY_TO_SUBMIT with flags set
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING", "READY_TO_SUBMIT"]:
        transition(conn, app.id, to)
    update_application(conn, app.id, verifier_passed=1, auto_safe=1,
                       submit_tier="auto_safe")
    return get_application(conn, app.id)


def _make_gated_app(conn, url_suffix=""):
    job = create_job(conn, source="test",
                     url=f"https://gt.test/{url_suffix or id(conn)}",
                     platform="workday", has_screener=1)
    app = create_application(conn, job_id=job.id)
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING",
               "READY_TO_SUBMIT", "WAITING_FOR_USER_APPROVAL"]:
        transition(conn, app.id, to)
    update_application(conn, app.id, verifier_passed=1, submit_tier="gated")
    return get_application(conn, app.id)


# ── 1. auto_safe submit ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_auto_safe_submit_succeeds(seeded_db):
    app = _make_auto_safe_app(seeded_db, "sub1")
    gmail = MockGmailClient()
    result = submit_auto_safe(seeded_db, app.id, gmail_client=gmail)
    assert result.success is True
    assert result.receipt and result.receipt.startswith("GMAIL:")
    assert len(gmail.sent) == 1
    saved = get_application(seeded_db, app.id)
    assert saved.state == "SUBMITTED"
    assert saved.receipt is not None


@pytest.mark.unit
def test_auto_safe_submit_blocked_without_verifier(seeded_db):
    """verifier_passed=0 must block auto_safe submit."""
    app = _make_auto_safe_app(seeded_db, "sub2")
    update_application(seeded_db, app.id, verifier_passed=0)
    gmail = MockGmailClient()
    with pytest.raises(AutoSafeSubmitError, match="verifier_passed=0"):
        submit_auto_safe(seeded_db, app.id, gmail_client=gmail)


@pytest.mark.unit
def test_auto_safe_rejected_for_gated_job(seeded_db):
    """auto_safe=0 job must not use the auto_safe submit path."""
    job = create_job(seeded_db, source="test", url="https://as.test/gate1",
                     platform="workday")
    app = create_application(seeded_db, job_id=job.id)
    for to in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING", "READY_TO_SUBMIT"]:
        transition(seeded_db, app.id, to)
    update_application(seeded_db, app.id, verifier_passed=1, auto_safe=0)
    gmail = MockGmailClient()
    with pytest.raises(AutoSafeSubmitError, match="auto_safe=0"):
        submit_auto_safe(seeded_db, app.id, gmail_client=gmail)


# ── 2. Digest builder ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_digest_sent_for_waiting_apps(seeded_db):
    _make_gated_app(seeded_db, "dig1")
    _make_gated_app(seeded_db, "dig2")
    gmail = MockGmailClient()
    msg_id = build_digest(seeded_db, "user@example.com", gmail_client=gmail)
    assert msg_id is not None
    assert len(gmail.sent) == 1
    body = gmail.sent[0]["body"]
    assert "APP-" in body
    assert "APPROVE" in body


@pytest.mark.unit
def test_digest_not_sent_when_no_pending(seeded_db):
    gmail = MockGmailClient()
    result = build_digest(seeded_db, "user@example.com", gmail_client=gmail)
    assert result is None
    assert len(gmail.sent) == 0


# ── 3. Approval reply parser ──────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_reply_all_commands():
    text = """
APP-1 APPROVE
APP-2 REJECT
APP-3 SKIP
APP-4 EDIT
APP-5 SNOOZE
DONE
"""
    cmds = parse_reply(text)
    assert len(cmds) == 6
    by_id = {c.app_id: c.command for c in cmds if c.app_id}
    assert by_id[1] == "APPROVE"
    assert by_id[2] == "REJECT"
    assert by_id[3] == "SKIP"
    assert by_id[4] == "EDIT"
    assert by_id[5] == "SNOOZE"
    done = [c for c in cmds if c.command == "DONE"]
    assert len(done) == 1


@pytest.mark.unit
def test_parse_reply_case_insensitive():
    cmds = parse_reply("app-1 approve\nApp-2 Skip")
    assert len(cmds) == 2
    assert cmds[0].command == "APPROVE"


@pytest.mark.unit
def test_apply_approve_sets_approved_by_user(seeded_db):
    app = _make_gated_app(seeded_db, "apr1")
    cmds = parse_reply(f"APP-{app.id} APPROVE")
    results = apply_commands(seeded_db, cmds)
    assert results[0].success is True
    saved = get_application(seeded_db, app.id)
    assert saved.approved_by_user == 1


@pytest.mark.unit
def test_apply_skip_transitions_to_skipped(seeded_db):
    app = _make_gated_app(seeded_db, "sk1")
    cmds = parse_reply(f"APP-{app.id} SKIP")
    apply_commands(seeded_db, cmds)
    saved = get_application(seeded_db, app.id)
    assert saved.state == "SKIPPED"


@pytest.mark.unit
def test_apply_edit_transitions_to_tailoring(seeded_db):
    app = _make_gated_app(seeded_db, "ed1")
    cmds = parse_reply(f"APP-{app.id} EDIT")
    apply_commands(seeded_db, cmds)
    saved = get_application(seeded_db, app.id)
    assert saved.state == "TAILORING"


@pytest.mark.unit
def test_apply_snooze_transitions_to_snoozed(seeded_db):
    app = _make_gated_app(seeded_db, "sn1")
    cmds = parse_reply(f"APP-{app.id} SNOOZE")
    apply_commands(seeded_db, cmds)
    saved = get_application(seeded_db, app.id)
    assert saved.state == "SNOOZED"


# ── 4. No gated job submits without approved_by_user ─────────────────────────

@pytest.mark.unit
def test_gated_job_cannot_submit_without_approval(seeded_db):
    app = _make_gated_app(seeded_db, "gate_no_approve")
    # approved_by_user is still 0 — SUBMITTING must fail
    from src.db.state_machine import SubmittingInvariantError
    with pytest.raises(SubmittingInvariantError):
        transition(seeded_db, app.id, "SUBMITTING")


# ── 5. Overnight runner integration ──────────────────────────────────────────

@pytest.mark.unit
def test_overnight_auto_safe_submitted(seeded_db):
    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = tempfile.mkdtemp()
    try:
        job = create_job(seeded_db, source="test",
                         url="https://on.test/auto1",
                         platform="email",
                         email_apply_addr="hr@co.com",
                         clean_jd="Python and PostgreSQL required.")
        create_application(seeded_db, job_id=job.id)
        gmail = MockGmailClient()
        from src.storage.bank_loader import seed_from_yaml
        seed_from_yaml(seeded_db,
                       os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))

        stats = run_overnight(
            seeded_db,
            scorer_fn=_high_scorer,
            rephraser_fn=_mock_rephraser,
            extractor_fn=_no_claims_extractor,
            gmail_client=gmail,
        )
        assert stats.submitted >= 1
        assert stats.failed == 0
    finally:
        fm.APPLICATIONS_BASE = orig


@pytest.mark.unit
def test_overnight_gated_parks_for_approval(seeded_db):
    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = tempfile.mkdtemp()
    try:
        job = create_job(seeded_db, source="test",
                         url="https://on.test/gated1",
                         platform="workday",
                         has_screener=1,
                         clean_jd="Python and PostgreSQL required.")
        create_application(seeded_db, job_id=job.id)
        from src.storage.bank_loader import seed_from_yaml
        seed_from_yaml(seeded_db,
                       os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))

        stats = run_overnight(
            seeded_db,
            scorer_fn=_high_scorer,
            rephraser_fn=_mock_rephraser,
            extractor_fn=_no_claims_extractor,
            gmail_client=MockGmailClient(),
        )
        assert stats.gated >= 1
        assert stats.submitted == 0
    finally:
        fm.APPLICATIONS_BASE = orig


@pytest.mark.unit
def test_overnight_low_score_skipped(seeded_db):
    import src.storage.folders as fm
    orig = fm.APPLICATIONS_BASE
    fm.APPLICATIONS_BASE = tempfile.mkdtemp()
    try:
        job = create_job(seeded_db, source="test",
                         url="https://on.test/low1",
                         platform="email",
                         clean_jd="We want very specific skills not in profile.")
        create_application(seeded_db, job_id=job.id)
        stats = run_overnight(
            seeded_db,
            scorer_fn=_low_scorer,
            rephraser_fn=_mock_rephraser,
            extractor_fn=_no_claims_extractor,
            gmail_client=MockGmailClient(),
        )
        assert stats.skipped >= 1
        assert stats.submitted == 0
    finally:
        fm.APPLICATIONS_BASE = orig
