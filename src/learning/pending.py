"""
Pending-facts flow.

When the agent discovers a potential new personal fact (e.g. a screener
question reveals a preference the candidate hasn't documented), it writes
it to pending/pending_user_verification.md — NEVER to verified_facts.yaml
or source_bank.

The protected_files_guard hook also enforces this at the file-write level.
"""
from __future__ import annotations
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PENDING_FILE = os.path.join(_REPO_ROOT, "pending", "pending_user_verification.md")


def record_pending_fact(
    fact_text: str,
    context: str,
    *,
    application_id: Optional[int] = None,
    pending_file: Optional[str] = None,
) -> str:
    """
    Append one new candidate fact to the pending verification file.
    Returns the path written to.

    This function MUST NEVER write to verified_facts.yaml or source_bank.
    Only the human may promote a pending fact.
    """
    path = pending_file or PENDING_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    app_tag = f"(App #{application_id})" if application_id else ""
    entry = (
        f"\n## [{now}] {app_tag}\n"
        f"**Fact:** {fact_text}\n"
        f"**Context:** {context}\n"
        f"**Status:** PENDING — awaiting your approval\n"
        f"> To promote: add to knowledge_base/profile/verified_facts.yaml "
        f"then re-run `seed_from_yaml`.\n"
    )

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry)

    logger.info(
        "recorded pending fact app_id=%s: %r", application_id, fact_text[:60]
    )
    return path


def get_pending_facts(pending_file: Optional[str] = None) -> list[str]:
    """Return raw lines from the pending file (for display / audit)."""
    path = pending_file or PENDING_FILE
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return fh.readlines()
