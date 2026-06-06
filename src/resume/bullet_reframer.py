"""
Bullet reframer — rewrites bullet text to match JD emphasis.

Rules:
- NEVER change format or design (font, bold, italic, size, spacing)
- NEVER add facts, metrics, tools, or credentials not in the original bullet
- NEVER use em dashes, AI buzzwords, or passive voice
- Ground truth DOCX is NEVER touched; this always runs on the duplicate
- Bullets with zero runs or unrecognised structure are left unchanged

Approach:
  1. Collect all List Paragraph + experience Normal bullets as raw text
  2. Send the batch to the rephraser (LLM) with JD keywords + strict rules
  3. Write new text back into the runs:
     - Single-run bullet: replace run[0].text
     - Multi-run bullet (same formatting across runs): put full text in run[0], clear others
     - Mixed-format runs (e.g. one bold): fall back to proportional distribution
"""
from __future__ import annotations
import copy
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Paragraph styles that contain experience bullets
_BULLET_STYLES = {"List Paragraph"}

# Paragraphs in the experience section that use Normal style but ARE bullets
# (3D Engineering Automation uses Normal style for its bullets — detected by position)
_EXPERIENCE_NORMAL_RANGE = range(24, 27)  # paragraphs 24-26 in Pranav's resume

# Blacklist: never emit these words (also enforced in the LLM prompt)
_BLACKLIST = re.compile(
    r"\b(leverage[sd]?|utiliz[es]?d?|utilise[sd]?|delve[sd]?|foster[s]?|"
    r"streamline[sd]?|empower[s]?|spearhead[s]?|orchestrate[sd]?|harness[es]?|"
    r"synerg[iy]|seamless|impactful|cutting.edge|transformative|proactive)\b",
    re.IGNORECASE,
)

# Em-dash variants
_EM_DASHES = re.compile(r"[—–]")


def reframe_bullets_in_docx(
    docx_path: str,
    hot_keywords: list[str],
    job_title: str = "",
    rephraser_fn: Optional[Callable] = None,
    cache_path: Optional[str] = None,
    partial_only: bool = False,
) -> int:
    """
    Reframe all bullet points in the DOCX file at `docx_path` in-place.
    Returns the number of bullets reframed.
    The file at docx_path MUST be a copy — ground truth is never passed here.
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx not installed; skipping bullet reframing")
        return 0

    doc = Document(docx_path)
    bullet_paras = _collect_bullets(doc)

    if not bullet_paras:
        logger.warning("No bullet paragraphs found in %s", docx_path)
        return 0

    # Build raw texts
    originals = [_para_text(p) for _, p in bullet_paras]
    kw_lower = [k.lower() for k in hot_keywords]

    # partial_only: only reframe bullets that contain none of the hot keywords
    if partial_only:
        reframe_mask = [
            not any(kw in text.lower() for kw in kw_lower)
            for text in originals
        ]
    else:
        reframe_mask = [True] * len(originals)

    # Load from cache file if provided (used for interactive/demo runs)
    if cache_path:
        import json as _json
        import pathlib as _pl
        cp = _pl.Path(cache_path)
        if cp.is_file():
            try:
                reframed = _json.loads(cp.read_text(encoding="utf-8"))
                logger.info("Loaded %d reframed bullets from cache %s", len(reframed), cache_path)
            except Exception as exc:
                logger.warning("Cache load failed (%s); calling rephraser", exc)
                reframed = _call_rephraser(originals, hot_keywords, job_title, rephraser_fn)
        else:
            reframed = _call_rephraser(originals, hot_keywords, job_title, rephraser_fn)
    else:
        # Reframe via LLM or passthrough
        reframed = _call_rephraser(originals, hot_keywords, job_title, rephraser_fn)

    if len(reframed) != len(originals):
        logger.warning(
            "Rephraser returned %d bullets, expected %d — using originals",
            len(reframed), len(originals),
        )
        return 0

    changed = 0
    for (idx, para), new_text, do_reframe in zip(bullet_paras, reframed, reframe_mask):
        if not do_reframe:
            continue
        new_text = _clean(new_text)
        old_text = _para_text(para)
        if not new_text.strip() or new_text == old_text:
            continue
        # Never let a reframed bullet grow longer than the original —
        # that directly risks pushing the resume to 2 pages
        if len(new_text) > len(old_text) + 10:
            logger.debug("bullet[%d] reframe rejected (grew %d→%d chars)", idx, len(old_text), len(new_text))
            continue
        _write_text_to_para(para, new_text)
        changed += 1
        logger.debug("bullet[%d] reframed:\n  OLD: %s\n  NEW: %s", idx, old_text[:80], new_text[:80])

    doc.save(docx_path)
    logger.info("reframed %d/%d bullets in %s", changed, len(originals), docx_path)
    return changed


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collect_bullets(doc) -> list[tuple[int, object]]:
    """Return (index, paragraph) for all bullet-style paragraphs."""
    result = []
    for i, para in enumerate(doc.paragraphs):
        if para.style.name in _BULLET_STYLES:
            text = para.text.strip()
            if text:
                result.append((i, para))
        elif para.style.name == "Normal" and i in _EXPERIENCE_NORMAL_RANGE:
            text = para.text.strip()
            if text:
                result.append((i, para))
    return result


def _para_text(para) -> str:
    return "".join(r.text for r in para.runs)


def _write_text_to_para(para, new_text: str):
    """
    Write new_text into the paragraph's runs while preserving run formatting.

    Strategy:
    - All-same-format runs (common in this resume): put everything in run[0], clear rest
    - Mixed format: proportional distribution (best-effort)
    """
    runs = para.runs
    if not runs:
        return

    # Check if all runs share the same bold/italic/size
    sample_bold   = runs[0].bold
    sample_italic = runs[0].italic
    sample_size   = runs[0].font.size
    uniform = all(
        r.bold == sample_bold and r.italic == sample_italic and r.font.size == sample_size
        for r in runs
    )

    if uniform or len(runs) == 1:
        # Simplest path: dump everything into run[0], blank out the rest
        runs[0].text = new_text
        for r in runs[1:]:
            r.text = ""
    else:
        # Mixed formatting — distribute proportionally by character count
        original_text = _para_text(para)
        total_old = len(original_text) or 1
        total_new = len(new_text)

        pos = 0
        for i, r in enumerate(runs):
            if i == len(runs) - 1:
                r.text = new_text[pos:]
            else:
                share = round(len(r.text) / total_old * total_new)
                r.text = new_text[pos: pos + share]
                pos += share


def _call_rephraser(
    originals: list[str],
    hot_keywords: list[str],
    job_title: str,
    rephraser_fn,
) -> list[str]:
    """Call the rephraser function or fall back to the default Anthropic rephraser."""
    if rephraser_fn is not None:
        # Rephraser expects BankItem list — wrap raw text strings
        from src.verifier.retrieval import BankItem
        items = [
            BankItem(item_type="experience", value=t, source="ground_truth_resume", usage_level="resume")
            for t in originals
        ]
        try:
            result = rephraser_fn(items, hot_keywords, job_title)
            if isinstance(result, list) and len(result) == len(originals):
                return result
        except Exception as exc:
            logger.warning("rephraser_fn failed: %s — using direct API", exc)

    # Direct Anthropic API call (runtime path)
    return _anthropic_reframe(originals, hot_keywords, job_title)


def _anthropic_reframe(
    originals: list[str],
    hot_keywords: list[str],
    job_title: str,
) -> list[str]:
    """Call Anthropic API to reframe bullets. Falls back to originals on error."""
    import json, os

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic not installed; returning originals unchanged")
        return originals

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set; returning originals unchanged")
        return originals

    prompt = f"""Reframe these resume bullets for a {job_title!r} role.
