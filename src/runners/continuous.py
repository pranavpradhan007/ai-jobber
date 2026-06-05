"""
Continuous pipeline runner.

Runs the full job-agent loop at a configurable interval throughout
the day (and night). Each cycle:

  1. DISCOVER   — queue Indeed searches → Claude Code executes MCP calls
  2. IMPORT     — import any pending job results from MCP results cache
  3. PIPELINE   — score → tailor → verify → package all DISCOVERED apps
  4. APPROVALS  — check Gmail for reply commands → process them
  5. SUBMIT     — run approved apps through submission
  6. DIGEST     — if new gated apps exist, queue a digest email
  7. SLEEP      — wait interval_minutes before next cycle

This is designed to be left running in a terminal or as a background
process. Claude Code should also be available to execute MCP actions
(Gmail drafts, Indeed searches) that Python queues.

Usage:
    job-agent run-loop                      # default: every 30 min
    job-agent run-loop --interval 60        # every hour
    job-agent run-loop --interval 10        # every 10 min (aggressive)
    job-agent run-loop --once               # single cycle, then exit
    job-agent run-loop --skip-discover      # skip Indeed search this cycle
"""
from __future__ import annotations
import logging
import os
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_STOP = False


def _handle_signal(sig, frame):
    global _STOP
    logger.info("Signal %s received — stopping after current cycle", sig)
    _STOP = True


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


@dataclass
class CycleStats:
    cycle: int = 0
    discovered: int = 0
    scored: int = 0
    skipped: int = 0
    tailored: int = 0
    submitted: int = 0
    gated: int = 0
    approvals_processed: int = 0
    errors: list[str] = field(default_factory=list)


def run_continuous(
    conn: sqlite3.Connection,
    *,
    interval_minutes: int = 30,
    once: bool = False,
    skip_discover: bool = False,
    skip_approvals: bool = False,
    candidate_name: str = "Pranav Tushar Pradhan",
    digest_recipient: Optional[str] = None,
    scorer_fn=None,
    rephraser_fn=None,
    extractor_fn=None,
    gmail_client=None,
    max_jobs_per_cycle: int = 20,
) -> None:
    """
    Run the pipeline in a continuous loop.

    Press Ctrl-C to stop cleanly after the current cycle completes.
    """
    global _STOP
    _STOP = False

    if gmail_client is None:
        from src.gmail.client import MCPGmailClient
        gmail_client = MCPGmailClient()

    recipient = digest_recipient or os.environ.get(
        "DIGEST_RECIPIENT",
        os.environ.get("YOUR_EMAIL_ADDRESS", ""),
    )

    cycle = 0
    while not _STOP:
        cycle += 1
        stats = CycleStats(cycle=cycle)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        logger.info("=" * 60)
        logger.info("CYCLE %d  started at %s", cycle, now)
        logger.info("=" * 60)

        # ── 1. Queue Indeed discovery searches ──────────────────────────────
        if not skip_discover:
            try:
                n = _queue_discovery(conn, cycle=cycle)
                logger.info("DISCOVER: queued %d searches (cycle %d) for Claude Code", n, cycle)
            except Exception as exc:
                msg = f"discover error: {exc}"
                logger.error(msg)
                stats.errors.append(msg)

        # ── 2. Run the core pipeline on DISCOVERED apps ──────────────────────
        try:
            from src.runners.overnight import run_overnight
            pipeline_stats = run_overnight(
                conn,
                scorer_fn=scorer_fn,
                rephraser_fn=rephraser_fn,
                extractor_fn=extractor_fn,
                gmail_client=gmail_client,
                candidate_name=candidate_name,
                max_jobs=max_jobs_per_cycle,
                dry_run=False,
            )
            stats.scored    = pipeline_stats.scored
            stats.skipped   = pipeline_stats.skipped
            stats.tailored  = pipeline_stats.tailored
            stats.submitted = pipeline_stats.submitted
            stats.gated     = pipeline_stats.gated
            stats.errors   += pipeline_stats.errors
            logger.info(
                "PIPELINE: scored=%d skipped=%d tailored=%d submitted=%d gated=%d",
                stats.scored, stats.skipped, stats.tailored,
                stats.submitted, stats.gated,
            )
        except Exception as exc:
            msg = f"pipeline error: {exc}"
            logger.error(msg, exc_info=True)
            stats.errors.append(msg)

        # ── 3. Check Gmail for approval replies ──────────────────────────────
        if not skip_approvals:
            try:
                n = _check_approvals(conn, gmail_client)
                stats.approvals_processed = n
                if n:
                    logger.info("APPROVALS: processed %d command(s)", n)
            except Exception as exc:
                msg = f"approvals error: {exc}"
                logger.error(msg)
                stats.errors.append(msg)

        # ── 4. Send digest for newly gated apps ──────────────────────────────
        if stats.gated > 0 and recipient:
            try:
                from src.gmail.digest import build_digest
                action_id = build_digest(conn, recipient, gmail_client=gmail_client)
                if action_id:
                    logger.info("DIGEST: queued draft action_id=%s", action_id)
            except Exception as exc:
                msg = f"digest error: {exc}"
                logger.error(msg)
                stats.errors.append(msg)

        # ── 5. Cycle summary ─────────────────────────────────────────────────
        _log_cycle_summary(stats)

        if once or _STOP:
            break

        logger.info("Sleeping %d minutes until next cycle…", interval_minutes)
        _interruptible_sleep(interval_minutes * 60)

    logger.info("Continuous runner stopped after %d cycle(s).", cycle)


