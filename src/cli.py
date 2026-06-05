"""
Job-Agent V1 CLI entry point.
Commands: export-csv, run-overnight, prep-batch, check-approvals
"""
from __future__ import annotations
import csv
import logging
import os
import sys

import click

from src.db.connection import get_connection

logger = logging.getLogger(__name__)


@click.group()
@click.option(
    "--db",
    default=None,
    envvar="JOB_AGENT_DB",
    help="Path to the SQLite database file.",
)
@click.option(
    "--log-level",
    default="INFO",
    envvar="LOG_LEVEL",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
@click.pass_context
def main(ctx: click.Context, db: str, log_level: str) -> None:
    """Job-Agent V1 — local laptop automation."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    ctx.ensure_object(dict)
    ctx.obj["db"] = db or os.environ.get("JOB_AGENT_DB", "job_agent.db")


# ---------------------------------------------------------------------------
# export-csv
# ---------------------------------------------------------------------------

@main.command("export-csv")
@click.option(
    "--out-dir",
    default=".",
    help="Directory to write CSV files into.",
    show_default=True,
)
@click.option(
    "--all/--applied-only",
    "export_all",
    default=False,
    help="Export all applications (default: applied-only).",
)
@click.pass_context
def export_csv(ctx: click.Context, out_dir: str, export_all: bool) -> None:
    """Export applications to CSV from the v_data_csv / v_applied_csv views."""
    db_path = ctx.obj["db"]
    conn = get_connection(db_path)

    os.makedirs(out_dir, exist_ok=True)

    views = []
    if export_all:
        views.append(("v_data_csv", os.path.join(out_dir, "data.csv")))
    views.append(("v_applied_csv", os.path.join(out_dir, "applied.csv")))

    for view_name, out_path in views:
        _export_view(conn, view_name, out_path)
        click.echo(f"Exported {view_name} → {out_path}")

    conn.close()


def _export_view(conn, view_name: str, out_path: str) -> int:
    """Write a view's rows to a CSV file. Returns row count."""
    cur = conn.execute(f"SELECT * FROM {view_name}")  # noqa: S608 (view, not user input)
    rows = cur.fetchall()
    if not rows:
        # Write header-only CSV so the file always exists
        # Re-query to get column names even with 0 rows
        cur2 = conn.execute(
            "SELECT name FROM pragma_table_info(?) ORDER BY cid",
            (view_name,),
        )
        # pragma_table_info doesn't work on views; use the cursor description trick
        cur3 = conn.execute(f"SELECT * FROM {view_name} LIMIT 0")  # noqa: S608
        fieldnames = [d[0] for d in cur3.description]
    else:
        fieldnames = rows[0].keys()

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))

    logger.info("exported %d rows from %s to %s", len(rows), view_name, out_path)
    return len(rows)


# ---------------------------------------------------------------------------
# add-job
# ---------------------------------------------------------------------------

@main.command("add-job")
@click.option("--url", required=True, help="Job posting URL (used as unique key).")
@click.option("--company", required=True)
@click.option("--title", required=True)
@click.option("--platform",
              type=click.Choice(["workday","greenhouse","lever","ashby","email","api","custom"],
                                case_sensitive=False),
              default="custom", show_default=True)
@click.option("--location", default="", help="e.g. 'New York, NY' or 'Remote'")
@click.option("--remote", is_flag=True, default=False)
@click.option("--has-screener", is_flag=True, default=False,
              help="Job requires extra screener Q&A.")
@click.option("--login-required", is_flag=True, default=False)
@click.option("--email-apply", default="", help="Apply-by-email address (sets platform=email).")
@click.option("--jd-file", default=None, type=click.Path(exists=True),
              help="Path to a .txt file containing the raw job description.")
