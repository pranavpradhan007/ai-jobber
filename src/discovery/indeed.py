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

# ── US-wide search matrix ────────────────────────────────────────────────────
# Covers remote + 6 major US tech hubs.
# LinkedIn note: Indeed aggregates jobs cross-posted from LinkedIn, Greenhouse,
# Workday etc. — no separate LinkedIn MCP is available. For LinkedIn-exclusive
# postings use `job-agent add-job --url <linkedin_url> ...` manually.

_ROLE_SEARCHES = [
    "Machine Learning Engineer",
    "AI Research Engineer",
    "Applied Scientist Machine Learning",
    "Reinforcement Learning Engineer",
    "LLM Engineer Python PyTorch",
    "Research Engineer deep learning",
    "Data Scientist machine learning",
    "MLOps ML Platform Engineer",
    "AI Engineer NLP computer vision",
    "Scientific ML Engineer JAX",
    "Game AI Engineer reinforcement learning",
    "Robotics ML Engineer simulation",
]

# ── Complete US coverage ─────────────────────────────────────────────────────
# One or more cities per state so every US market is reached.
# Large tech states get multiple entries (CA, TX, NY, FL, WA, MA, etc.).
# "remote" always runs first — catches nationwide remote-ok postings.
_US_LOCATIONS = [
    # ── Always ───────────────────────────────────────────────────────────────
    "remote",

    # ── Alabama ──────────────────────────────────────────────────────────────
    "Birmingham, AL",
    "Huntsville, AL",
    "Montgomery, AL",

    # ── Alaska ───────────────────────────────────────────────────────────────
    "Anchorage, AK",

    # ── Arizona ──────────────────────────────────────────────────────────────
    "Phoenix, AZ",
    "Scottsdale, AZ",
    "Tempe, AZ",
    "Tucson, AZ",
    "Mesa, AZ",
    "Chandler, AZ",

    # ── Arkansas ─────────────────────────────────────────────────────────────
    "Little Rock, AR",
    "Fayetteville, AR",

    # ── California ───────────────────────────────────────────────────────────
    "San Francisco, CA",
    "San Jose, CA",
    "Oakland, CA",
    "Palo Alto, CA",
    "Mountain View, CA",
    "Sunnyvale, CA",
    "Santa Clara, CA",
    "Menlo Park, CA",
    "Redwood City, CA",
    "Fremont, CA",
    "Los Angeles, CA",
    "Santa Monica, CA",
    "Culver City, CA",
    "Burbank, CA",
    "Long Beach, CA",
    "Irvine, CA",
    "San Diego, CA",
    "Sacramento, CA",
    "Fresno, CA",
    "Santa Barbara, CA",
    "Santa Cruz, CA",
    "Pasadena, CA",
    "Riverside, CA",

    # ── Colorado ─────────────────────────────────────────────────────────────
    "Denver, CO",
    "Boulder, CO",
    "Colorado Springs, CO",
    "Fort Collins, CO",
    "Aurora, CO",

    # ── Connecticut ──────────────────────────────────────────────────────────
    "Hartford, CT",
    "New Haven, CT",
    "Stamford, CT",
    "Bridgeport, CT",

    # ── Delaware ─────────────────────────────────────────────────────────────
    "Wilmington, DE",

    # ── Florida ──────────────────────────────────────────────────────────────
    "Miami, FL",
    "Fort Lauderdale, FL",
    "Boca Raton, FL",
    "West Palm Beach, FL",
    "Tampa, FL",
    "St. Petersburg, FL",
    "Orlando, FL",
    "Jacksonville, FL",
    "Tallahassee, FL",
    "Gainesville, FL",

    # ── Georgia ──────────────────────────────────────────────────────────────
    "Atlanta, GA",
    "Alpharetta, GA",
    "Savannah, GA",
    "Augusta, GA",

    # ── Hawaii ───────────────────────────────────────────────────────────────
    "Honolulu, HI",

    # ── Idaho ────────────────────────────────────────────────────────────────
    "Boise, ID",
    "Coeur d'Alene, ID",

    # ── Illinois ─────────────────────────────────────────────────────────────
    "Chicago, IL",
    "Evanston, IL",
    "Naperville, IL",
    "Schaumburg, IL",
    "Springfield, IL",

    # ── Indiana ──────────────────────────────────────────────────────────────
    "Indianapolis, IN",
    "Fort Wayne, IN",
    "Bloomington, IN",

    # ── Iowa ─────────────────────────────────────────────────────────────────
    "Des Moines, IA",
    "Iowa City, IA",
    "Cedar Rapids, IA",

    # ── Kansas ───────────────────────────────────────────────────────────────
    "Wichita, KS",
    "Overland Park, KS",
    "Kansas City, KS",

    # ── Kentucky ─────────────────────────────────────────────────────────────
    "Louisville, KY",
    "Lexington, KY",

    # ── Louisiana ────────────────────────────────────────────────────────────
    "New Orleans, LA",
    "Baton Rouge, LA",

    # ── Maine ────────────────────────────────────────────────────────────────
    "Portland, ME",

    # ── Maryland ─────────────────────────────────────────────────────────────
    "Baltimore, MD",
    "Bethesda, MD",
    "Rockville, MD",
    "Silver Spring, MD",
    "Annapolis, MD",

    # ── Massachusetts ────────────────────────────────────────────────────────
    "Boston, MA",
    "Cambridge, MA",
    "Somerville, MA",
    "Waltham, MA",
    "Burlington, MA",
    "Worcester, MA",
    "Springfield, MA",

    # ── Michigan ─────────────────────────────────────────────────────────────
    "Detroit, MI",
    "Ann Arbor, MI",
    "Grand Rapids, MI",
    "Lansing, MI",

    # ── Minnesota ────────────────────────────────────────────────────────────
    "Minneapolis, MN",
    "St. Paul, MN",
    "Rochester, MN",

    # ── Mississippi ──────────────────────────────────────────────────────────
    "Jackson, MS",

    # ── Missouri ─────────────────────────────────────────────────────────────
    "Kansas City, MO",
    "St. Louis, MO",
    "Columbia, MO",

    # ── Montana ──────────────────────────────────────────────────────────────
    "Billings, MT",
    "Missoula, MT",

    # ── Nebraska ─────────────────────────────────────────────────────────────
    "Omaha, NE",
    "Lincoln, NE",

    # ── Nevada ───────────────────────────────────────────────────────────────
    "Las Vegas, NV",
    "Reno, NV",
    "Henderson, NV",

    # ── New Hampshire ────────────────────────────────────────────────────────
    "Manchester, NH",
    "Nashua, NH",

    # ── New Jersey ───────────────────────────────────────────────────────────
    "Newark, NJ",
    "Jersey City, NJ",
    "Princeton, NJ",
    "Hoboken, NJ",
    "Trenton, NJ",

    # ── New Mexico ───────────────────────────────────────────────────────────
    "Albuquerque, NM",
    "Santa Fe, NM",

    # ── New York ─────────────────────────────────────────────────────────────
    "New York, NY",
    "Brooklyn, NY",
    "Buffalo, NY",
    "Rochester, NY",
    "Albany, NY",
    "Syracuse, NY",
    "Ithaca, NY",

    # ── North Carolina ───────────────────────────────────────────────────────
    "Charlotte, NC",
    "Raleigh, NC",
    "Durham, NC",
    "Chapel Hill, NC",
    "Greensboro, NC",
    "Winston-Salem, NC",

    # ── North Dakota ─────────────────────────────────────────────────────────
    "Fargo, ND",
    "Bismarck, ND",

    # ── Ohio ─────────────────────────────────────────────────────────────────
    "Columbus, OH",
    "Cleveland, OH",
    "Cincinnati, OH",
    "Dayton, OH",
    "Toledo, OH",

    # ── Oklahoma ─────────────────────────────────────────────────────────────
    "Oklahoma City, OK",
    "Tulsa, OK",

    # ── Oregon ───────────────────────────────────────────────────────────────
    "Portland, OR",
    "Eugene, OR",
    "Bend, OR",
    "Hillsboro, OR",

    # ── Pennsylvania ─────────────────────────────────────────────────────────
    "Philadelphia, PA",
    "Pittsburgh, PA",
    "Harrisburg, PA",
    "Allentown, PA",
    "State College, PA",

    # ── Rhode Island ─────────────────────────────────────────────────────────
    "Providence, RI",

    # ── South Carolina ───────────────────────────────────────────────────────
    "Columbia, SC",
    "Charleston, SC",
    "Greenville, SC",

    # ── South Dakota ─────────────────────────────────────────────────────────
    "Sioux Falls, SD",

    # ── Tennessee ────────────────────────────────────────────────────────────
    "Nashville, TN",
    "Memphis, TN",
    "Knoxville, TN",
    "Chattanooga, TN",

    # ── Texas ────────────────────────────────────────────────────────────────
    "Austin, TX",
    "Dallas, TX",
    "Fort Worth, TX",
    "Houston, TX",
    "San Antonio, TX",
    "El Paso, TX",
    "Plano, TX",
    "Irving, TX",
    "Round Rock, TX",
    "College Station, TX",

    # ── Utah ─────────────────────────────────────────────────────────────────
    "Salt Lake City, UT",
    "Provo, UT",
    "Ogden, UT",
    "St. George, UT",

    # ── Vermont ──────────────────────────────────────────────────────────────
    "Burlington, VT",

    # ── Virginia ─────────────────────────────────────────────────────────────
    "Arlington, VA",
    "Richmond, VA",
    "McLean, VA",
    "Reston, VA",
    "Charlottesville, VA",
    "Norfolk, VA",

    # ── Washington ───────────────────────────────────────────────────────────
    "Seattle, WA",
    "Bellevue, WA",
    "Redmond, WA",
    "Kirkland, WA",
    "Tacoma, WA",
    "Spokane, WA",

    # ── Washington DC ────────────────────────────────────────────────────────
    "Washington, DC",

    # ── West Virginia ────────────────────────────────────────────────────────
    "Charleston, WV",
    "Morgantown, WV",

    # ── Wisconsin ────────────────────────────────────────────────────────────
    "Milwaukee, WI",
    "Madison, WI",

    # ── Wyoming ──────────────────────────────────────────────────────────────
    "Cheyenne, WY",
]

