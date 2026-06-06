"""
Screener answer pre-computation engine.

Pre-computes ALL answers — static lookups and LLM-generated open-ended responses —
BEFORE the browser session opens, so there is zero LLM latency during DOM interaction.

Static answers: resolved from application_answers.json and EEO defaults.
Open-ended:    ONE batched queue_llm_task call for all open-ended categories,
               result cached to {folder_path}/screener_answers.json.
               If LLM call times out: safe fallback defaults are used.
"""
from __future__ import annotations
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safe open-ended fallbacks (used when LLM is unavailable or times out)
# ---------------------------------------------------------------------------
_OPEN_ENDED_SAFE_DEFAULTS: dict[str, str] = {
    "why_this_company": (
        "I am drawn to this company because of its work in AI and machine learning "
        "and the opportunity to contribute to impactful technology at scale."
    ),
    "why_this_role": (
        "My background in ML engineering, model training, and data pipelines aligns "
        "well with the requirements of this role, and I am excited to bring my skills "
        "to a team solving challenging technical problems."
    ),
    "strengths": (
        "I bring strong Python, deep learning (PyTorch, JAX), and data engineering "
        "skills combined with research experience in scientific ML and applied AI systems."
    ),
    "biggest_project": (
        "I developed ML pipelines for physics-informed neural networks at NYU, and "
        "contributed to a grid-agnostic JAX model for fluid dynamics simulations."
    ),
    "challenge_overcome": (
        "I had to re-architect a data pipeline under a tight deadline when our source "
        "schema changed mid-project. I collaborated with the team to redesign the ETL "
        "process and delivered the updated system on time."
    ),
    "tell_us_about": (
        "I am an ML engineer and researcher graduating from NYU Tandon with an MS in "
        "Computer Science in 2026. My work spans scientific ML, NLP, and data engineering "
        "across research and industry internship settings."
    ),
    "relevant_experience": (
        "I have 3-5 years of combined research and industry experience in machine learning, "
        "including model training, inference optimization, data pipelines, and applied AI."
    ),
}

# ---------------------------------------------------------------------------
# EEO defaults (imported from field_mapper to avoid duplication)
# ---------------------------------------------------------------------------
def _get_eeo_defaults() -> dict[str, str]:
    try:
        from src.browser.field_mapper import EEO_DEFAULTS
        return dict(EEO_DEFAULTS)
    except Exception:
        return {
            "eeo_gender":     "Male",
            "eeo_ethnicity":  "Asian",
            "eeo_veteran":    "I am not a protected veteran",
            "eeo_disability": "No, I don't have a disability",
        }

