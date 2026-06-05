"""
Per-application learning note writer.

After each application, writes a structured note to learning/ covering:
  - portal_quirks: unexpected fields, broken UX, timeout issues
  - question_patterns: new screener questions encountered
  - keyword_mappings: JD keywords that matched (or didn't) bank items
  - blockers: anything that required human intervention

Notes are append-only — each application gets its own dated section.
"""
from __future__ import annotations
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LEARNING_DIR = os.path.join(_REPO_ROOT, "learning")


@dataclass
class LearningNote:
    application_id: int
    company: str
    title: str
    portal: str
    score: Optional[float]
    state: str
    portal_quirks: list[str] = field(default_factory=list)
    question_patterns: list[str] = field(default_factory=list)
    keyword_mappings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def write_learning_note(
    conn: sqlite3.Connection,
    app_id: int,
    *,
    portal_quirks: Optional[list[str]] = None,
    question_patterns: Optional[list[str]] = None,
    keyword_mappings: Optional[list[str]] = None,
    blockers: Optional[list[str]] = None,
    learning_dir: Optional[str] = None,
) -> str:
    """
    Write a structured learning note for `app_id`.
    Returns the path of the notes file written to.
    """
    learning_dir = learning_dir or LEARNING_DIR
    os.makedirs(learning_dir, exist_ok=True)

    # Load app + job context
    cur = conn.execute(
        """
        SELECT a.id, a.state, a.score, a.submit_tier,
               j.company, j.title, j.platform
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        WHERE a.id = ?
        """,
        (app_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"Application {app_id} not found")

    note = LearningNote(
        application_id=app_id,
        company=row["company"] or "Unknown",
        title=row["title"] or "Unknown",
        portal=row["platform"] or "unknown",
        score=row["score"],
        state=row["state"],
        portal_quirks=portal_quirks or [],
        question_patterns=question_patterns or [],
        keyword_mappings=keyword_mappings or [],
        blockers=blockers or [],
    )

    # Each file is per-category; we append dated sections
    _append_section(
        os.path.join(learning_dir, "portal_quirks.md"),
        "Portal Quirks",
        note,
        note.portal_quirks,
    )
    _append_section(
        os.path.join(learning_dir, "question_patterns.md"),
        "Question Patterns",
        note,
        note.question_patterns,
    )
    _append_section(
        os.path.join(learning_dir, "keyword_mappings.md"),
        "Keyword Mappings",
        note,
        note.keyword_mappings,
    )

    # Master log — always written, even if all lists are empty
    master_path = os.path.join(learning_dir, "application_log.md")
    _append_master(master_path, note)

    logger.info("wrote learning note for app_id=%d", app_id)
    return master_path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _append_section(
    path: str,
    heading: str,
    note: LearningNote,
    items: list[str],
) -> None:
    if not items:
        return
    header = (
        f"\n## [{note.timestamp}] App #{note.application_id} "
        f"— {note.company} / {note.title}\n"
    )
    lines = [header]
    for item in items:
        lines.append(f"- {item}")
    lines.append("")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _append_master(path: str, note: LearningNote) -> None:
    score_str = f"{note.score:.1f}" if note.score is not None else "n/a"
    lines = [
        f"\n## [{note.timestamp}] App #{note.application_id}",
        f"- Company: {note.company}",
        f"- Role:    {note.title}",
        f"- Portal:  {note.portal}",
        f"- Score:   {score_str}",
        f"- State:   {note.state}",
    ]
    if note.portal_quirks:
        lines.append(f"- Quirks:  {'; '.join(note.portal_quirks)}")
    if note.blockers:
        lines.append(f"- Blocked: {'; '.join(note.blockers)}")
    lines.append("")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