@click.option("--jd", default="", help="Job description text (inline, use quotes).")
@click.pass_context
def add_job_cmd(ctx, url, company, title, platform, location, remote,
                has_screener, login_required, email_apply, jd_file, jd):
    """Add a job posting to the database and create a DISCOVERED application."""
    from src.db.connection import get_connection as _gc
    from src.db.jobs import create_job, get_job_by_url
    from src.db.applications import create_application
    from src.parsing.jd_parser import parse_jd

    conn = _gc(ctx.obj["db"])

    # Dedup check
    existing = get_job_by_url(conn, url)
    if existing:
        click.echo(f"Job already exists: id={existing.id}  {existing.company} — {existing.title}")
        conn.close()
        return

    # Load JD text
    raw_jd = jd
    if jd_file:
        with open(jd_file, encoding="utf-8") as fh:
            raw_jd = fh.read()

    # Override platform for email-apply
    if email_apply:
        platform = "email"

    # Parse JD
    parsed = parse_jd(raw_jd) if raw_jd.strip() else None
    clean_jd = parsed.clean_jd if parsed else None
    jd_hash  = parsed.jd_hash  if parsed else None

    job = create_job(
        conn,
        source="manual",
        url=url,
        company=company,
        title=title,
        location=location or None,
        remote=1 if remote else 0,
        raw_jd=raw_jd or None,
        clean_jd=clean_jd,
        platform=platform,
        has_screener=1 if has_screener else 0,
        login_required=1 if login_required else 0,
        email_apply_addr=email_apply or None,
    )
    # Update hash if parsed
    if jd_hash:
        conn.execute("UPDATE jobs SET jd_hash=? WHERE id=?", (jd_hash, job.id))
        conn.commit()

    app = create_application(conn, job_id=job.id)
    conn.close()

    click.echo(f"Added job  id={job.id}   {company} — {title}")
    click.echo(f"Application id={app.id}  state=DISCOVERED  platform={platform}")
    if parsed:
        click.echo(f"JD parsed:  {len(raw_jd)} chars → hash={jd_hash[:16]}...")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@main.command("status")
