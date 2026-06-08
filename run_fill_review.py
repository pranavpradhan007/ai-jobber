"""
Fill-and-review mode: discovers new LinkedIn jobs, scores them, fills the
best one's application form in Chrome, then STOPS before clicking Submit.

Usage:
  1.  Launch Chrome with debug port:  launch_chrome_debug.bat
  2.  Log in to LinkedIn in that Chrome window.
  3.  Run: python run_fill_review.py

Chrome stays open on the review/submit page. Submit manually when satisfied,
or press Ctrl+C / close Chrome to abandon.
"""
import logging
import os
import sys
import pathlib

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

os.environ["JOB_AGENT_DB"] = "job_agent.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("fill_review.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

import src.storage.folders as fm
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())
pathlib.Path("applications").mkdir(exist_ok=True)

from src.db.connection import get_connection
conn = get_connection("job_agent.db")

# ── Step 1: Discover fresh LinkedIn jobs ─────────────────────────────────────
print("\n" + "="*60)
print("  STEP 1: LinkedIn Discovery")
print("="*60)
try:
    from src.discovery.linkedin_browser import discover_jobs, LINKEDIN_ROLES, LINKEDIN_LOCATIONS
    from src.discovery.indeed import import_jobs
    job_dicts = discover_jobs(
        role_queries=LINKEDIN_ROLES,
        locations=LINKEDIN_LOCATIONS,
        max_per_search=10,
        headless=False,
    )
    if job_dicts:
        result = import_jobs(conn, job_dicts, source="linkedin_browser")
        conn.commit()
        added = result.get("added", 0) if isinstance(result, dict) else result
        print(f"  Discovered {len(job_dicts)} jobs, {added} new imported")
    else:
        print("  No new jobs discovered — will use existing approved apps")
except Exception as e:
    logger.warning("LinkedIn discovery failed: %s — using existing approved apps", e)

# ── Step 2: Score newly discovered DISCOVERED apps (no submission) ─────────────
print("\n" + "="*60)
print("  STEP 2: Scoring & gating new apps")
print("="*60)
from src.scoring.heuristic_scorer import make_heuristic_scorer
from src.verifier.bank_extractor import make_bank_extractor
from src.resume.rephrase import mock_rephraser_clean
from src.runners.overnight import run_overnight, OvernightStats
from src.runners.overnight import _process_one as _overnight_process_one

scorer    = make_heuristic_scorer(conn)
extractor = make_bank_extractor(conn)

# Only score DISCOVERED apps — do NOT touch WAITING_FOR_USER_APPROVAL apps here
# (dry_run=True would fake-submit them and advance their state)
discovered_ids = [r["id"] for r in conn.execute(
    "SELECT id FROM applications WHERE state='DISCOVERED' LIMIT 50"
).fetchall()]
stats = OvernightStats()
for _aid in discovered_ids:
    try:
        _overnight_process_one(
            conn, _aid, stats,
            scorer_fn=scorer,
            rephraser_fn=mock_rephraser_clean,
            extractor_fn=extractor,
            gmail_client=None,
            candidate_name="Pranav Tushar Pradhan",
            dry_run=True,
        )
    except Exception as _e:
        logger.warning("scoring app_id=%d: %s", _aid, _e)
print(f"  scored={stats.scored} skipped={stats.skipped} gated={stats.gated}")

# ── Step 3: Pick the best available app to fill ──────────────────────────────
print("\n" + "="*60)
print("  STEP 3: Selecting best app to fill")
print("="*60)

# Prefer apps with Workday / Greenhouse portal apply URLs, not just LinkedIn listings
# Fall back to any approved app if none with direct portal URL available
candidates = conn.execute("""
    SELECT a.id, a.score, a.folder_path, a.resume_path,
           j.company, j.title, j.platform, j.url, j.clean_jd, j.raw_jd
    FROM applications a JOIN jobs j ON a.job_id=j.id
    WHERE a.state IN ('WAITING_FOR_USER_APPROVAL','SKIPPED')
      AND a.approved_by_user = 1
    ORDER BY
      CASE WHEN LOWER(j.platform) IN ('workday','greenhouse','lever','ashby','taleo','icims')
                OR j.url LIKE '%workday%' OR j.url LIKE '%greenhouse%'
                OR j.url LIKE '%lever%' OR j.url LIKE '%ashby%' THEN 0
           WHEN LOWER(j.platform) = 'linkedin' THEN 2
           ELSE 1 END,
      a.score DESC
    LIMIT 10
""").fetchall()

if not candidates:
    print("  No approved apps found. Run overnight first to score and approve some jobs.")
    sys.exit(1)

