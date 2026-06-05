"""
Score gate.

Assigns should_apply and submit_tier to an application based on:
  - scorecard (threshold check)
  - job platform + has_screener + login_required flags

submit_tier assignment rules (from PHASE_0_REFINED_PLAN.md §7):
  platform='email'  → auto_safe  (overrides screener flag)
  platform='api'    → auto_safe
  has_screener=1    → gated
  login_required=1  → gated
  platform in workday/greenhouse/lever/ashby/custom → gated
  else              → auto_safe
"""
from __future__ import annotations
import logging
import sqlite3

from src.db.applications import update_application
from src.scoring.scorer import Scorecard

logger = logging.getLogger(__name__)

_GATED_PLATFORMS = {"workday", "greenhouse", "lever", "ashby", "custom"}
_AUTO_SAFE_PLATFORMS = {"email", "api"}


def apply_score_gate(
    conn: sqlite3.Connection,
    app_id: int,
    scorecard: Scorecard,
    *,
    platform: str = "custom",
    has_screener: int = 0,
    login_required: int = 0,
) -> dict:
    """
    Apply the score gate and tier assignment.
    Updates the application row and enqueues tailoring work if should_apply=1.
    Returns a dict with should_apply, submit_tier, auto_safe.
    """
    should_apply = 1 if scorecard.passed_threshold else 0
    submit_tier, auto_safe = _assign_tier(platform, has_screener, login_required)

    update_application(
        conn, app_id,
        should_apply=should_apply,
        submit_tier=submit_tier,
        auto_safe=auto_safe,
    )

    if should_apply:
        logger.info(
            "score gate PASS app_id=%d score=%.1f tier=%s",
            app_id, scorecard.total_score, submit_tier,
        )
    else:
        logger.info(
            "score gate FAIL app_id=%d score=%.1f — skip downstream work",
            app_id, scorecard.total_score,
        )

    return {
        "should_apply": should_apply,
        "submit_tier": submit_tier,
        "auto_safe": auto_safe,
    }


def _assign_tier(
    platform: str,
    has_screener: int,
    login_required: int,
) -> tuple[str, int]:
    """Return (submit_tier, auto_safe_flag)."""
    plat = (platform or "custom").lower()
    if plat in _AUTO_SAFE_PLATFORMS:
        return "auto_safe", 1
    if has_screener or login_required or plat in _GATED_PLATFORMS:
        return "gated", 0
    # Default: treat unknown platforms as gated to be safe
    return "gated", 0
