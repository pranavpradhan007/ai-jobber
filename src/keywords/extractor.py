"""
Keyword extractor.
Scans clean_jd for terms that appear in source_bank and produces hot_keywords.
Also accepts an optional LLM extractor for terms not in the bank (mocked in tests).
"""
from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ExtractorFn = Callable[[str], list[str]]


@dataclass
class KeywordMatch:
    keyword: str
    bank_item_id: Optional[int]   # None if not in source_bank
    in_bank: bool
    item_type: Optional[str]


@dataclass
class KeywordReport:
    hot_keywords: list[str]
    matches: list[KeywordMatch] = field(default_factory=list)


def extract_keywords(
    conn: sqlite3.Connection,
    clean_jd: str,
    *,
    extractor: Optional[ExtractorFn] = None,
    surface: str = "resume",
) -> KeywordReport:
    """
    Extract keywords from clean_jd.
    1. String-match all source_bank items against the JD text.
    2. Optionally call extractor(clean_jd) for additional terms.
    Returns a KeywordReport.
    """
    matches: list[KeywordMatch] = []
    jd_lower = clean_jd.lower()

    # Step 1: bank items present in JD
    cur = conn.execute(
        "SELECT id, content, item_type, usage_level FROM source_bank "
        "WHERE usage_level != 'private_never_submit'"
    )
    for row in cur.fetchall():
        if row["content"].lower() in jd_lower:
            matches.append(KeywordMatch(
                keyword=row["content"],
                bank_item_id=row["id"],
                in_bank=True,
                item_type=row["item_type"],
            ))

    # Step 2: LLM extractor for additional terms (mocked in tests)
    if extractor is not None:
        extra = extractor(clean_jd)
        existing = {m.keyword.lower() for m in matches}
        for kw in extra:
            if kw.lower() not in existing:
                # Look up in bank (may or may not exist)
                row2 = conn.execute(
                    "SELECT id, item_type FROM source_bank WHERE LOWER(content)=LOWER(?)",
                    (kw,),
                ).fetchone()
                matches.append(KeywordMatch(
                    keyword=kw,
                    bank_item_id=row2["id"] if row2 else None,
                    in_bank=row2 is not None,
                    item_type=row2["item_type"] if row2 else None,
                ))
                existing.add(kw.lower())

    hot = [m.keyword for m in matches]
    logger.info("extracted %d hot keywords from JD", len(hot))
    return KeywordReport(hot_keywords=hot, matches=matches)


def save_keywords(folder_path: str, report: KeywordReport) -> str:
    """Write hot_keywords.json to the application folder. Returns path."""
    path = os.path.join(folder_path, "hot_keywords.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "hot_keywords": report.hot_keywords,
                "matches": [
                    {
                        "keyword": m.keyword,
                        "bank_item_id": m.bank_item_id,
                        "in_bank": m.in_bank,
                        "item_type": m.item_type,
                    }
                    for m in report.matches
                ],
            },
            fh,
            indent=2,
        )
    return path
