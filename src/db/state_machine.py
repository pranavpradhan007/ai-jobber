"""
Application state machine.

One public function: transition(conn, app_id, to_state, reason, actor)
  - Validates the transition against LEGAL_TRANSITIONS.
  - Enforces the SUBMITTING invariant.
  - Updates applications.state.
  - Appends to state_transitions (immutable audit trail).
  - Never swallows errors — every failure raises with context.
"""
from __future__ import annotations
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legal transition graph
# ---------------------------------------------------------------------------
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED":                 {"SCORED", "SKIPPED", "AI_TRAP_DETECTED"},
    "SCORED":                     {"TAILORING", "SKIPPED", "AI_TRAP_DETECTED"},
    "TAILORING":                  {"RESUME_VERIFIED", "FAILED"},
    "RESUME_VERIFIED":            {"PACKAGING"},
    "PACKAGING":                  {"READY_TO_SUBMIT"},
    "READY_TO_SUBMIT":            {"SUBMITTING", "WAITING_FOR_USER_APPROVAL", "AI_TRAP_DETECTED", "SKIPPED", "FAILED", "WAITING_FOR_CAPTCHA", "WAITING_FOR_MFA"},
    "WAITING_FOR_USER_APPROVAL":  {"SUBMITTING", "SKIPPED", "TAILORING", "SNOOZED", "AI_TRAP_DETECTED", "WAITING_FOR_MFA", "WAITING_FOR_CAPTCHA"},
    "SUBMITTING":                 {
                                      "SUBMITTED",
                                      "WAITING_FOR_CAPTCHA",
                                      "WAITING_FOR_MFA",
                                      "WAITING_FOR_GMAIL_VERIFICATION",
                                      "FAILED",
                                  },
    "SUBMITTED":                  {"MONITORING"},
    "MONITORING":                 {
                                      "INTERVIEW_SCHEDULED",
                                      "OA_RECEIVED",
                                      "REJECTED",
                                      "MONITORING",   # re-entry for additional events
                                  },
    "WAITING_FOR_CAPTCHA":        {"SUBMITTING", "FAILED", "READY_TO_SUBMIT"},
    "WAITING_FOR_MFA":            {"SUBMITTING", "FAILED"},
    "WAITING_FOR_GMAIL_VERIFICATION": {"SUBMITTING", "FAILED"},
    "SNOOZED":                    {"WAITING_FOR_USER_APPROVAL"},
    "FAILED":                     {"DEAD"},
    # Terminal states — no outbound edges
    "AI_TRAP_DETECTED":           set(),
    "INTERVIEW_SCHEDULED":        set(),
    "OA_RECEIVED":                set(),
    "REJECTED":                   set(),
    "SKIPPED":                    set(),
    "DEAD":                       set(),
}

ALL_STATES: frozenset[str] = frozenset(LEGAL_TRANSITIONS.keys())


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StateMachineError(Exception):
    """Base for all state machine errors."""


class IllegalTransitionError(StateMachineError):
    """Raised when from_state → to_state is not in LEGAL_TRANSITIONS."""


class SubmittingInvariantError(StateMachineError):
    """Raised when SUBMITTING preconditions are not met."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transition(
    conn: sqlite3.Connection,
    app_id: int,
    to_state: str,
    *,
    reason: Optional[str] = None,
    actor: str = "system",
) -> str:
    """
    Atomically transition application `app_id` to `to_state`.

    Returns the new state string.
    Raises IllegalTransitionError or SubmittingInvariantError on violation.
    All errors are logged before raising.
    """
    if to_state not in ALL_STATES:
        msg = f"Unknown state '{to_state}' for application {app_id}"
        logger.error(msg)
        raise IllegalTransitionError(msg)

    # Read current state + invariant columns inside a transaction
    cur = conn.execute(
        "SELECT state, verifier_passed, auto_safe, approved_by_user "
        "FROM applications WHERE id = ?",
        (app_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"Application {app_id} not found")

    from_state = row["state"]
    verifier_passed = row["verifier_passed"]
    auto_safe = row["auto_safe"]
    approved_by_user = row["approved_by_user"]

    # ── 1. Validate transition legality ──────────────────────────────────────
    allowed = LEGAL_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        msg = (
            f"Illegal transition {from_state!r} → {to_state!r} "
            f"for application {app_id}. "
            f"Allowed from {from_state!r}: {sorted(allowed) or '(terminal)'}"
        )
        logger.error(msg)
        raise IllegalTransitionError(msg)

    # ── 2. SUBMITTING invariant ───────────────────────────────────────────────
    if to_state == "SUBMITTING":
        _check_submitting_invariant(
            app_id, verifier_passed, auto_safe, approved_by_user
        )

    # ── 3. Apply transition ───────────────────────────────────────────────────
    conn.execute(
        "UPDATE applications SET state = ? WHERE id = ?",
        (to_state, app_id),
    )
    conn.execute(
        """
        INSERT INTO state_transitions
            (application_id, from_state, to_state, reason, actor)
        VALUES (?, ?, ?, ?, ?)
        """,
        (app_id, from_state, to_state, reason, actor),
    )
    conn.commit()

    logger.info(
        "state transition app_id=%d  %s → %s  reason=%r actor=%s",
        app_id, from_state, to_state, reason, actor,
    )
    return to_state


def get_transitions(
    conn: sqlite3.Connection, app_id: int
) -> list[dict]:
    """Return all state_transitions rows for an application, oldest first."""
    cur = conn.execute(
        """
        SELECT id, application_id, from_state, to_state, reason, actor, transitioned_at
        FROM state_transitions
        WHERE application_id = ?
        ORDER BY id
        """,
        (app_id,),
    )
    return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_submitting_invariant(
    app_id: int,
    verifier_passed: int,
    auto_safe: int,
    approved_by_user: int,
) -> None:
    """
    SUBMITTING requires one of:
      approved_by_user = 1  (user approval overrides automated verifier)
      auto_safe = 1 AND verifier_passed = 1  (fully automated path)
    """
    errors = []

    # User approval is the highest authority — if approved_by_user=1, verifier_passed
    # is not required (the user has seen and accepted the application content).
    if not approved_by_user:
        if not verifier_passed:
            errors.append("verifier_passed = 0 (verifier has not cleared all claims)")
        if not auto_safe:
            errors.append(
                "neither auto_safe=1 nor approved_by_user=1 "
                "(gated jobs require explicit user approval)"
            )

    if errors:
        msg = (
            f"SUBMITTING invariant violated for application {app_id}: "
            + "; ".join(errors)
        )
        logger.error(msg)
        raise SubmittingInvariantError(msg)
