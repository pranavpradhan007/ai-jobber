"""
Unit tests for src/db/state_machine.py
Covers: all legal transitions, illegal transition rejection,
        SUBMITTING invariant, audit trail completeness.
"""
import pytest

from src.db.state_machine import (
    LEGAL_TRANSITIONS,
    ALL_STATES,
    IllegalTransitionError,
    SubmittingInvariantError,
    transition,
    get_transitions,
)
from src.db.jobs import create_job
from src.db.applications import create_application, update_application


# ── Helpers ─────────────────────────────────────────────────────────────────

def _seed_app(conn, *, state="DISCOVERED", verifier_passed=0,
              auto_safe=0, approved_by_user=0):
    """Create a job + application wired to the given state."""
    job = create_job(conn, source="test",
                     url=f"https://example.com/job/{id(state)}")
    app = create_application(conn, job_id=job.id, state=state)
    if verifier_passed or auto_safe or approved_by_user:
        update_application(
            conn, app.id,
            verifier_passed=verifier_passed,
            auto_safe=auto_safe,
            approved_by_user=approved_by_user,
        )
    return app


# ── 1. Smoke: happy-path legal transitions ───────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("from_st,to_st", [
    ("DISCOVERED",               "SCORED"),
    ("DISCOVERED",               "SKIPPED"),
    ("SCORED",                   "TAILORING"),
    ("SCORED",                   "SKIPPED"),
    ("TAILORING",                "RESUME_VERIFIED"),
    ("TAILORING",                "FAILED"),
    ("RESUME_VERIFIED",          "PACKAGING"),
    ("PACKAGING",                "READY_TO_SUBMIT"),
    ("READY_TO_SUBMIT",          "WAITING_FOR_USER_APPROVAL"),
    ("WAITING_FOR_USER_APPROVAL","SKIPPED"),
    ("WAITING_FOR_USER_APPROVAL","TAILORING"),
    ("WAITING_FOR_USER_APPROVAL","SNOOZED"),
    ("SUBMITTED",                "MONITORING"),
    ("MONITORING",               "INTERVIEW_SCHEDULED"),
    ("MONITORING",               "OA_RECEIVED"),
    ("MONITORING",               "REJECTED"),
    ("MONITORING",               "MONITORING"),
    ("WAITING_FOR_CAPTCHA",      "SUBMITTING"),
    ("WAITING_FOR_CAPTCHA",      "FAILED"),
    ("WAITING_FOR_MFA",          "SUBMITTING"),
    ("SNOOZED",                  "WAITING_FOR_USER_APPROVAL"),
    ("FAILED",                   "DEAD"),
])
def test_legal_transition(tmp_db, from_st, to_st):
    """All entries in LEGAL_TRANSITIONS must succeed without error."""
    # Some to_states require the SUBMITTING invariant — set flags if needed
    needs_submitting = (to_st == "SUBMITTING")
    app = _seed_app(
        tmp_db,
        state=from_st,
        verifier_passed=1 if needs_submitting else 0,
        auto_safe=1 if needs_submitting else 0,
    )
    new_state = transition(tmp_db, app.id, to_st, reason="test", actor="pytest")
    assert new_state == to_st


@pytest.mark.unit
def test_ready_to_submit_to_submitting_auto_safe(tmp_db):
    """READY_TO_SUBMIT → SUBMITTING succeeds when auto_safe=1 and verifier_passed=1."""
    app = _seed_app(tmp_db, state="READY_TO_SUBMIT",
                    verifier_passed=1, auto_safe=1)
    state = transition(tmp_db, app.id, "SUBMITTING", reason="auto_safe path")
    assert state == "SUBMITTING"


@pytest.mark.unit
def test_waiting_approval_to_submitting_approved(tmp_db):
    """WAITING_FOR_USER_APPROVAL → SUBMITTING succeeds when approved_by_user=1."""
    app = _seed_app(tmp_db, state="WAITING_FOR_USER_APPROVAL",
                    verifier_passed=1, approved_by_user=1)
    state = transition(tmp_db, app.id, "SUBMITTING", reason="user approved")
    assert state == "SUBMITTING"


