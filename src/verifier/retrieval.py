"""
Source-bank retrieval API.

Returns only items whose usage_level permits the requested surface.
Surface hierarchy (most permissive → most restrictive):
  resume > cover_letter > screener > private_never_submit (never permitted)

A bank item with usage_level='resume' is permitted on resume, cover_letter,
and screener surfaces. An item with usage_level='screener' is permitted
only on screener.  'private_never_submit' is blocked everywhere.
"""
from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Higher number = more permissive (can appear on lower-number surfaces too)
_LEVEL_RANK: dict[str, int] = {
    "private_never_submit": -1,   # never permitted on any surface
    "screener":              1,
    "cover_letter":          2,
    "resume":                3,
}

_SURFACE_RANK: dict[str, int] = {
    "screener":     1,
    "cover_letter": 2,
    "resume":       3,
}


@dataclass
class BankItem:
    id: int
    item_type: str
    content: str
    source_ref: Optional[str]
    usage_level: str
    notes: Optional[str]


class UsageLevelViolation(Exception):
    """Raised when a bank item's usage_level forbids the target surface."""


def is_permitted(usage_level: str, surface: str) -> bool:
    """
    Return True if a bank item with `usage_level` may appear on `surface`.
    private_never_submit is always False.
    """
    if usage_level == "private_never_submit":
        return False
    item_rank = _LEVEL_RANK.get(usage_level, 0)
    surf_rank = _SURFACE_RANK.get(surface, 0)
    # An item is permitted when its rank >= the surface rank
    # (i.e. resume-level items can go everywhere except private_never_submit)
    return item_rank >= surf_rank


def get_bank_item(
    conn: sqlite3.Connection,
    content: str,
    item_type: Optional[str] = None,
) -> Optional[BankItem]:
    """
    Exact-match lookup by content (case-insensitive).
    Optionally filter by item_type.
    Returns None if not found.
    """
    if item_type:
        cur = conn.execute(
            "SELECT * FROM source_bank "
            "WHERE LOWER(content) = LOWER(?) AND item_type = ?",
            (content, item_type),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM source_bank WHERE LOWER(content) = LOWER(?)",
            (content,),
        )
    row = cur.fetchone()
    return _row_to_item(row) if row else None


def retrieve_for_surface(
    conn: sqlite3.Connection,
    surface: str,
    item_type: Optional[str] = None,
    keywords: Optional[list[str]] = None,
) -> list[BankItem]:
    """
    Return all bank items permitted on `surface`.
    Optionally filter by item_type and/or keyword substring match.
    """
    if surface not in _SURFACE_RANK:
        raise ValueError(f"Unknown surface {surface!r}")

    # Collect permitted usage_levels for this surface
    permitted_levels = [
        lvl for lvl, rank in _LEVEL_RANK.items()
        if rank >= _SURFACE_RANK[surface]
    ]

    placeholders = ",".join("?" * len(permitted_levels))
    params: list = list(permitted_levels)

    type_clause = ""
    if item_type:
        type_clause = "AND item_type = ? "
        params.append(item_type)

    cur = conn.execute(
        f"SELECT * FROM source_bank "
        f"WHERE usage_level IN ({placeholders}) {type_clause}"
        f"ORDER BY item_type, content",
        params,
    )
    items = [_row_to_item(r) for r in cur.fetchall()]

    if keywords:
        lower_kw = [k.lower() for k in keywords]
        items = [
            it for it in items
            if any(kw in it.content.lower() for kw in lower_kw)
        ]

    return items


def check_item_for_surface(
    conn: sqlite3.Connection,
    content: str,
    surface: str,
    item_type: Optional[str] = None,
) -> BankItem:
    """
    Look up `content` in source_bank and verify it is permitted on `surface`.
    Raises UsageLevelViolation if found but not permitted.
    Raises KeyError if not found at all.
    """
    item = get_bank_item(conn, content, item_type)
    if item is None:
        raise KeyError(f"'{content}' not found in source_bank")
    if not is_permitted(item.usage_level, surface):
        raise UsageLevelViolation(
            f"'{content}' has usage_level={item.usage_level!r} "
            f"which is not permitted on surface={surface!r}"
        )
    return item


def _row_to_item(row) -> BankItem:
    return BankItem(
        id=row["id"],
        item_type=row["item_type"],
        content=row["content"],
        source_ref=row["source_ref"],
        usage_level=row["usage_level"],
        notes=row["notes"],
    )
