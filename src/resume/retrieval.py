"""
Bullet retrieval for resume tailoring.
Selects source_bank items relevant to the job's hot keywords.
Returns ordered list of BankItems to use as tailoring inputs.
"""
from __future__ import annotations
import logging
import sqlite3
from typing import Optional

from src.verifier.retrieval import BankItem, retrieve_for_surface

logger = logging.getLogger(__name__)

# How many bullets to select per item type
MAX_METRICS = 4
MAX_TOOLS = 8
MAX_SKILLS = 6
MAX_CREDENTIALS = 3


def retrieve_bullets(
    conn: sqlite3.Connection,
    hot_keywords: list[str],
    surface: str = "resume",
) -> dict[str, list[BankItem]]:
    """
    Retrieve source_bank items that best match the hot_keywords for a surface.
    Returns a dict keyed by item_type with ranked lists of BankItems.
    """
    kw_lower = {k.lower() for k in hot_keywords}
    result: dict[str, list[BankItem]] = {
        "metric": [],
        "tool": [],
        "skill": [],
        "credential": [],
        "title": [],
        "keyword": [],
    }

    all_items = retrieve_for_surface(conn, surface)

    for item in all_items:
        # Prioritise items whose content matches a hot keyword
        if item.content.lower() in kw_lower:
            result.setdefault(item.item_type, []).insert(0, item)
        else:
            result.setdefault(item.item_type, []).append(item)

    # Trim to limits
    result["metric"] = result.get("metric", [])[:MAX_METRICS]
    result["tool"] = result.get("tool", [])[:MAX_TOOLS]
    result["skill"] = result.get("skill", [])[:MAX_SKILLS]
    result["credential"] = result.get("credential", [])[:MAX_CREDENTIALS]

    total = sum(len(v) for v in result.values())
    logger.info(
        "retrieved %d bank items for tailoring (hot_keywords=%d)",
        total, len(hot_keywords),
    )
    return result