@click.pass_context
def status_cmd(ctx):
    """Show a summary of all applications grouped by state."""
    from src.db.connection import get_connection as _gc
    conn = _gc(ctx.obj["db"])
    cur = conn.execute(
        """
        SELECT a.state, COUNT(*) as n
        FROM applications a
        GROUP BY a.state
        ORDER BY a.created_at
        """
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        click.echo("No applications yet.")
        return
    click.echo(f"{'State':<35} {'Count':>5}")
    click.echo("-" * 42)
    for row in rows:
        click.echo(f"{row['state']:<35} {row['n']:>5}")


# ---------------------------------------------------------------------------
# run-overnight
# ---------------------------------------------------------------------------

@main.command("run-overnight")
@click.option("--dry-run", is_flag=True, default=False,
              help="Score and tailor but do not actually submit.")
@click.option("--max-jobs", default=50, show_default=True,
              help="Maximum number of DISCOVERED apps to process.")
@click.option("--candidate-name", default=None, show_default=True,
              help="Override candidate name (defaults to config.CANDIDATE_NAME)")
@click.pass_context
def run_overnight_cmd(ctx, dry_run, max_jobs, candidate_name):
    """Run the overnight pipeline (discover → score → tailor → submit/gate)."""
    from src.db.connection import get_connection as _gc
    from src.runners.overnight import run_overnight
    from src.config import CANDIDATE_NAME
    conn = _gc(ctx.obj["db"])
    stats = run_overnight(
        conn,
        candidate_name=candidate_name or CANDIDATE_NAME,
        max_jobs=max_jobs,
        dry_run=dry_run,
    )
    conn.close()
    click.echo(
        f"Pipeline: scored={stats.scored} skipped={stats.skipped} "
        f"tailored={stats.tailored} submitted={stats.submitted} "
        f"gated={stats.gated} failed={stats.failed}"
    )
    if stats.errors:
        click.echo(f"Errors ({len(stats.errors)}):")
        for e in stats.errors:
            click.echo(f"  {e}")


# ---------------------------------------------------------------------------
# run-loop  (continuous daytime + overnight runner)
# ---------------------------------------------------------------------------

@main.command("run-loop")
@click.option("--interval", default=30, show_default=True,
              help="Minutes between pipeline cycles.")
@click.option("--once", is_flag=True, default=False,
              help="Run a single cycle and exit (same as run-overnight but with discovery + approvals).")
@click.option("--skip-discover", is_flag=True, default=False,
              help="Skip queuing Indeed searches this cycle.")
@click.option("--skip-approvals", is_flag=True, default=False,
              help="Skip checking Gmail for reply commands.")
@click.option("--max-jobs", default=20, show_default=True,
              help="Max DISCOVERED apps to process per cycle.")
@click.option("--to", "digest_recipient", default=None,
              help="Digest email recipient (defaults to DIGEST_RECIPIENT env var).")
@click.option("--candidate-name", default=None)
@click.pass_context
def run_loop_cmd(ctx, interval, once, skip_discover, skip_approvals,
                 max_jobs, digest_recipient, candidate_name):
    """Run the pipeline continuously — discovers, scores, tailors, approves, submits.

    Designed to run all day (not just overnight). Each cycle:
      1. Queue Indeed searches across the full US (Claude Code executes via MCP)
      2. Score + tailor + verify all DISCOVERED applications
      3. Submit auto_safe jobs immediately
      4. Gate jobs requiring approval → queue digest email
      5. Check Gmail for your phone replies → process APPROVE/EDIT/REJECT/SNOOZE
      6. Sleep interval minutes, then repeat

    Press Ctrl-C to stop cleanly after the current cycle finishes.

    Examples:
      job-agent run-loop                    # run every 30 min, all day
      job-agent run-loop --interval 60      # every hour
      job-agent run-loop --once             # single full cycle
      job-agent run-loop --skip-discover    # skip Indeed this cycle
    """
    import logging
    from src.db.connection import get_connection as _gc
    from src.runners.continuous import run_continuous
    from src.config import CANDIDATE_NAME

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    conn = _gc(ctx.obj["db"])
    try:
        run_continuous(
            conn,
            interval_minutes=interval,
            once=once,
            skip_discover=skip_discover,
            skip_approvals=skip_approvals,
            candidate_name=candidate_name or CANDIDATE_NAME,
            digest_recipient=digest_recipient,
            max_jobs_per_cycle=max_jobs,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# prep-batch
# ---------------------------------------------------------------------------

@main.command("prep-batch")
@click.option("--to", "recipient", required=True,
              help="Recipient email address for the digest.")
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_context
def prep_batch_cmd(ctx, recipient, dry_run):
    """Build and send the morning approval digest email."""
    from src.db.connection import get_connection as _gc
    from src.gmail.digest import build_digest
    conn = _gc(ctx.obj["db"])
    msg_id = build_digest(conn, recipient, dry_run=dry_run)
    conn.close()
    if msg_id:
        click.echo(f"Digest sent (message_id={msg_id})")
    else:
        click.echo("No pending applications — digest not sent.")


# ---------------------------------------------------------------------------
# check-approvals
# ---------------------------------------------------------------------------

@main.command("check-approvals")
@click.option("--reply-file", default=None,
              help="Path to a file containing the reply text.")
@click.option("--from-gmail", is_flag=True, default=False,
              help="Read replies from Gmail via MCP (requires Claude Code).")
@click.option("--digest-id", default=None,
              help="Only process replies for this specific DIGEST-* ID.")
@click.pass_context
def check_approvals_cmd(ctx, reply_file, from_gmail, digest_id):
    """Parse and apply approval reply commands.

    Sources (in priority order):
      --from-gmail   Search Gmail for replies to digest threads (MCP-bridged)
      --reply-file   Read from a local file
      stdin          Pipe reply text directly
    """
    import sys
    from src.db.connection import get_connection as _gc
    from src.approvals.parser import parse_reply, apply_commands

    conn = _gc(ctx.obj["db"])

    if from_gmail:
        from src.gmail.reply_watcher import watch_for_replies, mark_digest_processed
        from src.gmail.client import MCPGmailClient

        client = MCPGmailClient()
        cached = watch_for_replies(conn, gmail_client=client, digest_id=digest_id)

        if not cached:
            pending_dir = client._dir / "pending"
            pending = list(pending_dir.glob("search_digest_*.json"))
            click.echo(
                f"No cached replies yet. Queued {len(pending)} Gmail search request(s).\n"
                "Claude Code will execute the search via MCP — then re-run this command."
            )
            conn.close()
            return

        all_results = []
        for did, reply_text in cached.items():
            click.echo(f"\nProcessing replies for {did}:")
            commands = parse_reply(reply_text)
            if not commands:
                click.echo("  (no commands found in reply)")
                continue
            results = apply_commands(conn, commands)
            for r in results:
                status = "OK" if r.success else f"ERROR: {r.error}"
                detail = f" — {r.detail}" if r.detail else ""
                click.echo(f"  APP-{r.app_id or 'N/A'} {r.command}: {status}{detail}")
            all_results.extend(results)
            mark_digest_processed(conn, did)

        conn.close()
        return

    # File or stdin path
    if reply_file:
        with open(reply_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        click.echo("Paste reply text (Ctrl-D when done):")
        text = sys.stdin.read()

    commands = parse_reply(text)
    if not commands:
        click.echo("No valid commands found in reply.")
        conn.close()
        return

    results = apply_commands(conn, commands)
    conn.close()

    for r in results:
        status = "OK" if r.success else f"ERROR: {r.error}"
        detail = f" — {r.detail}" if r.detail else ""
        click.echo(f"  APP-{r.app_id or 'N/A'} {r.command}: {status}{detail}")


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

@main.command("discover")
@click.option("--role", default=None,
              help="Specific job title / keywords (adds to defaults, doesn't replace).")
@click.option("--location", default=None,
              help="Restrict to this location. Default: all US (7 cities + remote).")
@click.option("--remote-only", is_flag=True, default=False,
              help="Search remote positions only.")
@click.option("--limit", default=10, show_default=True,
              help="Max results per search term.")
@click.pass_context
def discover_cmd(ctx, role, location, remote_only, limit):
    """Discover new jobs from Indeed across the whole US via Claude Code MCP.

    Searches 12 roles x 7 locations (remote + NYC, SF, Seattle, Boston,
    Austin, Chicago) by default — 84 searches total.

    Note on LinkedIn: The available MCP is Indeed (not LinkedIn). Indeed
    aggregates most jobs cross-posted from LinkedIn, Greenhouse, Workday etc.
    For LinkedIn-exclusive postings use: job-agent add-job --url <url> ...

    To run a full discovery cycle:
      1. job-agent discover          ← queue searches (this command)
      2. Claude Code executes them   ← happens automatically in an active session
      3. job-agent run-loop --once   ← process newly DISCOVERED jobs
    """
    from src.discovery.indeed import discover_searches_for_profile
    from src.db.connection import get_connection as _gc
    import json
    from pathlib import Path
    from datetime import datetime, timezone

    # Build search list
    locs = [location] if location else (["remote"] if remote_only else None)
    searches = discover_searches_for_profile(locations=locs)
    if role:
        extra_locs = ["remote", "New York, NY", "San Francisco, CA",
                      "Seattle, WA", "Boston, MA", "Austin, TX", "Chicago, IL"]
        if location:
            extra_locs = [location]
        if remote_only:
            extra_locs = ["remote"]
        for loc in extra_locs:
            searches.append({"search": role, "location": loc})

    manifest = {
        "action":     "discover_jobs",
        "searches":   searches,
        "limit":      limit,
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

    actions_dir = Path("gmail_actions") / "pending"
    actions_dir.mkdir(parents=True, exist_ok=True)
    req_path = actions_dir / "discover_jobs_request.json"
    req_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    from src.discovery.indeed import DEFAULT_SEARCHES, rotate_searches
    total = len(DEFAULT_SEARCHES)
    locations_used = sorted({s["location"] for s in searches})
    click.echo(
        f"Queued {len(searches)} searches this cycle "
        f"({total} total in pool across {len({s['location'] for s in DEFAULT_SEARCHES})} US locations)."
    )
    click.echo()
    for loc in locations_used:
        n = sum(1 for s in searches if s["location"] == loc)
        click.echo(f"  {loc:<28} {n:>2} role searches")
    click.echo()
    click.echo("Full US rotation: every search runs once every ~10 cycles (~5 hrs at 30-min interval).")
    click.echo("Claude Code executes via MCP Indeed — then: job-agent run-loop --once")
