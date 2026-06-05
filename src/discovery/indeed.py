"""
Indeed job discovery bridge.

Claude Code calls the MCP Indeed tool (search_jobs + get_job_details),
then passes the results here for deduplication and DB import.

Typical orchestration flow:
  1. Claude Code calls: searches = discover_searches_for_profile()
  2. Claude Code calls MCP search_jobs for each search term
  3. Claude Code calls MCP get_job_details for each result
  4. Claude Code calls: import_jobs(conn, job_dicts)
  5. Python returns count of new DISCOVERED applications

Nothing in this file calls MCP directly — that's Claude Code's job.
"""
from __future__ import annotations
import hashlib
import logging
import re
import sqlite3
from typing import Optional

from src.db.jobs import create_job, get_job_by_url
from src.db.applications import create_application
from src.parsing.jd_parser import parse_jd

logger = logging.getLogger(__name__)

# Default searches tuned to Pranav's profile
DEFAULT_SEARCHES = [
    {"search": "Machine Learning Engineer",          "location": "New York, NY"},
    {"search": "AI Research Engineer",               "location": "New York, NY"},
    {"search": "Applied Scientist ML",               "location": "New York, NY"},
    {"search": "Reinforcement Learning Engineer",    "location": "remote"},
    {"search": "LLM Engineer Python PyTorch",        "location": "remote"},
    {"search": "Research Engineer deep learning",    "location": "New York, NY"},
    {"search": "Data Scientist machine learning",    "location": "New York, NY"},
    {"search": "MLOps ML Platform Engineer",         "location": "remote"},
]


def discover_searches_for_profile(
    custom_searches: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Return the list of Indeed searches to run for this profile.
    Claude Code iterates this and calls MCP search_jobs for each entry.
    """
    return custom_searches or DEFAULT_SEARCHES


def import_jobs(
    conn: sqlite3.Connection,
    job_dicts: list[dict],
    *,
    source: str = "indeed",
) -> dict:
    """
    Import a batch of job dicts (from MCP get_job_details results) into the DB.
    Deduplicates by URL. Returns a summary dict.

    Expected fields per job_dict (all optional except url):
        url, company, title, location, remote, raw_jd, platform,
        has_screener, salary_min, salary_max, job_type
    """
    added = skipped_dup = skipped_no_jd = 0
    new_app_ids = []

    for raw in job_dicts:
        url = (raw.get("url") or raw.get("apply_url") or "").strip()
        if not url:
            logger.warning("Skipping job with no URL: %s", raw.get("title"))
            skipped_no_jd += 1
            continue

        # Dedup check
        existing = get_job_by_url(conn, url)
        if existing:
            logger.debug("Duplicate job (already in DB): %s", url)
            skipped_dup += 1
            continue

        raw_jd = raw.get("description") or raw.get("raw_jd") or ""
        if not raw_jd.strip():
            logger.info("Skipping job with no JD: %s — %s", raw.get("company"), raw.get("title"))
            skipped_no_jd += 1
            continue

        # Parse / hash the JD
        try:
            parsed = parse_jd(raw_jd)
            clean_jd = parsed.clean_jd
            jd_hash  = parsed.jd_hash
        except Exception as exc:
            logger.warning("JD parse failed for %s: %s", url, exc)
            clean_jd = raw_jd[:4000]
            jd_hash  = hashlib.sha256(raw_jd.encode()).hexdigest()

        # Infer platform from URL
        platform = _infer_platform(url, raw.get("platform"))

        # Infer remote
        loc = (raw.get("location") or "").lower()
        is_remote = 1 if ("remote" in loc or raw.get("remote")) else 0

        try:
            job = create_job(
                conn,
                source=source,
                url=url,
                company=raw.get("company") or "",
                title=raw.get("title") or "",
                location=raw.get("location") or "",
                remote=is_remote,
                raw_jd=raw_jd,
                clean_jd=clean_jd,
                platform=platform,
                has_screener=1 if raw.get("has_screener") else 0,
                login_required=0,
                email_apply_addr=raw.get("email_apply_addr") or None,
            )
            if jd_hash:
                conn.execute("UPDATE jobs SET jd_hash=? WHERE id=?", (jd_hash, job.id))
                conn.commit()

            app = create_application(conn, job_id=job.id)
            new_app_ids.append(app.id)
            added += 1
            logger.info(
                "Imported: [%d] %s — %s (%s)",
                job.id, job.company, job.title, platform,
            )
        except Exception as exc:
            logger.error("Failed to import job %s: %s", url, exc)
            skipped_no_jd += 1

    summary = {
        "added": added,
        "skipped_duplicate": skipped_dup,
        "skipped_no_jd": skipped_no_jd,
        "new_app_ids": new_app_ids,
    }
    logger.info("Import complete: %s", summary)
    return summary


def _infer_platform(url: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit.lower()
    url_lower = url.lower()
    for name in ("workday", "greenhouse", "lever", "ashby"):
        if name in url_lower:
            return name
    if "linkedin.com" in url_lower:
        return "linkedin"
    if "indeed.com" in url_lower:
        return "indeed"
    return "custom"