# ── 2. Illegal transitions must be rejected ──────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("from_st,bad_to_st", [
    ("DISCOVERED",     "SUBMITTING"),
    ("DISCOVERED",     "MONITORING"),
    ("SCORED",         "SUBMITTED"),
    ("TAILORING",      "SCORED"),
    ("SKIPPED",        "DISCOVERED"),       # terminal
    ("REJECTED",       "TAILORING"),        # terminal
    ("DEAD",           "PENDING"),          # unknown state + terminal
    ("SUBMITTED",      "DISCOVERED"),
    ("MONITORING",     "SUBMITTING"),
])
def test_illegal_transition_rejected(tmp_db, from_st, bad_to_st):
    """Illegal transitions must raise IllegalTransitionError."""
    app = _seed_app(tmp_db, state=from_st)
    with pytest.raises(IllegalTransitionError):
        transition(tmp_db, app.id, bad_to_st)


@pytest.mark.unit
def test_unknown_to_state_rejected(tmp_db):
    """An entirely unknown to_state must raise IllegalTransitionError."""
    app = _seed_app(tmp_db, state="DISCOVERED")
    with pytest.raises(IllegalTransitionError):
        transition(tmp_db, app.id, "DOES_NOT_EXIST")


# ── 3. SUBMITTING invariant ───────────────────────────────────────────────────

@pytest.mark.unit
def test_submitting_blocked_when_verifier_not_passed(tmp_db):
    """verifier_passed=0 must block SUBMITTING even if approved."""
    app = _seed_app(tmp_db, state="READY_TO_SUBMIT",
                    verifier_passed=0, auto_safe=1)
    with pytest.raises(SubmittingInvariantError):
        transition(tmp_db, app.id, "SUBMITTING")


@pytest.mark.unit
def test_submitting_blocked_when_no_approval_and_not_auto_safe(tmp_db):
    """verifier_passed=1 but neither auto_safe nor approved_by_user must block."""
    app = _seed_app(tmp_db, state="READY_TO_SUBMIT",
                    verifier_passed=1, auto_safe=0, approved_by_user=0)
    with pytest.raises(SubmittingInvariantError):
        transition(tmp_db, app.id, "SUBMITTING")


@pytest.mark.unit
def test_submitting_blocked_gated_without_approval(tmp_db):
    """A gated job with verifier_passed=1 but no human approval is blocked."""
    app = _seed_app(tmp_db, state="WAITING_FOR_USER_APPROVAL",
                    verifier_passed=1, auto_safe=0, approved_by_user=0)
    with pytest.raises(SubmittingInvariantError):
        transition(tmp_db, app.id, "SUBMITTING")


# ── 4. Audit trail: state_transitions populated ──────────────────────────────

@pytest.mark.unit
def test_state_transitions_logged(tmp_db):
    """Every transition must create a row in state_transitions."""
    app = _seed_app(tmp_db, state="DISCOVERED")
    transition(tmp_db, app.id, "SCORED", reason="fit=75", actor="scorer")
    transition(tmp_db, app.id, "TAILORING", reason="above threshold", actor="system")

    rows = get_transitions(tmp_db, app.id)
    assert len(rows) == 2
    assert rows[0]["from_state"] == "DISCOVERED"
    assert rows[0]["to_state"] == "SCORED"
    assert rows[0]["reason"] == "fit=75"
    assert rows[0]["actor"] == "scorer"
    assert rows[1]["from_state"] == "SCORED"
    assert rows[1]["to_state"] == "TAILORING"


@pytest.mark.unit
def test_all_legal_transitions_covered_by_graph():
    """Verify the graph has no orphan states (every target is also a source)."""
    all_targets = {t for targets in LEGAL_TRANSITIONS.values() for t in targets}
    unknown_targets = all_targets - ALL_STATES
    assert not unknown_targets, f"Target states not in ALL_STATES: {unknown_targets}"
