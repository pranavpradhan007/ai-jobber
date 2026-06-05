"""
resume_diff.md generator.
Documents which source_bank items were included, what was rephrased,
and the verifier result summary.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone

from src.verifier.retrieval import BankItem
from src.verifier.diff_verifier import VerifierReport

logger = logging.getLogger(__name__)


def write_resume_diff(
    folder_path: str,
    original_bullets: list[BankItem],
    rephrased_bullets: list[str],
    verifier_report: VerifierReport,
    job_title: str = "",
) -> str:
    """
    Write resume_diff.md to the application folder.
    Returns the path.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Resume Diff",
        f"",
        f"**Generated:** {now}",
        f"**Job title:** {job_title or '(unknown)'}",
        f"**Verifier passed:** {'YES' if verifier_report.passed else 'NO'}",
        f"",
        f"## Source bank items included",
        f"",
    ]

    for item in original_bullets:
        lines.append(
            f"- [{item.item_type}] `{item.content}`"
            f" (usage_level={item.usage_level}, source={item.source_ref or 'n/a'})"
        )

    lines += [
        f"",
        f"## Rephrased bullets",
        f"",
    ]
    for b in rephrased_bullets:
        lines.append(f"- {b}")

    lines += [
        f"",
        f"## Verifier results",
        f"",
        f"Total sentences: {verifier_report.total_sentences}",
        f"Total claims checked: {len(verifier_report.claims)}",
        f"Blocked claims: {len(verifier_report.blocked_claims)}",
        f"",
    ]

    if verifier_report.blocked_claims:
        lines.append("### Blocked claims")
        for c in verifier_report.blocked_claims:
            lines.append(
                f"- `{c.claim_text}` (type={c.claim_type}, "
                f"needs_approval={c.needs_approval})"
            )
    else:
        lines.append("All claims verified. ✓")

    content = "\n".join(lines) + "\n"
    path = os.path.join(folder_path, "resume_diff.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)

    logger.info("wrote resume_diff.md to %s", path)
    return path
