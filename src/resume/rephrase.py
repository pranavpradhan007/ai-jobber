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
    """
    Deterministic mock: uses plain action verbs, no AI language, no em dashes.
    Cycles through action verbs so bullets look varied.
    """
    _VERBS = ["Built", "Trained", "Designed", "Deployed", "Improved",
              "Reduced", "Integrated", "Benchmarked", "Evaluated", "Wrote"]
    return [
        f"{_VERBS[i % len(_VERBS)]} {b.content}."
        for i, b in enumerate(bullets)
    ]


def mock_rephraser_with_hallucination(
    bullets: list[BankItem],
    hot_keywords: list[str],
    job_title: str,
) -> list[str]:
    """Mock that injects a fake metric — verifier must catch this."""
    rephrased = mock_rephraser_clean(bullets, hot_keywords, job_title)
    rephrased.append("Increased revenue by 500% with a proprietary AI system.")
    return rephrased


def _default_rephraser(
    bullets: list[BankItem],
    hot_keywords: list[str],
    job_title: str,
    *,
    edit_instruction: Optional[str] = None,
) -> list[str]:
    """Runtime rephraser. Calls Claude via Claude Code bridge or direct API."""
    from src.config import DEFAULT_MODEL  # noqa: PLC0415
    from src.llm.claude_code_bridge import should_use_claude_code, queue_llm_task  # noqa: PLC0415

    bullet_texts = [b.content for b in bullets]

    edit_clause = ""
    if edit_instruction:
        edit_clause = (
            f"\n\nAdditional instruction from candidate: {edit_instruction!r}. "
            "Apply this as a stylistic/emphasis direction but do NOT invent "
            "new facts, metrics, tools, or credentials."
        )

    prompt = (
        f"Rephrase these resume bullets for a {job_title!r} role.\n"
        f"Emphasise these keywords where natural: {hot_keywords}.\n"
        "\n"
        "STRICT RULES — violations cause the output to be rejected:\n"
        "1. NEVER use em dashes (—). Use commas, colons, or rewrite.\n"
        "2. NEVER use these words: leverage, utilize, utilise, delve, foster,\n"
        "   facilitate, streamline, empower, spearhead, orchestrate, harness,\n"
        "   robust, innovative, cutting-edge, transformative, synergy,\n"
        "   seamlessly, impactful, passionate, excited, thrilled, world-class,\n"
        "   exceptional, outstanding, ground-breaking.\n"
        "3. NEVER start a bullet with 'Utilized', 'Leveraged', or 'Facilitated'.\n"
        "4. NEVER add facts, metrics, tools, titles, or credentials not in the\n"
        "   original bullets.\n"
        "5. Write like a human engineer — short, specific, past-tense action verbs:\n"
        "   built, trained, reduced, improved, integrated, deployed, designed,\n"
        "   benchmarked, evaluated, extended, wrote, shipped, led, ran.\n"
        "6. Each bullet: one clear idea, specific numbers where available.\n"
        "\n"
        f"Return a JSON array of strings, one string per bullet.{edit_clause}\n"
        "\n"
        f"Bullets:\n{json.dumps(bullet_texts, indent=2)}"
    )

    # Use Claude Code if configured, else direct API
    if should_use_claude_code():
        raw = queue_llm_task("rephrase", prompt, model=DEFAULT_MODEL)
        if not raw:
            logger.warning("Claude Code rephrase timed out, returning originals")
            return bullet_texts
    else:
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

    # Parse response
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(r) for r in result]
    except json.JSONDecodeError:
        logger.error("Rephraser returned non-JSON: %r", raw[:200])
    return bullet_texts  # fallback: return originals unchanged
