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
        f"Overnight: scored={stats.scored} skipped={stats.skipped} "
        f"tailored={stats.tailored} submitted={stats.submitted} "
        f"gated={stats.gated} failed={stats.failed}"
    )
    if stats.errors:
        click.echo(f"Errors ({len(stats.errors)}):")
        for e in stats.errors:
            click.echo(f"  {e}")


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
              help="Path to a file containing the reply text. "
                   "Reads from stdin if not specified.")
@click.pass_context
def check_approvals_cmd(ctx, reply_file):
    """Parse and apply approval reply commands from stdin or a file."""
    import sys
    from src.db.connection import get_connection as _gc
    from src.approvals.parser import parse_reply, apply_commands

    if reply_file:
        with open(reply_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    commands = parse_reply(text)
    if not commands:
        click.echo("No valid commands found in reply.")
        return

    conn = _gc(ctx.obj["db"])
    results = apply_commands(conn, commands)
    conn.close()

    for r in results:
        status = "OK" if r.success else f"ERROR: {r.error}"
        click.echo(f"  APP-{r.app_id or 'N/A'} {r.command}: {status}")
