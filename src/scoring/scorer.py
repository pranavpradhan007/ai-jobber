"""
Fit scorer.

Produces a weighted scorecard with an explainable reason string.
The scoring model is injected so tests use a deterministic mock.

Rubric (§12.3 of PHASE_0_REFINED_PLAN.md):
  title_match           25%
  must_have_skills      30%
  nice_to_have_skills   15%
  industry_fit          15%
  location_match        10%
  salary_fit             5%
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WEIGHTS: dict[str, float] = {
    "title_match":         0.25,
    "must_have_skills":    0.30,
    "nice_to_have_skills": 0.15,
    "industry_fit":        0.15,
    "location_match":      0.10,
    "salary_fit":          0.05,
}

SCORE_THRESHOLD = 60.0  # below this → should_apply = 0


@dataclass
class DimensionScore:
    name: str
    raw: float       # 0-100
    weight: float
    weighted: float  # raw * weight
    reason: str


@dataclass
class Scorecard:
    total_score: float
    dimensions: list[DimensionScore] = field(default_factory=list)
    summary_reason: str = ""
    passed_threshold: bool = False


# Type alias: ScorerFn(job_info: dict, profile_info: dict) -> dict[dimension -> {score, reason}]
ScorerFn = Callable[[dict, dict], dict]


def score_job(
    job_info: dict,
    profile_info: dict,
    *,
    scorer: Optional[ScorerFn] = None,
) -> Scorecard:
    """
    Score a job against a candidate profile.
    job_info: dict with keys title, location, remote, clean_jd, platform, has_screener
    profile_info: dict with keys title, skills, must_have, nice_to_have, location, remote, salary_range
    scorer: optional injectable scoring function (mocked in tests)
    """
    if scorer is None:
        scorer = _default_scorer

    raw_scores = scorer(job_info, profile_info)

    dimensions = []
    total = 0.0
    for dim, weight in WEIGHTS.items():
        entry = raw_scores.get(dim, {"score": 50, "reason": "no data"})
        raw = float(entry.get("score", 50))
        raw = max(0.0, min(100.0, raw))
        weighted = raw * weight
        total += weighted
        dimensions.append(DimensionScore(
            name=dim,
            raw=raw,
            weight=weight,
            weighted=weighted,
            reason=entry.get("reason", ""),
        ))

    passed = total >= SCORE_THRESHOLD
    summary = _build_summary(dimensions, total, passed)
    logger.info("scored job total=%.1f passed=%s", total, passed)
    return Scorecard(
        total_score=round(total, 2),
        dimensions=dimensions,
        summary_reason=summary,
        passed_threshold=passed,
    )


def save_scorecard(folder_path: str, scorecard: Scorecard) -> str:
    """Write scorecard.json to the application folder. Returns path."""
    path = os.path.join(folder_path, "scorecard.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "total_score": scorecard.total_score,
                "passed_threshold": scorecard.passed_threshold,
                "summary_reason": scorecard.summary_reason,
                "dimensions": [
                    {
                        "name": d.name,
                        "raw": d.raw,
                        "weight": d.weight,
                        "weighted": d.weighted,
                        "reason": d.reason,
                    }
                    for d in scorecard.dimensions
                ],
            },
            fh,
            indent=2,
        )
    return path


def _build_summary(
    dimensions: list[DimensionScore], total: float, passed: bool
) -> str:
    top = sorted(dimensions, key=lambda d: d.weighted, reverse=True)[:3]
    top_reasons = "; ".join(f"{d.name}({d.raw:.0f})" for d in top)
    verdict = "APPLY" if passed else "SKIP"
    return f"{verdict} score={total:.1f} — top factors: {top_reasons}"


def _default_scorer(job_info: dict, profile_info: dict) -> dict:
    """
    Runtime scorer: calls the Anthropic API.
    NOT used in tests — inject a mock scorer instead.
    """
    import anthropic  # noqa: PLC0415

    from src.config import DEFAULT_MODEL  # noqa: PLC0415
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""Score this job against the candidate profile on each dimension (0-100).
Return JSON: {{"title_match": {{"score": N, "reason": "..."}}, ...}}

Job: {json.dumps(job_info, indent=2)}
Profile: {json.dumps(profile_info, indent=2)}

Dimensions: title_match, must_have_skills, nice_to_have_skills, industry_fit, location_match, salary_fit"""

    msg = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    import re
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)
