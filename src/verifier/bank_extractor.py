"""
Bank-pattern extractor — no LLM / no API key required.

Scans each sentence for exact source_bank content strings and returns
them as claim dicts. Used as the `extractor` argument to verify_text()
in tests and in the no-API-key pipeline path.

Any term found in source_bank is treated as "allowed" by definition
(it came from the bank). Terms NOT in the bank that look like metrics
or credentials are flagged for review.
"""
from __future__ import annotations
import re
import sqlite3
from typing import Optional


# Patterns that look like invented metrics (numbers + % / x / times)
_METRIC_RE = re.compile(
    r'(?:reduced?|improved?|increased?|decreased?|achieved?|cut|saved?)'
    r'[^.!?]{0,40}'
    r'(?:\d+(?:\.\d+)?\s*(?:%|percent|x\b|times|×))',
    re.IGNORECASE,
)


def make_bank_extractor(conn: sqlite3.Connection):
    """
    Return an ExtractorFn that scans sentences for source_bank matches.
    Captures bank items at construction time.
    """
    cur = conn.execute("SELECT content, item_type FROM source_bank")
    bank = [(row["content"], row["item_type"]) for row in cur.fetchall()]
    # Sort longest first so multi-word matches beat single-word substrings
    bank.sort(key=lambda x: len(x[0]), reverse=True)

    def extractor(sentence: str) -> list[dict]:
        found = []
        seen_spans: list[tuple[int, int]] = []
        s_lower = sentence.lower()

        for content, item_type in bank:
            idx = s_lower.find(content.lower())
            if idx == -1:
                continue
            end = idx + len(content)
            # Skip if span overlaps an already-matched span
            if any(s <= idx < e or s <= end <= e for s, e in seen_spans):
                continue
            seen_spans.append((idx, end))
            found.append({"text": content, "type": item_type})

        # Also flag metric-like phrases NOT in the bank
        for m in _METRIC_RE.finditer(sentence):
            phrase = m.group(0).strip()
            phrase_lower = phrase.lower()
            if not any(phrase_lower in c.lower() for c, _ in bank):
                # Only add if not already captured
                if phrase_lower not in {f["text"].lower() for f in found}:
                    found.append({"text": phrase, "type": "metric"})

        return found

    return extractor
