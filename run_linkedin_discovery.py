"""
LinkedIn job discovery runner.

Usage:
  1. Make sure JobAgent Chrome is running (launch_chrome_debug.bat)
     and you are logged into linkedin.com in that Chrome window.
  2. Run: python run_linkedin_discovery.py

Discovered jobs are imported into job_agent.db as DISCOVERED applications.
The overnight runner then scores and gates them like any other job.
"""
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("JOB_AGENT_DB", "job_agent.db")

import pathlib
import src.storage.folders as fm
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())
pathlib.Path("applications").mkdir(exist_ok=True)

from src.db.connection import get_connection
from src.discovery.linkedin_browser import discover_jobs, LINKEDIN_ROLES, LINKEDIN_LOCATIONS

# ── Optional overrides ────────────────────────────────────────────────────────
ROLES     = LINKEDIN_ROLES       # edit to narrow the search
LOCATIONS = LINKEDIN_LOCATIONS   # edit to narrow locations
MAX_PER_SEARCH = 20

conn = get_connection("job_agent.db")

print("LinkedIn discovery starting...")
print(f"  Roles:     {len(ROLES)}")
print(f"  Locations: {len(LOCATIONS)}")
print(f"  Max/search: {MAX_PER_SEARCH}")
print()
print("  Connecting to JobAgent Chrome (CDP port 9222)...")
print("  Make sure launch_chrome_debug.bat is running and you are")
print("  logged into linkedin.com in that window.")
print()

job_dicts = discover_jobs(
    role_queries=ROLES,
    locations=LOCATIONS,
    max_per_search=MAX_PER_SEARCH,
    headless=False,   # headed via CDP so saved LinkedIn login is used
)

if not job_dicts:
    print("No jobs discovered. Check:")
    print("  - Is launch_chrome_debug.bat running?")
    print("  - Are you logged into linkedin.com in the JobAgent Chrome?")
    sys.exit(0)

print(f"Discovered {len(job_dicts)} jobs from LinkedIn")

# Import into DB
from src.discovery.indeed import import_jobs
result = import_jobs(conn, job_dicts, source="linkedin_browser")
conn.commit()

added  = result.get("added",       0) if isinstance(result, dict) else result
skipped = result.get("skipped_dup", 0) if isinstance(result, dict) else 0
print(f"Imported {added} new jobs ({skipped} duplicates skipped)")
print()
print("Run `python run_live_overnight.py` to score + gate them.")

conn.close()