# Full matrix: 12 roles × ~200 locations = ~2400 search combinations.
# rotate_searches() spreads these across cycles (60 per cycle).
# Full sweep completes every ~40 cycles ≈ 20 hours at 30-min intervals.
DEFAULT_SEARCHES: list[dict] = [
    {"search": role, "location": loc}
    for role in _ROLE_SEARCHES
    for loc in _US_LOCATIONS
]


def rotate_searches(
    searches: list[dict],
    cycle: int,
    batch_size: int = 48,
) -> list[dict]:
    """
    Return the slice of searches for this cycle number.

    With ~2400 total searches and batch_size=48, every search runs once
    across ~50 cycles (≈25 hours at 30-min intervals — roughly once per day).
    'remote' searches always run — they're prepended to every batch.

    Args:
        searches:   full DEFAULT_SEARCHES list.
        cycle:      current cycle number (0-based or 1-based, doesn't matter).
        batch_size: how many searches to run per cycle.
    """
    remote = [s for s in searches if s["location"] == "remote"]
    non_remote = [s for s in searches if s["location"] != "remote"]

    # Rotate through non-remote searches
    start = ((cycle - 1) * batch_size) % max(len(non_remote), 1)
    end   = start + batch_size
    if end <= len(non_remote):
        batch = non_remote[start:end]
    else:
        # Wrap around
        batch = non_remote[start:] + non_remote[: end - len(non_remote)]

    # Always include remote
    return remote + batch


def discover_searches_for_profile(
    custom_searches: Optional[list[dict]] = None,
    *,
    locations: Optional[list[str]] = None,
    roles: Optional[list[str]] = None,
) -> list[dict]:
    """
    Return the list of Indeed searches to run for this profile.

    Args:
        custom_searches: override the entire list.
        locations: restrict to specific locations (e.g. ["remote", "Seattle, WA"]).
        roles: restrict to specific role keywords.

    Claude Code iterates this and calls MCP search_jobs for each entry.
    """
    if custom_searches:
        return custom_searches
    searches = DEFAULT_SEARCHES
    if locations:
        loc_set = {l.lower() for l in locations}
        searches = [s for s in searches if s["location"].lower() in loc_set]
    if roles:
        role_set = {r.lower() for r in roles}
        searches = [s for s in searches
                    if any(r in s["search"].lower() for r in role_set)]
    return searches


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