# ---------------------------------------------------------------------------
# Question bank: (category, regex, answer_key_or_handler)
#   answer_key:  key in candidate_answers dict (or _STATIC_SYNTHETIC below)
#   "_llm":      open-ended, needs LLM generation
# ---------------------------------------------------------------------------
_QUESTION_BANK: list[tuple[str, str, str]] = [
    # Work eligibility
    ("work_authorization",  r"authorized.{0,30}work|right.{0,10}work|eligible.{0,15}work|legally.{0,15}work|lawfully", "work_authorization"),
    ("sponsorship",         r"sponsor|visa.{0,20}sponsor|require.{0,10}sponsor|need.{0,10}sponsor",                    "requires_sponsorship"),
    ("us_authorized",       r"us.{0,5}citizen|permanent.{0,5}resid|green.{0,5}card",                                  "us_citizen_or_pr"),

    # Salary
    ("salary",              r"salary|compensation|expected.{0,15}pay|desired.{0,10}pay|pay.{0,15}expect",              "salary_expectation"),

    # Logistics
    ("years_experience",    r"years?.{0,10}experience|how.{0,15}years|experience.{0,10}years",                        "years_experience"),
    ("start_date",          r"start.{0,10}date|available.{0,15}start|when.{0,15}start|earliest.{0,10}start",          "_start_date"),
    ("background_check",    r"background.{0,10}check|consent.{0,20}background|drug.{0,10}test",                       "_yes"),
    ("remote_preference",   r"remote|work.{0,15}remote|preference.{0,15}remote|remote.{0,15}work",                    "remote_preference"),
    ("relocation",          r"reloc|willing.{0,15}reloc|open.{0,10}reloc|relocate",                                   "willing_to_relocate"),
    ("education_level",     r"highest.{0,20}educ|degree|education.{0,15}level|highest.{0,10}degree",                  "education_degree"),
    ("graduation_year",     r"grad.{0,10}year|when.{0,15}graduat|expected.{0,15}grad|graduation",                     "graduation_year"),
    ("driver_license",      r"driver.{0,10}licen|driving.{0,10}licen|valid.{0,10}licen",                              "_no"),
    ("18_or_older",         r"18.{0,10}(years|or older)|over.{0,5}18|legal.{0,10}age|at least 18",                   "_yes"),

    # Contact / profile
    ("phone",               r"^phone$|phone.{0,10}number|mobile.{0,10}number|telephone",                              "phone"),
    ("linkedin",            r"linkedin|linkedin.{0,10}url|linkedin.{0,10}profile",                                    "linkedin_url"),
    ("github",              r"github|github.{0,10}url|github.{0,10}profile",                                          "github_url"),
    ("portfolio",           r"portfolio|personal.{0,10}website|website.{0,10}url",                                    "portfolio_url"),
    ("cover_letter",        r"cover.{0,10}letter|additional.{0,15}comment|anything.{0,20}add",                        "cover_letter_default"),

    # EEO
    ("gender",              r"^gender$|gender.{0,10}identity",                                                        "eeo_gender"),
    ("ethnicity",           r"^race$|^ethnicity$|ethnic.{0,15}group|racial",                                          "eeo_ethnicity"),
    ("veteran",             r"veteran|military.{0,15}status",                                                         "eeo_veteran"),
    ("disability",          r"disabilit",                                                                              "eeo_disability"),

    # Open-ended (LLM required)
    ("why_this_company",    r"why.{0,20}(us|company|organization|firm)|interest.{0,20}(us|company)",                  "_llm"),
    ("why_this_role",       r"why.{0,20}(role|position|job|this.{0,10}opport)",                                       "_llm"),
    ("strengths",           r"strength|what.{0,20}bring|superpower|best.{0,10}qualit",                                "_llm"),
    ("biggest_project",     r"biggest.{0,20}project|notable.{0,20}project|proud.{0,20}project|significant.{0,10}work","_llm"),
    ("challenge_overcome",  r"challenge|difficult.{0,20}(situation|problem)|obstacle|overcome",                       "_llm"),
    ("tell_us_about",       r"tell.{0,20}(us|me).{0,20}(yourself|background)|introduce|brief.{0,10}bio",             "_llm"),
    ("relevant_experience", r"relevant.{0,20}experience|describe.{0,20}experience|experience.{0,10}relevant",         "_llm"),
]

# Synthetic static values for answer keys that don't exist in application_answers.json
_STATIC_SYNTHETIC: dict[str, str] = {
    "_yes":        "Yes",
    "_no":         "No",
    "_start_date": "2 weeks",
}