def _queue_discovery(conn: sqlite3.Connection, cycle: int = 1) -> int:
    """
    Write a pending discovery manifest for Claude Code to execute.

    Uses rotate_searches() so the full US coverage (564 searches) is
    spread across cycles rather than run all at once.
    Each cycle gets 12 remote + 48 city searches = 60 searches.
    Full rotation completes every ~10 cycles (~5 hours at 30-min intervals).
    """
    import json
    from pathlib import Path
    from src.discovery.indeed import discover_searches_for_profile, rotate_searches, DEFAULT_SEARCHES

    all_searches = discover_searches_for_profile()
    batch = rotate_searches(all_searches, cycle=cycle, batch_size=48)

    actions_dir = Path("gmail_actions") / "pending"
    actions_dir.mkdir(parents=True, exist_ok=True)

    req_path = actions_dir / "discover_jobs_request.json"
    manifest = {
        "action":     "discover_jobs",
        "searches":   batch,
        "cycle":      cycle,
        "total_pool": len(DEFAULT_SEARCHES),
        "limit":      10,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z",
        "status":     "pending",
        "instruction": (
            "For each search call MCP search_jobs (country_code='US'). "
            "For each result call get_job_details to get the full description. "
            "Then call: from src.discovery.indeed import import_jobs; "
            "import_jobs(conn, job_dicts) with keys: url, company, title, "
            "location, description."
        ),
    }
    req_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return len(batch)


def _check_approvals(conn: sqlite3.Connection, gmail_client) -> int:
    """Read cached Gmail replies and apply approval commands."""
    from src.gmail.reply_watcher import watch_for_replies, mark_digest_processed
    from src.approvals.parser import parse_reply, apply_commands

    cached = watch_for_replies(conn, gmail_client=gmail_client)
    if not cached:
        return 0

    total = 0
    for digest_id, reply_text in cached.items():
        commands = parse_reply(reply_text)
        if not commands:
            continue
        results = apply_commands(conn, commands)
        n = sum(1 for r in results if r.success)
        total += n
        mark_digest_processed(conn, digest_id)
        logger.info("Processed %d/%d commands for %s", n, len(results), digest_id)

    return total


def _log_cycle_summary(stats: CycleStats) -> None:
    logger.info(
        "CYCLE %d DONE  scored=%d skipped=%d tailored=%d "
        "submitted=%d gated=%d approvals=%d errors=%d",
        stats.cycle, stats.scored, stats.skipped, stats.tailored,
        stats.submitted, stats.gated, stats.approvals_processed,
        len(stats.errors),
    )
    for err in stats.errors:
        logger.warning("  error: %s", err)


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in short chunks so Ctrl-C is responsive."""
    chunk = 5
    elapsed = 0
    while elapsed < seconds and not _STOP:
        time.sleep(min(chunk, seconds - elapsed))
        elapsed += chunk
