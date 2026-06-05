"""
LinkedIn job discovery via official job alert emails.

LinkedIn sends job alert emails to your Gmail. This module reads those
emails via Claude Code's MCP Gmail hook, parses the job listings, and
imports them into the DB as DISCOVERED applications.

No scraping. No browser automation. No LinkedIn API key.
Uses only LinkedIn's own official email alerts + Gmail MCP.

Setup (one-time, you do this manually):
  1. Go to linkedin.com/jobs
  2. Search for a role (e.g. "Machine Learning Engineer")
  3. Click "Set alert" → Email → Daily (or As they happen)
  4. Repeat for each role you want covered
  5. LinkedIn will email jobalerts@linkedin.com → your Gmail

The agent then reads these emails every cycle automatically.

Email subjects look like:
  "5 new Machine Learning Engineer jobs for you"
  "New jobs matching: AI Research Engineer"
  "Pranav, 3 new jobs match your search"
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Gmail search query for LinkedIn job alert emails
LINKEDIN_ALERT_QUERY = (
    'from:jobalerts@linkedin.com OR '
    'from:jobs-noreply@linkedin.com OR '
    'subject:"new jobs" from:linkedin.com'
)

_ACTIONS_DIR = Path("gmail_actions")


def queue_linkedin_email_fetch(gmail_client) -> str:
    """
    Write a pending action for Claude Code to:
      1. Search Gmail for LinkedIn job alert emails
      2. Extract job listings from each email
      3. Call write_linkedin_jobs() with the results

    Returns the request_id.
    """
    req_id = f"linkedin_alerts_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    manifest = {
        "action":      "fetch_linkedin_job_alerts",
        "request_id":  req_id,
        "query":       LINKEDIN_ALERT_QUERY,
        "created_at":  datetime.now(timezone.utc).isoformat() + "Z",
        "status":      "pending",
        "instruction": (
            f"Search Gmail with query: {LINKEDIN_ALERT_QUERY!r}. "
            "For each matching thread, call get_thread(FULL_CONTENT). "
            "Parse each email body to extract job listings. "
            "Each listing typically has: job title, company, location, "
            "and a LinkedIn job URL (jobs.linkedin.com/jobs/view/NNNNN). "
            "Build a list of dicts: [{title, company, location, url, snippet}]. "
            "Then call: from src.discovery.linkedin_email import write_linkedin_jobs; "
            f"write_linkedin_jobs('{req_id}', job_list). "
            "After processing each thread, label it as read so it isn't re-processed."
        ),
    }
    pending_dir = _ACTIONS_DIR / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = pending_dir / f"{req_id}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Queued LinkedIn alert fetch: %s", req_id)
    return req_id


def write_linkedin_jobs(request_id: str, job_list: list[dict]) -> None:
    """
    Called by Claude Code after parsing LinkedIn alert emails.
    Writes the job list so import_from_cached_alerts() can read it.
    """
    results_dir = _ACTIONS_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{request_id}.json"
    path.write_text(
        json.dumps({
            "request_id": request_id,
            "jobs":       job_list,
            "written_at": datetime.now(timezone.utc).isoformat() + "Z",
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote %d LinkedIn jobs for %s", len(job_list), request_id)


def import_from_cached_alerts(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> dict:
    """
    Read all cached LinkedIn job results and import them to the DB.
    Returns a summary dict. Called by the continuous runner.
    """
    from src.discovery.indeed import import_jobs

    results_dir = _ACTIONS_DIR / "results"
    if not results_dir.exists():
        return {"added": 0, "files": 0}

    all_jobs: list[dict] = []
    files_read = 0

    for path in results_dir.glob("linkedin_alerts_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            jobs = data.get("jobs", [])
            all_jobs.extend(jobs)
            files_read += 1
            path.unlink()  # consume and delete so we don't re-import
        except Exception as exc:
            logger.warning("Failed to read LinkedIn result %s: %s", path, exc)

    if not all_jobs:
        return {"added": 0, "files": files_read}

    # Normalise fields to match import_jobs() expectations
    normalised = []
    for j in all_jobs:
        url = j.get("url") or j.get("linkedin_url") or ""
        if not url:
            continue
        normalised.append({
            "url":         url,
            "company":     j.get("company", ""),
            "title":       j.get("title", ""),
            "location":    j.get("location", ""),
            "description": j.get("snippet") or j.get("description") or "",
            "platform":    "linkedin",
        })

    if dry_run:
        logger.info("dry_run: would import %d LinkedIn jobs", len(normalised))
        return {"added": len(normalised), "files": files_read, "dry_run": True}

    summary = import_jobs(conn, normalised, source="linkedin")
    logger.info("LinkedIn import: %s", summary)
    return {"files": files_read, **summary}


# ── Email body parsers ────────────────────────────────────────────────────────

def parse_linkedin_alert_email(email_body: str) -> list[dict]:
    """
    Parse a LinkedIn job alert email body and extract job listings.
    Handles both plain-text and HTML-stripped variants.
    Returns list of {title, company, location, url} dicts.
    """
    jobs = []

    # Extract LinkedIn job URLs (the definitive signal)
    url_pattern = re.compile(
        r'https?://(?:www\.)?linkedin\.com/jobs/view/(\d+)',
        re.IGNORECASE,
    )
    job_ids_seen = set()

    for m in url_pattern.finditer(email_body):
        job_id = m.group(1)
        if job_id in job_ids_seen:
            continue
        job_ids_seen.add(job_id)

        url = f"https://www.linkedin.com/jobs/view/{job_id}"
        # Try to extract context around this URL
        start = max(0, m.start() - 400)
        end   = min(len(email_body), m.end() + 200)
        context = email_body[start:end]

        title    = _extract_field(context, ["title", "position", "role"])
        company  = _extract_field(context, ["company", "employer", "at "])
        location = _extract_field(context, ["location", "city", "remote"])

        jobs.append({
            "url":      url,
            "title":    title or "Unknown",
            "company":  company or "Unknown",
            "location": location or "",
            "snippet":  context[:200].strip(),
        })

    return jobs


def _extract_field(text: str, hints: list[str]) -> Optional[str]:
    """Heuristic: find a short meaningful string near a hint word."""
    for hint in hints:
        idx = text.lower().find(hint)
        if idx == -1:
            continue
        # Grab the word cluster after the hint
        snippet = text[idx + len(hint): idx + len(hint) + 80].strip()
        # Strip HTML tags
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        # Take up to the first newline or pipe
        snippet = re.split(r"[\n\r|·•]", snippet)[0].strip()
        if 2 < len(snippet) < 80:
            return snippet
    return None
