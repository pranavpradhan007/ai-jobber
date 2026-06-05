"""
Gmail outcome classifier.

Maps an inbound email (subject + body snippet) to one of:
  receipt | rejection | oa | interview | positive_reply | unknown

Design:
  - ClassifierFn is injected so tests use deterministic keyword-based mocks.
  - The default runtime classifier calls the Anthropic API (never called in tests).
  - Low-confidence results produce 'unknown' and preserve the raw excerpt
    for human review — never a confident wrong call.
  - Confidence threshold: 0.7. Below that → unknown.
"""
from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = frozenset(
    {"receipt", "rejection", "oa", "interview", "positive_reply", "unknown"}
)

CONFIDENCE_THRESHOLD = 0.7


@dataclass
class ClassificationResult:
    event_type: str          # one of VALID_EVENT_TYPES
    confidence: float        # 0.0 – 1.0
    raw_excerpt: str         # first 500 chars of body (always kept)


# ClassifierFn: (subject: str, body: str) -> ClassificationResult
ClassifierFn = Callable[[str, str], ClassificationResult]


def classify_email(
    subject: str,
    body: str,
    *,
    classifier: Optional[ClassifierFn] = None,
) -> ClassificationResult:
    """
    Classify one inbound email.
    Returns a ClassificationResult with event_type and confidence.
    """
    if classifier is None:
        classifier = _default_classifier

    result = classifier(subject, body)

    # Enforce: low-confidence → unknown (never a confident wrong call)
    if result.confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            "Low-confidence classification %.2f for %r → unknown",
            result.confidence, subject,
        )
        return ClassificationResult(
            event_type="unknown",
            confidence=result.confidence,
            raw_excerpt=result.raw_excerpt,
        )

    if result.event_type not in VALID_EVENT_TYPES:
        logger.warning("Classifier returned unknown event_type %r", result.event_type)
        return ClassificationResult(
            event_type="unknown",
            confidence=0.0,
            raw_excerpt=result.raw_excerpt,
        )

    return result


# ---------------------------------------------------------------------------
# Deterministic keyword classifier (used as test mock and as a fast fallback)
# ---------------------------------------------------------------------------

def keyword_classifier(subject: str, body: str) -> ClassificationResult:
    """
    Simple keyword-based classifier. Deterministic — used in all tests.
    Not a live LLM call.
    """
    text = (subject + " " + body).lower()
    excerpt = (subject + " " + body)[:500]

    # Priority order: interview > oa > positive_reply > rejection > receipt
    # OA must be checked before positive_reply because OA emails often open
    # with "Congratulations" which would otherwise trigger positive_reply.
    if any(kw in text for kw in (
        "interview", "schedule a call", "meet with", "zoom call",
        "phone screen", "technical interview", "onsite", "virtual interview"
    )):
        return ClassificationResult("interview", 0.92, excerpt)

    if any(kw in text for kw in (
        "online assessment", "coding challenge", "hackerrank",
        "take-home", "technical test", "assessment link",
    )):
        return ClassificationResult("oa", 0.85, excerpt)

    if any(kw in text for kw in (
        "excited to move forward", "pleased to inform", "offer",
        "congratulations", "next steps", "we'd love to", "positive",
        "impressed with your background",
    )):
        return ClassificationResult("positive_reply", 0.88, excerpt)

    if any(kw in text for kw in (
        "not moving forward", "not selected", "other candidates",
        "position has been filled", "unfortunately", "regret to inform",
        "will not be moving", "decided to pursue", "thank you for applying",
        "no longer being considered",
    )):
        return ClassificationResult("rejection", 0.90, excerpt)

    if any(kw in text for kw in (
        "received your application", "application received",
        "thank you for your interest", "we have received",
        "confirming receipt", "application submitted",
    )):
        return ClassificationResult("receipt", 0.85, excerpt)

    # Below threshold — unknown
    return ClassificationResult("unknown", 0.30, excerpt)


# ---------------------------------------------------------------------------
# Default runtime classifier (Anthropic API — not called in tests)
# ---------------------------------------------------------------------------

def _default_classifier(subject: str, body: str) -> ClassificationResult:
    """Runtime classifier. NOT used in tests."""
    import anthropic  # noqa: PLC0415
    import json

    excerpt = (subject + " " + body)[:500]
    from src.config import DEFAULT_MODEL  # noqa: PLC0415
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = (
        "Classify this job application email into exactly one category.\n"
        "Categories: receipt | rejection | oa | interview | positive_reply | unknown\n"
        "Return JSON: {\"event_type\": \"<category>\", \"confidence\": <0.0-1.0>}\n\n"
        f"Subject: {subject}\nBody: {body[:800]}"
    )
    msg = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        parsed = json.loads(raw)
        return ClassificationResult(
            event_type=parsed.get("event_type", "unknown"),
            confidence=float(parsed.get("confidence", 0.0)),
            raw_excerpt=excerpt,
        )
    except (json.JSONDecodeError, ValueError):
        logger.warning("classifier non-JSON response: %r", raw[:100])
        return ClassificationResult("unknown", 0.0, excerpt)
