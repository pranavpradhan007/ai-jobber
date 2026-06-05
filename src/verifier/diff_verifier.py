"""
Diff-verifier.

For each sentence in generated text, extracts claims (metrics, tools, titles,
credentials, companies, keywords) and verifies each against source_bank.

Claim extraction is done by a pluggable extractor function so tests can inject
a deterministic mock — no live model calls in tests.

The default extractor (used at runtime) calls the Anthropic API.
Tests inject extract_claims_mock().

Public API:
  verify_text(conn, app_id, text, surface, extractor=None) -> VerifierReport
  verifier_hard_gate(conn, app_id) -> None  (raises VerifierGateError if any claim blocked)
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from .retrieval import get_bank_item, is_permitted

logger = logging.getLogger(__name__)

# Type alias for the extractor function signature
# extractor(sentence: str) -> list[dict]
# Each dict: {"text": str, "type": str}  where type ∈ {metric,tool,credential,title,company,keyword}
ExtractorFn = Callable[[str], list[dict]]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ClaimResult:
    sentence: str
    claim_text: str
    claim_type: str
    source_bank_id: Optional[int]
    allowed: bool
    needs_approval: bool


@dataclass
class VerifierReport:
    application_id: int
    surface: str
    total_sentences: int
    claims: list[ClaimResult] = field(default_factory=list)
    passed: bool = True

    @property
    def blocked_claims(self) -> list[ClaimResult]:
        return [c for c in self.claims if not c.allowed]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class VerifierGateError(Exception):
    """Raised by verifier_hard_gate when any claim is blocked."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_text(
    conn: sqlite3.Connection,
    app_id: int,
    text: str,
    surface: str,
    *,
    extractor: Optional[ExtractorFn] = None,
) -> VerifierReport:
    """
    Verify all claims in `text` for `surface` against source_bank.
    Writes results to the `claims` table.
    Returns a VerifierReport.
    """
    if extractor is None:
        extractor = _default_extractor

    sentences = _split_sentences(text)
    report = VerifierReport(
        application_id=app_id,
        surface=surface,
        total_sentences=len(sentences),
    )

    for sentence in sentences:
        raw_claims = extractor(sentence)
        for raw in raw_claims:
            claim_text = raw.get("text", "").strip()
            claim_type = raw.get("type", "keyword")
            if not claim_text:
                continue

            result = _evaluate_claim(
                conn, app_id, sentence, claim_text, claim_type, surface
            )
            report.claims.append(result)
            if not result.allowed:
                report.passed = False

    return report


def verifier_hard_gate(conn: sqlite3.Connection, app_id: int) -> None:
    """
    Called by the state machine before any SUBMITTING transition.
    Raises VerifierGateError if any claim for this application is blocked.
    """
    cur = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE application_id=? AND allowed=0",
        (app_id,),
    )
    blocked_count = cur.fetchone()[0]
    if blocked_count:
        msg = (
            f"Verifier hard gate: {blocked_count} blocked claim(s) for "
            f"application {app_id}. Cannot advance to SUBMITTING."
        )
        logger.error(msg)
        raise VerifierGateError(msg)
    logger.info("verifier_hard_gate passed for application %d", app_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _evaluate_claim(
    conn: sqlite3.Connection,
    app_id: int,
    sentence: str,
    claim_text: str,
    claim_type: str,
    surface: str,
) -> ClaimResult:
    """Look up one claim in source_bank and write a `claims` row."""
    bank_item = get_bank_item(conn, claim_text)

    if bank_item is None:
        # Claim not in source_bank at all — blocked, needs approval
        allowed = False
        needs_approval = True
        source_bank_id = None
        logger.warning(
            "BLOCKED claim app_id=%d surface=%s type=%s claim=%r (not in source_bank)",
            app_id, surface, claim_type, claim_text,
        )
    elif not is_permitted(bank_item.usage_level, surface):
        # Found but usage_level forbids this surface
        allowed = False
        needs_approval = True
        source_bank_id = bank_item.id
        logger.warning(
            "BLOCKED claim app_id=%d surface=%s type=%s claim=%r "
            "(usage_level=%s forbids surface)",
            app_id, surface, claim_type, claim_text, bank_item.usage_level,
        )
    else:
        allowed = True
        needs_approval = False
        source_bank_id = bank_item.id
        logger.debug(
            "ALLOWED claim app_id=%d surface=%s type=%s claim=%r",
            app_id, surface, claim_type, claim_text,
        )

    # Write to claims table (always — for audit trail)
    conn.execute(
        """
        INSERT INTO claims
            (application_id, surface, sentence, claim_text, claim_type,
             source_bank_id, allowed, needs_approval)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (app_id, surface, sentence[:500], claim_text, claim_type,
         source_bank_id, int(allowed), int(needs_approval)),
    )

    # High-stakes blocked claims also get an approvals record
    if not allowed and claim_type in ("metric", "credential", "title"):
        conn.execute(
            """
            INSERT INTO approvals (application_id, approval_type, status)
            VALUES (?, 'claim', 'pending')
            """,
            (app_id,),
        )

    conn.commit()

    return ClaimResult(
        sentence=sentence,
        claim_text=claim_text,
        claim_type=claim_type,
        source_bank_id=source_bank_id,
        allowed=allowed,
        needs_approval=needs_approval,
    )


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences. Simple heuristic — adequate for resume/CL text."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


def _default_extractor(sentence: str) -> list[dict]:
    """
    Runtime extractor: calls the Anthropic API.
    NOT used in tests — inject a mock extractor instead.
    Lazy import so tests never touch the Anthropic SDK.
    """
    import os
    import anthropic  # noqa: PLC0415

    from src.config import DEFAULT_MODEL  # noqa: PLC0415
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = (
        "Extract all specific claims from this sentence. "
        "A claim is a metric, tool/technology name, job title, credential, "
        "company name, or specific skill keyword. "
        "Return JSON array: [{\"text\": \"<claim>\", \"type\": \"<metric|tool|credential|title|company|keyword>\"}]. "
        "If no claims, return []. Sentence: " + sentence
    )
    msg = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    try:
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError):
        logger.warning("Extractor returned non-JSON: %r", raw[:200])
        return []
