"""
Continuous pipeline runner.

Two interleaved loops:
  FAST  (every pipeline_interval, default 30 min)
    → score + tailor + verify + package all DISCOVERED apps
    → submit auto_safe jobs
    → check Gmail replies → process APPROVE/EDIT/REJECT/SNOOZE
    → send digest if new gated apps arrived

  SLOW  (every discover_interval, default 4 h)
    → queue Indeed searches across all US (60/cycle rotating through 2328)
    → queue LinkedIn job alert email parse
    → queue HN Who's Hiring + RemoteOK + We Work Remotely feeds
    → import any cached results from the last discovery cycle

Press Ctrl-C to stop after the current cycle finishes.

Usage:
  job-agent run-loop                          # 30 min pipeline, 4 h discovery
  job-agent run-loop --pipeline-interval 10   # faster pipeline
  job-agent run-loop --discover-interval 2    # discover every 2 h
  job-agent run-loop --once                   # single full cycle
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


def _handle_signal(sig, _frame):
    global _STOP
    logger.info("Signal %s — stopping after this cycle", sig)
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
    approvals: int = 0
    errors: list[str] = field(default_factory=list)


def run_continuous(
    conn: sqlite3.Connection,
    *,
    pipeline_interval: int = 30,    # minutes between pipeline runs
    discover_interval: int = 240,   # minutes between discovery sweeps (4 h)
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
    global _STOP
    _STOP = False

    if gmail_client is None:
        from src.gmail.client import MCPGmailClient
        gmail_client = MCPGmailClient()

    recipient = digest_recipient or os.environ.get(
        "DIGEST_RECIPIENT", os.environ.get("YOUR_EMAIL_ADDRESS", "")
    )

    cycle = 0
    last_discovery_at: Optional[float] = None   # epoch seconds

    while not _STOP:
        cycle += 1
        stats = CycleStats(cycle=cycle)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        logger.info("─" * 56)
        logger.info("CYCLE %d  %s", cycle, now_str)
        logger.info("─" * 56)

        # ── Discovery (every discover_interval hours) ────────────────────────
        now_epoch = time.time()
        run_discovery = (
            not skip_discover
            and (
                last_discovery_at is None
                or (now_epoch - last_discovery_at) >= discover_interval * 60
            )
        )

        if run_discovery:
            stats.discovered += _run_discovery(conn, cycle, gmail_client)
            last_discovery_at = time.time()
        else:
            mins_since = int((now_epoch - (last_discovery_at or 0)) / 60)
            next_in    = max(0, discover_interval - mins_since)
            logger.info("DISCOVER: skipping (next sweep in ~%d min)", next_in)

        # ── Pipeline ─────────────────────────────────────────────────────────
        _run_pipeline(conn, stats, scorer_fn, rephraser_fn, extractor_fn,
                      gmail_client, candidate_name, max_jobs_per_cycle)

        # ── Approvals ────────────────────────────────────────────────────────
        if not skip_approvals:
            stats.approvals = _run_approvals(conn, gmail_client)

        # ── Digest ───────────────────────────────────────────────────────────
        if stats.gated > 0 and recipient:
            _send_digest(conn, recipient, gmail_client)

        # ── Summary ──────────────────────────────────────────────────────────
        logger.info(
            "DONE  discovered=%d scored=%d skipped=%d tailored=%d "
            "submitted=%d gated=%d approvals=%d errors=%d",
            stats.discovered, stats.scored, stats.skipped, stats.tailored,
            stats.submitted, stats.gated, stats.approvals, len(stats.errors),
        )
        for e in stats.errors:
            logger.warning("  %s", e)

        if once or _STOP:
            break

        logger.info("Sleeping %d min until next cycle…", pipeline_interval)
        _sleep(pipeline_interval * 60)

    logger.info("Runner stopped after %d cycle(s).", cycle)


# ── Private helpers ───────────────────────────────────────────────────────────

def _run_discovery(
    conn: sqlite3.Connection,
    cycle: int,
    gmail_client,
) -> int:
    """Queue all discovery sources and import any cached results."""
    from src.discovery.indeed import discover_searches_for_profile, rotate_searches, DEFAULT_SEARCHES
    from src.discovery.linkedin_email import queue_linkedin_email_fetch, import_from_cached_alerts
    from src.discovery.feeds import queue_feed_fetches, import_cached_feeds
    import json
    from pathlib import Path

    logger.info("DISCOVER: sweeping all sources…")
    imported = 0

    # 1. Import cached results from previous sweep
    li = import_from_cached_alerts(conn)
    if li.get("added"):
        logger.info("  LinkedIn alerts:  +%d jobs", li["added"])
        imported += li["added"]

    feeds = import_cached_feeds(conn)
    if feeds.get("added"):
        for src, n in feeds.get("sources", {}).items():
            logger.info("  %-18s +%d jobs", src + ":", n)
        imported += feeds["added"]

    # 2. Queue new discovery requests for this sweep
    # Indeed — rotating batch
    batch = rotate_searches(
        discover_searches_for_profile(), cycle=cycle, batch_size=48
    )
    manifest = {
        "action":      "discover_jobs",
        "searches":    batch,
        "cycle":       cycle,
        "total_pool":  len(DEFAULT_SEARCHES),
        "limit":       10,
        "created_at":  datetime.now(timezone.utc).isoformat() + "Z",
        "status":      "pending",
        "instruction": (
            "For each search call MCP search_jobs (country_code='US'). "
            "For each result call get_job_details. "
            "Then: from src.discovery.indeed import import_jobs; "
            "import_jobs(conn, job_dicts) keys: url, company, title, location, description."
        ),
    }
    pending = Path("gmail_actions") / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "discover_jobs_request.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    logger.info("  Indeed:           queued %d searches (cycle %d/%d)",
                len(batch), cycle, len(DEFAULT_SEARCHES) // 48 + 1)

    # LinkedIn email alerts
    queue_linkedin_email_fetch(gmail_client)
    logger.info("  LinkedIn alerts:  queued email fetch")

    # External feeds (HN, RemoteOK, WWR)
    feed_ids = queue_feed_fetches()
    logger.info("  External feeds:   queued %d fetches (HN + RemoteOK + WWR)", len(feed_ids))

    return imported


def _run_pipeline(
    conn, stats: CycleStats,
    scorer_fn, rephraser_fn, extractor_fn,
    gmail_client, candidate_name, max_jobs,
) -> None:
    from src.runners.overnight import run_overnight
    try:
        s = run_overnight(
            conn,
            scorer_fn=scorer_fn,
            rephraser_fn=rephraser_fn,
            extractor_fn=extractor_fn,
            gmail_client=gmail_client,
            candidate_name=candidate_name,
            max_jobs=max_jobs,
            dry_run=False,
        )
        stats.scored    = s.scored
        stats.skipped   = s.skipped
        stats.tailored  = s.tailored
        stats.submitted = s.submitted
        stats.gated     = s.gated
        stats.errors   += s.errors
    except Exception as exc:
        msg = f"pipeline: {exc}"
        logger.error(msg, exc_info=True)
        stats.errors.append(msg)


def _run_approvals(conn, gmail_client) -> int:
    from src.gmail.reply_watcher import watch_for_replies, mark_digest_processed
    from src.approvals.parser import parse_reply_natural, apply_commands
    import os
    try:
        cached = watch_for_replies(conn, gmail_client=gmail_client)
        total = 0
        for did, text in cached.items():
            cmds = parse_reply_natural(text, conn)
            if cmds:
                results = apply_commands(conn, cmds)
                n = sum(1 for r in results if r.success)
                total += n
                mark_digest_processed(conn, did)
                _send_approval_confirmation(conn, gmail_client, results, os.environ.get("YOUR_EMAIL_ADDRESS", ""))
        return total
    except Exception as exc:
        logger.warning("approvals: %s", exc)
        return 0


def _send_approval_confirmation(conn, gmail_client, results, recipient: str) -> None:
    """Email the user a plain-English summary of what was actioned from their reply."""
    if not recipient or not results:
        return
    try:
        import sqlite3 as _sq
        lines = []
        for r in results:
            if not r.success:
                lines.append(f"  Could not action APP-{r.app_id}: {r.error}")
                continue
            if r.app_id:
                row = conn.execute(
                    "SELECT j.company, j.title FROM applications a "
                    "JOIN jobs j ON a.job_id=j.id WHERE a.id=?", (r.app_id,)
                ).fetchone()
                company = row["company"] if row else f"APP-{r.app_id}"
                title   = row["title"]   if row else ""
                cmd_map = {
                    "APPROVE": f"Approved — will submit: {company} ({title})",
                    "REJECT":  f"Rejected: {company} ({title})",
                    "SKIP":    f"Skipped: {company} ({title})",
                    "EDIT":    f"Re-tailoring: {company} ({title}){' — ' + r.detail if r.detail else ''}",
                    "SNOOZE":  f"Snoozed: {company} ({title}){' — ' + r.detail if r.detail else ''}",
                }
                lines.append("  " + cmd_map.get(r.command, f"{r.command}: {company}"))
        if not lines:
            return
        body = "Got it — here is what I actioned from your reply:\n\n" + "\n".join(lines)
        body += "\n\nApproved jobs will be submitted on the next overnight run.\nReply again any time to change a decision.\n\n-- ai-jobber"
        gmail_client.send_email(
            to=recipient,
            subject="[ai-jobber] Confirmed: your decisions",
            body=body,
        )
        logger.info("Sent approval confirmation to %s (%d actions)", recipient, len(results))
    except Exception as exc:
        logger.warning("Could not send approval confirmation: %s", exc)


def _send_digest(conn, recipient, gmail_client) -> None:
    from src.gmail.digest import build_digest
    try:
        aid = build_digest(conn, recipient, gmail_client=gmail_client)
        if aid:
            logger.info("DIGEST: queued action_id=%s", aid)
    except Exception as exc:
        logger.warning("digest: %s", exc)


def _sleep(seconds: int) -> None:
    chunk, elapsed = 5, 0
    while elapsed < seconds and not _STOP:
        time.sleep(min(chunk, seconds - elapsed))
        elapsed += chunk
