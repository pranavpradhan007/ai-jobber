"""
External job feed discovery.

Sources:
  - Hacker News "Who's Hiring" (monthly thread — high quality ML/AI/eng roles)
  - RemoteOK API (JSON, filterable by tag)
  - We Work Remotely RSS (XML, tech-focused remote roles)

Note: StackOverflow Jobs shut down March 2022.
      GitHub Jobs shut down May 2021.
      This module covers the same audience via the above sources.

All fetches are done via Claude Code's WebFetch — queued as action manifests.
Nothing in here calls the network directly.
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ACTIONS_DIR = Path("gmail_actions")

# ── Source definitions ────────────────────────────────────────────────────────

# RemoteOK public JSON API — no key needed
REMOTEOK_URL = "https://remoteok.io/api?tag=machine-learning"
REMOTEOK_TAGS = [
    "machine-learning",
    "ai",
    "python",
    "deep-learning",
    "nlp",
    "data-science",
]

# We Work Remotely RSS — curated remote tech jobs
WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-data-science-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
]

# HN Who's Hiring — monthly thread.
# Claude Code searches HN for the current month's thread ID.
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search?query=Ask+HN+Who+Is+Hiring&tags=story,ask_hn&numericFilters=created_at_i>1700000000"


# ── Manifest helpers ──────────────────────────────────────────────────────────

def queue_feed_fetches() -> list[str]:
    """
    Write pending action manifests for Claude Code to fetch all external feeds.
    Returns list of request_ids queued.
    """
    pending = _ACTIONS_DIR / "pending"
    pending.mkdir(parents=True, exist_ok=True)

    ids = []

    # RemoteOK (one request per tag batch)
    tags_str = ",".join(REMOTEOK_TAGS[:3])  # top 3 tags
    req_id = f"feed_remoteok_{_ts()}"
    _write_manifest(pending / f"{req_id}.json", {
        "action":     "fetch_feed",
        "source":     "remoteok",
        "request_id": req_id,
        "urls":       [f"https://remoteok.io/api?tag={t}" for t in REMOTEOK_TAGS],
        "instruction": (
            "Fetch each URL with WebFetch. Each returns a JSON array. "
            "The first element is metadata — skip it. "
            "For each job entry extract: position (title), company, location, "
            "url, description (tags joined). "
            "Deduplicate by url. "
            "Then call: from src.discovery.feeds import write_feed_results; "
            f"write_feed_results('{req_id}', 'remoteok', job_list)"
        ),
    })
    ids.append(req_id)

    # We Work Remotely RSS
    req_id = f"feed_wwr_{_ts()}"
    _write_manifest(pending / f"{req_id}.json", {
        "action":     "fetch_feed",
        "source":     "weworkremotely",
        "request_id": req_id,
        "urls":       WWR_FEEDS,
        "instruction": (
            "Fetch each RSS URL with WebFetch. Parse the XML: each <item> has "
            "<title>, <company>, <region>, <link>, <description>. "
            "Filter for items where title/description contains any of: "
            "machine learning, ML, AI, data science, python, NLP, deep learning, engineer. "
            "Build list of dicts: {title, company, location (region), url (link), snippet (first 200 chars of description)}. "
            "Then call: from src.discovery.feeds import write_feed_results; "
            f"write_feed_results('{req_id}', 'weworkremotely', job_list)"
        ),
    })
    ids.append(req_id)

    # Hacker News Who's Hiring
    req_id = f"feed_hn_{_ts()}"
    _write_manifest(pending / f"{req_id}.json", {
        "action":     "fetch_feed",
        "source":     "hackernews",
        "request_id": req_id,
        "urls":       [HN_SEARCH_URL],
        "instruction": (
            f"Fetch {HN_SEARCH_URL!r} with WebFetch to find the latest 'Ask HN: Who is hiring?' thread. "
            "Get the top result's objectID. "
            "Then fetch: https://hacker-news.firebaseio.com/v0/item/{objectID}.json "
            "to get the thread's kid comment IDs (first 100). "
            "For each kid ID fetch: https://hacker-news.firebaseio.com/v0/item/{kid}.json "
            "Filter for comments containing: machine learning, ML engineer, AI engineer, "
            "data scientist, NLP, deep learning, Python, PyTorch, JAX, LLM, remote. "
            "For each match extract: company (first word before |), title, location, "
            "url (first http URL in text), snippet (first 300 chars). "
            "Then call: from src.discovery.feeds import write_feed_results; "
            f"write_feed_results('{req_id}', 'hackernews', job_list)"
        ),
    })
    ids.append(req_id)

    logger.info("Queued %d external feed fetches", len(ids))
    return ids


def write_feed_results(request_id: str, source: str, job_list: list[dict]) -> None:
    """Called by Claude Code after fetching and parsing a feed."""
    results = _ACTIONS_DIR / "results"
    results.mkdir(parents=True, exist_ok=True)
    path = results / f"{request_id}.json"
    path.write_text(
        json.dumps({
            "request_id": request_id,
            "source":     source,
            "jobs":       job_list,
            "written_at": datetime.now(timezone.utc).isoformat() + "Z",
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote %d %s jobs for %s", len(job_list), source, request_id)


def import_cached_feeds(conn: sqlite3.Connection) -> dict:
    """
    Read all cached external feed results and import them.
    Returns aggregate summary.
    """
    from src.discovery.indeed import import_jobs

    results_dir = _ACTIONS_DIR / "results"
    if not results_dir.exists():
        return {"added": 0, "sources": {}}

    by_source: dict[str, list[dict]] = {}
    for path in results_dir.glob("feed_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            src  = data.get("source", "unknown")
            jobs = data.get("jobs", [])
            by_source.setdefault(src, []).extend(jobs)
            path.unlink()
        except Exception as exc:
            logger.warning("Bad feed result %s: %s", path, exc)

    totals: dict[str, int] = {}
    total_added = 0
    for src, jobs in by_source.items():
        normalised = _normalise(jobs, src)
        if normalised:
            summary = import_jobs(conn, normalised, source=src)
            n = summary.get("added", 0)
            totals[src] = n
            total_added += n

    return {"added": total_added, "sources": totals}


def _normalise(jobs: list[dict], source: str) -> list[dict]:
    out = []
    for j in jobs:
        url = (j.get("url") or j.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        out.append({
            "url":         url,
            "company":     j.get("company", ""),
            "title":       j.get("title") or j.get("position", ""),
            "location":    j.get("location") or j.get("region", "remote"),
            "description": j.get("description") or j.get("snippet") or j.get("text", ""),
            "platform":    source,
        })
    return out


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _write_manifest(path: Path, data: dict) -> None:
    data.setdefault("created_at", datetime.now(timezone.utc).isoformat() + "Z")
    data.setdefault("status", "pending")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