# Compile regexes once at module load
_COMPILED_BANK: list[tuple[str, re.Pattern, str]] = [
    (cat, re.compile(rx, re.IGNORECASE), key)
    for cat, rx, key in _QUESTION_BANK
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class ScreenerResult:
    answers: dict       # normalized_label_or_category → answer_text
    cache_path: str
    llm_called: bool = False
    llm_timed_out: bool = False


def match_question_to_category(label_text: str) -> Optional[tuple[str, str]]:
    """
    Match a label string to a (category, answer_key) pair.
    Returns None if no pattern matches.
    Case-insensitive. Tries patterns in order.
    """
    if not label_text:
        return None
    norm = label_text.strip().lower()
    for cat, rx, key in _COMPILED_BANK:
        if rx.search(norm):
            return cat, key
    return None


def precompute_screener_answers(
    app_id: int,
    job_title: str,
    company: str,
    clean_jd: str,
    candidate_answers: dict,
    *,
    cache_dir: str = "",
    force_recompute: bool = False,
    llm_timeout: int = 90,
) -> ScreenerResult:
    """
    Pre-compute all screener answers before the browser session opens.

    1. Load from cache if available and not force_recompute.
    2. Build static answers from candidate_answers + EEO defaults + synthetic defaults.
    3. If any open-ended (_llm) categories are needed, batch all into ONE LLM call.
    4. Cache result to {cache_dir}/screener_answers.json.
    5. Return merged flat dict.
    """
    cache_path = str(Path(cache_dir) / "screener_answers.json") if cache_dir else ""
    eeo = _get_eeo_defaults()

    # Load cache
    if cache_path and not force_recompute:
        cached = _load_cache(cache_path)
        if cached is not None:
            logger.info("app_id=%d screener answers loaded from cache (%d entries)", app_id, len(cached))
            return ScreenerResult(answers=cached, cache_path=cache_path)

    # Build static answers
    merged: dict[str, str] = {}
    merged.update(eeo)
    merged.update({k: v for k, v in candidate_answers.items() if isinstance(v, str)})
    merged.update(_STATIC_SYNTHETIC)

    # Determine which open-ended categories might be needed
    llm_categories = [cat for cat, _, key in _QUESTION_BANK if key == "_llm"]
    llm_called = False
    llm_timed_out = False

    # Only call LLM if we have JD context to work with
    if job_title or company or clean_jd:
        llm_answers = _generate_open_ended(
            job_title, company, clean_jd, candidate_answers,
            llm_categories, llm_timeout, app_id,
        )
        if llm_answers is None:
            llm_timed_out = True
            logger.warning("app_id=%d screener LLM timed out — using safe defaults", app_id)
            llm_answers = {}
        else:
            llm_called = True

        # Fill in open-ended answers (LLM result > safe defaults)
        for cat in llm_categories:
            if cat in llm_answers and llm_answers[cat].strip():
                merged[cat] = llm_answers[cat].strip()
            else:
                merged[cat] = _OPEN_ENDED_SAFE_DEFAULTS.get(cat, "")
    else:
        # No JD context — use safe defaults
        for cat in llm_categories:
            merged[cat] = _OPEN_ENDED_SAFE_DEFAULTS.get(cat, "")

    if cache_path:
        _save_cache(cache_path, merged)

    logger.info(
        "app_id=%d screener precompute: %d answers llm_called=%s timed_out=%s",
        app_id, len(merged), llm_called, llm_timed_out,
    )
    return ScreenerResult(
        answers=merged,
        cache_path=cache_path,
        llm_called=llm_called,
        llm_timed_out=llm_timed_out,
    )


# ---------------------------------------------------------------------------
# LLM integration
# ---------------------------------------------------------------------------

def _generate_open_ended(
    job_title: str,
    company: str,
    clean_jd: str,
    candidate_answers: dict,
    categories: list[str],
    timeout: int,
    app_id: int,
) -> Optional[dict[str, str]]:
    """
    Batch all open-ended categories into ONE LLM call.
    Returns dict of {category: answer} or None on timeout/error.
    """
    try:
        from src.llm.claude_code_bridge import should_use_claude_code, queue_llm_task
    except ImportError:
        logger.warning("app_id=%d claude_code_bridge not available; skipping LLM screener", app_id)
        return None

    if not should_use_claude_code():
        logger.debug("app_id=%d USE_CLAUDE_CODE_FOR_LLM not set; using safe defaults", app_id)
        return None

    prompt = _build_open_ended_prompt(job_title, company, clean_jd, candidate_answers, categories)
    try:
        raw = queue_llm_task("rephrase", prompt, timeout_seconds=timeout)
        if raw:
            return _parse_llm_response(raw, categories)
    except Exception as exc:
        logger.warning("app_id=%d screener LLM call failed: %s", app_id, exc)
    return None


def _build_open_ended_prompt(
    job_title: str,
    company: str,
    clean_jd: str,
    candidate: dict,
    categories: list[str],
) -> str:
    jd_snippet = (clean_jd or "")[:1200]
    return (
        f"You are filling out a job application for a {job_title!r} role at {company!r}.\n\n"
        f"Job description (excerpt):\n{jd_snippet}\n\n"
        "Candidate background:\n"
        f"- Name: {candidate.get('full_name', 'Pranav Pradhan')}\n"
        f"- Current title: {candidate.get('current_title', 'AI Research Engineer Intern')}\n"
        f"- Experience: {candidate.get('years_experience', '3-5')} years in ML/AI engineering\n"
        f"- Education: {candidate.get('education_degree', 'MS Computer Science')} "
        f"from {candidate.get('education_school', 'NYU Tandon')}, {candidate.get('graduation_year', '2026')}\n\n"
        "Write brief, genuine answers (2-4 sentences each) for the following application questions.\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        f"{json.dumps({cat: '' for cat in categories}, indent=2)}\n\n"
        "Rules:\n"
        "1. Be factual — only use information provided above.\n"
        "2. Keep each answer under 150 words.\n"
        "3. Do not make up projects, tools, or facts not mentioned.\n"
        "4. Use first person ('I'). No em dashes. No AI buzzwords.\n"
        "5. Return only the JSON — no explanation or preamble.\n"
    )


def _parse_llm_response(raw: str, categories: list[str]) -> dict[str, str]:
    """Extract JSON dict from LLM response. Robust to extra prose."""
    # Try direct parse
    try:
        parsed = json.loads(raw.strip())
        if isinstance(parsed, dict):
            return {cat: str(parsed.get(cat, "")) for cat in categories}
    except Exception:
        pass

    # Try extracting JSON object from prose
    m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return {cat: str(parsed.get(cat, "")) for cat in categories}
        except Exception:
            pass

    logger.warning("screener: could not parse LLM JSON response")
    return {cat: "" for cat in categories}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache(path: str) -> Optional[dict]:
    try:
        p = Path(path)
        if p.is_file() and p.stat().st_size > 10:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    return None


def _save_cache(path: str, data: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("screener: could not save cache to %s: %s", path, exc)
