"""
Bullet retrieval for resume tailoring.

Selects source_bank items most relevant to the job's hot keywords.
Bullets are scored by how many hot keywords they contain, then ranked
per category — highest-relevance bullets go first.

1-page hard rule: total bullets are capped at MAX_TOTAL_BULLETS (14).
Distribution prioritises experience metrics (most impactful for ATS) then
tools, skills, credentials.
"""
from __future__ import annotations
import logging
import sqlite3

from src.verifier.retrieval import BankItem, retrieve_for_surface

logger = logging.getLogger(__name__)

# Per-category caps — must sum to <= MAX_TOTAL_BULLETS
MAX_METRICS     = 4
MAX_TOOLS       = 5
MAX_SKILLS      = 3
MAX_CREDENTIALS = 2
MAX_TOTAL_BULLETS = MAX_METRICS + MAX_TOOLS + MAX_SKILLS + MAX_CREDENTIALS  # 14


def retrieve_bullets(
    conn: sqlite3.Connection,
    hot_keywords: list[str],
    surface: str = "resume",
) -> dict[str, list[BankItem]]:
    """
    Retrieve source_bank items most relevant to hot_keywords.
    Returns a dict keyed by item_type with relevance-ranked BankItems.

    Selection strategy:
      1. Score each item by the number of hot_keywords contained in its content.
      2. Sort each category descending by relevance score.
      3. Take top-N per category (caps defined above).
      4. Total bullets capped at MAX_TOTAL_BULLETS to keep resume to 1 page.
    """
    kw_lower = [k.lower() for k in hot_keywords]

    result: dict[str, list[BankItem]] = {
        "metric": [],
        "tool": [],
        "skill": [],
        "credential": [],
        "title": [],
        "keyword": [],
    }

    all_items = retrieve_for_surface(conn, surface)

    # Score and bucket
    scored: dict[str, list[tuple[int, BankItem]]] = {k: [] for k in result}
    for item in all_items:
        content_lower = item.content.lower()
        score = sum(1 for kw in kw_lower if kw in content_lower)
        scored.setdefault(item.item_type, []).append((score, item))

    # Sort each bucket descending by relevance, then take top-N
    for itype, cap in (
        ("metric",     MAX_METRICS),
        ("tool",       MAX_TOOLS),
        ("skill",      MAX_SKILLS),
        ("credential", MAX_CREDENTIALS),
    ):
        ranked = sorted(scored.get(itype, []), key=lambda t: t[0], reverse=True)
        result[itype] = [item for _, item in ranked[:cap]]

    total = sum(len(v) for v in result.values())
    logger.info(
        "retrieved %d/%d bullets (hot_keywords=%d caps=m%d/t%d/s%d/c%d)",
        total, MAX_TOTAL_BULLETS,
        len(hot_keywords),
        MAX_METRICS, MAX_TOOLS, MAX_SKILLS, MAX_CREDENTIALS,
    )
    return result
