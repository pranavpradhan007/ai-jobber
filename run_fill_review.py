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
    from src.discovery.linkedin_browser import discover_jobs
    new_jobs = discover_jobs(conn, max_per_search=10)
    print(f"  Discovered {len(new_jobs)} new jobs")
except Exception as e:
    logger.warning("LinkedIn discovery failed: %s — using existing approved apps", e)
    new_jobs = []

# ── Step 2: Score newly discovered apps ──────────────────────────────────────
print("\n" + "="*60)
print("  STEP 2: Scoring & gating")
print("="*60)
from src.scoring.heuristic_scorer import make_heuristic_scorer
from src.verifier.bank_extractor import make_bank_extractor
from src.resume.rephrase import mock_rephraser_clean
from src.runners.overnight import run_overnight

scorer    = make_heuristic_scorer(conn)
extractor = make_bank_extractor(conn)

stats = run_overnight(
    conn,
    scorer_fn=scorer,
    rephraser_fn=mock_rephraser_clean,
    extractor_fn=extractor,
    candidate_name="Pranav Tushar Pradhan",
    dry_run=True,   # score + gate only; do NOT submit yet
)
print(f"  scored={stats.scored} skipped={stats.skipped} tailored={stats.tailored}")

# ── Step 3: Pick the best WAITING_FOR_USER_APPROVAL app with a portal URL ────
print("\n" + "="*60)
print("  STEP 3: Selecting best app to fill")
print("="*60)

# Prefer apps with Workday / Greenhouse portal apply URLs, not just LinkedIn listings
# Fall back to any approved app if none with direct portal URL available
cur = conn.execute("""
    SELECT a.id, a.score, a.folder_path, a.resume_path,
           j.company, j.title, j.platform, j.url, j.clean_jd, j.raw_jd
    FROM applications a JOIN jobs j ON a.job_id=j.id
    WHERE a.state IN ('WAITING_FOR_USER_APPROVAL','SKIPPED')
      AND a.approved_by_user = 1
    ORDER BY
      CASE WHEN j.url LIKE '%workday%' OR j.url LIKE '%greenhouse%'
                OR j.url LIKE '%lever%' OR j.url LIKE '%ashby%' THEN 0
           ELSE 1 END,
      a.score DESC
    LIMIT 1
""")
row = cur.fetchone()

if row is None:
    print("  No approved apps found. Run overnight first to score and approve some jobs.")
    sys.exit(1)

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

# ── Step 5: Fill form — PAUSE before Submit ───────────────────────────────────
print("\n" + "="*60)
print(f"  STEP 5: Filling form for APP-{app_id} — Chrome will open")
print(f"  The bot will fill ALL fields then STOP before Submit.")
print(f"  Review the form in Chrome, then manually click Submit if satisfied.")
print("="*60 + "\n")

result = auto_submit_portal(
    app_id, answers,
    url=url,
    folder_path=folder,
    pause_before_submit=True,   # fill everything, then STOP
)

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
