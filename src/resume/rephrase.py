"""
Bounded rephrase module.

The rephraser may REWORD but must not ADD new claims.
All facts must originate from the source_bank items provided.

In tests: inject a deterministic mock rephraser.
At runtime: calls the Anthropic API with explicit instructions.
"""
from __future__ import annotations
import json
import logging
import os
import re
from typing import Callable, Optional

from src.verifier.retrieval import BankItem

logger = logging.getLogger(__name__)

# Type: Rephraser(bullets: list[BankItem], hot_keywords: list[str], job_title: str) -> list[str]
RephraserFn = Callable[[list[BankItem], list[str], str], list[str]]


def rephrase_bullets(
    bullets: list[BankItem],
    hot_keywords: list[str],
    job_title: str = "",
    *,
    rephraser: Optional[RephraserFn] = None,
    edit_instruction: Optional[str] = None,
) -> list[str]:
    """
    Rephrase bank bullets to highlight hot keywords.
    Returns a list of rephrased bullet strings.
    The rephraser MUST NOT introduce any claim not in `bullets`.

    edit_instruction: optional user directive from APP-N EDIT "..." reply.
    Passed to the rephraser prompt as an additional constraint.
    """
    if not bullets:
        return []
    if rephraser is None:
        # Wrap default rephraser to inject edit_instruction
        base = _default_rephraser
        if edit_instruction:
            def rephraser(b, kw, jt):  # noqa: E731
                return base(b, kw, jt, edit_instruction=edit_instruction)
        else:
            rephraser = base
    result = rephraser(bullets, hot_keywords, job_title)
    logger.info("rephrased %d bullets (edit=%s)", len(result), bool(edit_instruction))
    return result


def mock_rephraser_clean(
    bullets: list[BankItem],
    hot_keywords: list[str],
    job_title: str,
) -> list[str]:
    """Deterministic mock: rewrites each bullet as 'Utilised {content}.'"""
    return [f"Utilised {b.content}." for b in bullets]


def mock_rephraser_with_hallucination(
    bullets: list[BankItem],
    hot_keywords: list[str],
    job_title: str,
) -> list[str]:
    """Mock that injects a fake metric — verifier must catch this."""
    rephrased = [f"Utilised {b.content}." for b in bullets]
    rephrased.append("Increased revenue by 500% with a proprietary AI system.")
    return rephrased


def _default_rephraser(
    bullets: list[BankItem],
    hot_keywords: list[str],
    job_title: str,
    *,
    edit_instruction: Optional[str] = None,
) -> list[str]:
    """Runtime rephraser. NOT called in tests."""
    import anthropic  # noqa: PLC0415
    from src.config import DEFAULT_MODEL  # noqa: PLC0415

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    bullet_texts = [b.content for b in bullets]

    edit_clause = ""
    if edit_instruction:
        edit_clause = (
            f"\n\nAdditional instruction from candidate: {edit_instruction!r}. "
            "Apply this as a stylistic/emphasis direction but do NOT invent "
            "new facts, metrics, tools, or credentials."
        )

    prompt = (
        f"Rephrase these resume bullets for a {job_title!r} role. "
        f"Highlight these keywords where natural: {hot_keywords}. "
        "You may reword but MUST NOT add any new metrics, tools, titles, or "
        "credentials not present in the original bullets. "
        f"Return a JSON array of strings, one per bullet.{edit_clause}\n\n"
        f"Bullets:\n{json.dumps(bullet_texts, indent=2)}"
    )
    msg = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(r) for r in result]
    except json.JSONDecodeError:
        logger.error("Rephraser returned non-JSON: %r", raw[:200])
    return bullet_texts  # fallback: return originals unchanged
