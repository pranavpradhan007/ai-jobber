"""
Seed source_bank from knowledge_base/profile/verified_facts.yaml.
Idempotent: uses INSERT OR IGNORE on the UNIQUE(item_type, content) constraint.
"""
from __future__ import annotations
import logging
import os
import sqlite3
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_FACTS_PATH = os.path.join(
    _REPO_ROOT, "knowledge_base", "profile", "verified_facts.yaml"
)

# Maps YAML top-level key → item_type in source_bank
_SECTION_TO_TYPE = {
    "metrics":     "metric",
    "tools":       "tool",
    "skills":      "skill",
    "credentials": "credential",
    "titles":      "title",
    "companies":   "company",
    "keywords":    "keyword",
}


def seed_from_yaml(
    conn: sqlite3.Connection,
    facts_path: Optional[str] = None,
) -> int:
    """
    Load verified_facts.yaml into source_bank.
    Returns the number of rows inserted (0 if all already present).
    """
    facts_path = facts_path or DEFAULT_FACTS_PATH
    with open(facts_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    inserted = 0
    for section, item_type in _SECTION_TO_TYPE.items():
        entries = data.get(section) or []
        for entry in entries:
            if isinstance(entry, str):
                content = entry
                source_ref = None
                usage_level = "resume"
                notes = None
            elif isinstance(entry, dict):
                content = entry.get("content", "").strip()
                source_ref = entry.get("source_ref")
                usage_level = entry.get("usage_level", "resume")
                notes = entry.get("notes")
            else:
                continue

            if not content:
                continue

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO source_bank
                    (item_type, content, source_ref, usage_level, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_type, content, source_ref, usage_level, notes),
            )
            if cur.rowcount:
                inserted += 1

    conn.commit()
    logger.info(
        "seeded source_bank from %s: %d new rows", facts_path, inserted
    )
    return inserted