Emphasise these JD keywords where they fit naturally: {hot_keywords}.

STRICT RULES — any violation means the output is REJECTED:
1. Keep every fact, number, metric, and tool from the original bullet.
2. NEVER add claims, tools, metrics, or credentials not in the original.
3. NEVER use em dashes. Use commas or colons instead.
4. NEVER use: leverage, utilize, utilise, delve, foster, streamline, empower,
   spearhead, orchestrate, harness, synergy, seamless, impactful, cutting-edge,
   transformative, robust, innovative, passionate, excited, thrilled.
5. Start each bullet with a strong past-tense action verb:
   built, trained, reduced, improved, integrated, deployed, designed,
   benchmarked, evaluated, extended, wrote, shipped, led, ran, developed,
   implemented, constructed, tested, optimized, scaled.
6. Google resume formula: [Verb] [what you did] [how/with what] [result/impact].
7. One idea per bullet. Keep it tight — no filler words.
8. Return a JSON array of strings, exactly {len(originals)} items.

Bullets to reframe:
{json.dumps(originals, indent=2)}"""

    try:
        from src.config import DEFAULT_MODEL
        model = DEFAULT_MODEL
    except Exception:
        model = "claude-sonnet-4-6"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Extract JSON array
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list) and len(parsed) == len(originals):
                return [str(x) for x in parsed]
        logger.warning("Unexpected rephraser output shape; using originals")
    except Exception as exc:
        logger.error("Anthropic reframe failed: %s", exc)

    return originals


def _clean(text: str) -> str:
    """Strip em dashes and blacklisted words from reframed text."""
    text = _EM_DASHES.sub(",", text)
    # Remove blacklisted words at sentence start
    text = re.sub(r"^(Leveraged|Utilized|Utilised|Facilitated|Spearheaded)\s+", "", text, flags=re.IGNORECASE)
    return text.strip()
