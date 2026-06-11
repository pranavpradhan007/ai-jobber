"""
Heuristic scorer — no LLM / no API key required.

Uses source_bank keyword matching against the JD to produce a real weighted
scorecard. Good for testing the pipeline and for cheap pre-filtering before
a more expensive model pass.

Usage:
    from src.scoring.heuristic_scorer import make_heuristic_scorer
    scorer = make_heuristic_scorer(conn)
    scorecard = score_job(job_info, profile_info, scorer=scorer)
"""
from __future__ import annotations
import re
import sqlite3
from typing import Optional


# Seniority: over-senior titles get a score penalty; mid/entry get a bonus.
_SENIOR_TITLE_WORDS = {"senior", "principal", "staff", "lead", "director",
                       "head of", "vp ", "vice president", "distinguished", "fellow"}
_MID_TITLE_WORDS    = {"mid", "ii", "iii", "2", "3", "associate", "junior",
                       "entry", "early career", "new grad", "graduate"}

# Role-family title keywords → used for title_match
_TITLE_KEYWORDS = {
    "high": {
        "machine learning engineer", "ml engineer", "applied scientist",
        "research engineer", "ai engineer", "applied ai", "llm engineer",
        "ai/ml", "ml infrastructure", "scientific ml",
    },
    "med": {
        "data scientist", "software engineer", "data engineer",
        "computer vision", "nlp engineer", "research scientist",
        "backend engineer", "platform engineer",
    },
    "low": {
        "frontend", "product manager", "devops", "marketing", "sales",
    },
}

# Remote signals in JD
_REMOTE_SIGNALS = {"remote", "distributed", "work from home", "wfh", "anywhere"}

# Domain/industry signals that match Pranav's profile
_DOMAIN_SIGNALS = {
    "machine learning", "deep learning", "neural", "llm", "language model",
    "reinforcement learning", "computer vision", "nlp", "ai", "ml",
    "research", "scientific", "simulation", "game",
}


def make_heuristic_scorer(conn: sqlite3.Connection):
    """
    Return a ScorerFn that scores jobs without any API call.
    Captures source_bank items at construction time.
    """
    # Load all resume-level bank items
    cur = conn.execute(
        "SELECT content, item_type FROM source_bank "
        "WHERE usage_level IN ('resume','cover_letter','screener')"
    )
    bank_items = [(row["content"].lower(), row["item_type"]) for row in cur.fetchall()]

    tools     = [c for c, t in bank_items if t == "tool"]
    skills    = [c for c, t in bank_items if t == "skill"]
    keywords  = [c for c, t in bank_items if t == "keyword"]
    all_items = [c for c, _ in bank_items]

    def scorer(job_info: dict, profile_info: dict) -> dict:
        jd   = (job_info.get("clean_jd") or job_info.get("raw_jd") or "").lower()
        jtitle = (job_info.get("title") or "").lower()
        loc  = (job_info.get("location") or "").lower()
        is_remote = job_info.get("remote", 0) == 1 or any(s in jd for s in _REMOTE_SIGNALS)

        # ── title_match (25%) ────────────────────────────────────────────────
        if any(k in jtitle for k in _TITLE_KEYWORDS["high"]):
            title_score = 92
            title_reason = f"'{jtitle}' is a strong target role match"
        elif any(k in jtitle for k in _TITLE_KEYWORDS["med"]):
            title_score = 70
            title_reason = f"'{jtitle}' is a reasonable match"
        elif any(k in jtitle for k in _TITLE_KEYWORDS["low"]):
            title_score = 20
            title_reason = f"'{jtitle}' is a poor fit for this profile"
        else:
            title_score = 55
            title_reason = f"'{jtitle}' — unclear match"

        # Seniority adjustment: penalise over-senior, reward mid/entry
        if any(w in jtitle for w in _SENIOR_TITLE_WORDS):
            title_score = max(0, title_score - 12)
            title_reason += " [−12 senior-level penalty]"
        elif any(w in jtitle for w in _MID_TITLE_WORDS):
            title_score = min(100, title_score + 5)
            title_reason += " [+5 mid/entry-level bonus]"

        # ── must_have_skills (30%) ────────────────────────────────────────────
        matched_tools  = [t for t in tools  if t in jd]
        matched_skills = [s for s in skills if s in jd]
        coverage = (len(matched_tools) + len(matched_skills)) / max(len(tools) + len(skills), 1)
        must_score = min(95, 40 + int(coverage * 70))
        must_reason = (
            f"{len(matched_tools)} tools + {len(matched_skills)} skills matched "
            f"({coverage:.0%} coverage)"
        )

        # ── nice_to_have_skills (15%) ─────────────────────────────────────────
        matched_kw = [k for k in keywords if k in jd]
        kw_coverage = len(matched_kw) / max(len(keywords), 1)
        nice_score = min(90, 40 + int(kw_coverage * 60))
        nice_reason = f"{len(matched_kw)} keyword matches"

        # ── industry_fit (15%) ────────────────────────────────────────────────
        domain_hits = sum(1 for s in _DOMAIN_SIGNALS if s in jd)
        industry_score = min(95, 40 + domain_hits * 6)
        industry_reason = f"{domain_hits} domain-signal matches"

        # ── location_match (10%) ─────────────────────────────────────────────
        if is_remote:
            loc_score = 100
            loc_reason = "remote position"
        elif "new york" in loc.lower() or "ny" in loc.lower():
            loc_score = 95
            loc_reason = "New York — ideal"
        elif loc.strip() == "" or "united states" in loc.lower():
            loc_score = 75
            loc_reason = "US-based, no remote info"
        else:
            loc_score = 55
            loc_reason = f"location: {job_info.get('location','?')}"

        # ── salary_fit (5%) ──────────────────────────────────────────────────
        salary_re = re.search(r'\$(\d{2,3})[kK]', jd)
        if salary_re:
            k = int(salary_re.group(1))
            if k >= 100:
                sal_score = 90
                sal_reason = f"${k}K — above expectation"
            elif k >= 80:
                sal_score = 75
                sal_reason = f"${k}K — meets expectation"
            else:
                sal_score = 40
                sal_reason = f"${k}K — below expectation"
        else:
            sal_score = 65
            sal_reason = "salary not specified"

        return {
            "title_match":         {"score": title_score,    "reason": title_reason},
            "must_have_skills":    {"score": must_score,     "reason": must_reason},
            "nice_to_have_skills": {"score": nice_score,     "reason": nice_reason},
            "industry_fit":        {"score": industry_score, "reason": industry_reason},
            "location_match":      {"score": loc_score,      "reason": loc_reason},
            "salary_fit":          {"score": sal_score,      "reason": sal_reason},
        }

    return scorer