from src.browser.auto_submit import auto_submit_portal, build_candidate_answers
from src.browser.screener_engine import precompute_screener_answers

# Try candidates in order until one succeeds (skip closed/404 jobs)
row = None
for _cand in candidates:
    print(f"  Trying: APP-{_cand['id']} — {_cand['company']} / {_cand['title']} (score={_cand['score']})")
    row = _cand
    break  # will re-try on error in Step 5

app_id   = row["id"]
folder   = (row["folder_path"] or "").replace("/", os.sep)
company  = row["company"] or "Unknown"
title    = row["title"] or "Unknown"
url      = row["url"] or ""
platform = row["platform"] or ""

print(f"  Selected: APP-{app_id} — {company} / {title}")
print(f"  Score: {row['score']}  |  Platform: {platform}")
print(f"  URL:   {url}")

if not folder:
    from src.storage.folders import create_application_folder
    folder = create_application_folder(conn, app_id).replace("/", os.sep)

# ── Step 4: Pre-compute screener answers ─────────────────────────────────────
print("\n" + "="*60)
print("  STEP 4: Pre-computing screener answers")
print("="*60)
from src.browser.auto_submit import auto_submit_portal, build_candidate_answers
from src.browser.screener_engine import precompute_screener_answers

base_answers = build_candidate_answers(
    app_id,
    job_title=title,
    company=company,
    resume_path=row["resume_path"] or "",
)
screener = precompute_screener_answers(
    app_id=app_id,
    job_title=title,
    company=company,
    clean_jd=row["clean_jd"] or row["raw_jd"] or "",
    candidate_answers=base_answers,
    cache_dir=folder,
)
answers = build_candidate_answers(
    app_id,
    job_title=title,
    company=company,
    resume_path=row["resume_path"] or "",
    screener_answers=screener.answers,
)
print(f"  {len(answers)} answers ready (llm_called={screener.llm_called})")

# ── Step 5: Fill form — PAUSE before Submit (retry on closed jobs) ─────────────
for attempt, _cand in enumerate(candidates):
    _app_id  = _cand["id"]
    _folder  = (_cand["folder_path"] or "").replace("/", os.sep)
    _company = _cand["company"] or "Unknown"
    _title   = _cand["title"] or "Unknown"
    _url     = _cand["url"] or ""

    if not _folder:
        from src.storage.folders import create_application_folder
        _folder = create_application_folder(conn, _app_id).replace("/", os.sep)

    _base = build_candidate_answers(_app_id, job_title=_title, company=_company,
                                    resume_path=_cand["resume_path"] or "")
    _sc = precompute_screener_answers(app_id=_app_id, job_title=_title, company=_company,
                                      clean_jd=_cand["clean_jd"] or _cand["raw_jd"] or "",
                                      candidate_answers=_base, cache_dir=_folder)
    _answers = build_candidate_answers(_app_id, job_title=_title, company=_company,
                                       resume_path=_cand["resume_path"] or "",
                                       screener_answers=_sc.answers)

    print("\n" + "="*60)
    print(f"  STEP 5 (attempt {attempt+1}): Filling APP-{_app_id} — {_company} / {_title}")
    print(f"  The bot fills ALL fields then STOPS before Submit.")
    print(f"  Review in Chrome, then manually click Submit if satisfied.")
    print("="*60 + "\n")

    _result = auto_submit_portal(
        _app_id, _answers,
        url=_url,
        folder_path=_folder,
        pause_before_submit=True,
    )

    _err = (_result.error or "").lower()
    _skip_signals = ("closed", "no longer", "no longer accepting", "not found",
                     "could not find submit", "form may have multiple", "no apply button",
                     "404", "page not found")
    if _result.error and any(s in _err for s in _skip_signals):
        print(f"  Skipping (job unavailable or no form): {_result.error[:80]}")
        continue

    app_id = _app_id
    break

print("\n" + "="*60)
print(f"  Review mode ended for APP-{app_id}")
print(f"  - If you manually submitted: run the DB update below to record it")
print(f"  - If you abandoned: no action needed")
print()
print("  To mark as SUBMITTED manually, run:")
cmd = (
    "python -c \"import sqlite3,datetime; c=sqlite3.connect('job_agent.db'); "
    "c.execute('UPDATE applications SET state=\\'SUBMITTED\\',submitted_at=? WHERE id=" + str(app_id) + "', "
    "(datetime.datetime.utcnow().isoformat(),)); c.commit(); print('done')\""
)
print(f"  {cmd}")
print("="*60)

conn.close()
