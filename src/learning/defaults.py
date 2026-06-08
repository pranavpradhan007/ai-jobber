"""
Candidate default proposals.

When the same screener question appears >= REPEAT_THRESHOLD times,
the learning module proposes it as a candidate default answer.

Proposals are written to learning/candidate_defaults.md — they are
NEVER auto-applied. The human reviews and decides whether to add
the answer to knowledge_base/profile/ files.
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REPEAT_THRESHOLD = 3  # propose after this many occurrences

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULTS_FILE = os.path.join(_REPO_ROOT, "learning", "candidate_defaults.md")
QUESTION_LOG_FILE = os.path.join(_REPO_ROOT, "learning", "seen_questions.json")


def record_question(
    question_text: str,
    answer_given: str,
    *,
    application_id: Optional[int] = None,
    log_file: Optional[str] = None,
    defaults_file: Optional[str] = None,
) -> Optional[str]:
    """
    Record a screener question + answer. If this question has appeared
    >= REPEAT_THRESHOLD times, write a candidate default proposal.

    Returns the defaults file path if a proposal was written, else None.
    Never auto-applies any answer.
    """
    log_file = log_file or QUESTION_LOG_FILE
    defaults_file = defaults_file or DEFAULTS_FILE

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    os.makedirs(os.path.dirname(defaults_file), exist_ok=True)

    # Load existing log
    seen: dict = {}
    if os.path.exists(log_file):
        with open(log_file, encoding="utf-8") as fh:
            try:
                seen = json.load(fh)
            except json.JSONDecodeError:
                seen = {}

    key = question_text.strip().lower()
    entry = seen.get(key, {"count": 0, "answers": [], "question": question_text})
    entry["count"] += 1
    if answer_given and answer_given not in entry["answers"]:
        entry["answers"].append(answer_given)
    seen[key] = entry

    with open(log_file, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, indent=2)

    # Propose default if threshold reached (exactly, to avoid repeated proposals)
    if entry["count"] == REPEAT_THRESHOLD:
        return _write_proposal(question_text, entry["answers"], defaults_file,
                               application_id)

    return None


def load_seen_questions(log_file: Optional[str] = None) -> dict:
    """
    Load the seen-questions log. Returns {} if file absent or corrupt.
    Keys are lowercase question texts; values are {count, answers, question} dicts.
    """
    log_file = log_file or QUESTION_LOG_FILE
    if not os.path.exists(log_file):
        return {}
    with open(log_file, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return {}


def _write_proposal(
    question: str,
    answers: list[str],
    defaults_file: str,
    application_id: Optional[int],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    app_tag = f"(last seen: App #{application_id})" if application_id else ""
    most_common = answers[-1] if answers else "(unknown)"

    proposal = (
        f"\n## [{now}] Candidate Default Proposal {app_tag}\n"
        f"**Question:** {question}\n"
        f"**Most recent answer:** {most_common}\n"
        f"**All answers given:** {', '.join(answers)}\n"
        f"**Action required:** Review and add to application_answers.json if correct.\n"
        f"> This was NOT auto-applied. Human review required.\n"
    )

    with open(defaults_file, "a", encoding="utf-8") as fh:
        fh.write(proposal)

    logger.info(
        "candidate default proposed for question: %r", question[:60]
    )
    return defaults_file
