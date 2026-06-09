"""
Fill-only mode: picks the best approved application, fills the form in Chrome,
then STOPS before clicking Submit for manual review.

Skips LinkedIn discovery (run run_linkedin_discovery.py separately if needed).

Usage:
  python run_fill_only.py
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

# ── Pick best approved app to fill ───────────────────────────────────────────
print("\n" + "="*60)
print("  Selecting best approved app to fill")
print("="*60)

candidates = conn.execute("""
    SELECT a.id, a.score, a.folder_path, a.resume_path,
           j.company, j.title, j.platform, j.url, j.clean_jd, j.raw_jd
    FROM applications a JOIN jobs j ON a.job_id=j.id
    WHERE a.state IN ('WAITING_FOR_USER_APPROVAL','SKIPPED')
      AND a.approved_by_user = 1
      AND a.id NOT IN (3)
      AND LOWER(j.platform) NOT IN ('remoteok','hackernews','custom')
      AND j.url NOT LIKE '%remoteok.com%'
      AND j.url NOT LIKE '%nomi.ai%'
    ORDER BY
      CASE WHEN LOWER(j.platform) = 'linkedin' THEN 0
           WHEN LOWER(j.platform) IN ('workday','greenhouse','lever','ashby','taleo','icims')
                OR j.url LIKE '%workday%' OR j.url LIKE '%greenhouse%'
                OR j.url LIKE '%lever%' OR j.url LIKE '%ashby%' THEN 1
           ELSE 2 END,
      a.score DESC
    LIMIT 15
""").fetchall()

if not candidates:
    print("  No approved apps found.")
    sys.exit(1)

for r in candidates:
    print(f"  Candidate: APP-{r['id']} [{r['platform']}] score={r['score']} — {r['company']} / {r['title']}")

from src.browser.auto_submit import auto_submit_portal, build_candidate_answers
from src.browser.screener_engine import precompute_screener_answers

# ── Fill form — PAUSE before Submit ──────────────────────────────────────────
app_id = None
for attempt, _cand in enumerate(candidates):
    _app_id  = _cand["id"]
    _folder  = (_cand["folder_path"] or "").replace("/", os.sep)
    _company = _cand["company"] or "Unknown"
    _title   = _cand["title"] or "Unknown"
    _url     = _cand["url"] or ""
    _platform = _cand["platform"] or ""

    if not _folder:
        from src.storage.folders import create_application_folder
        _folder = create_application_folder(conn, _app_id).replace("/", os.sep)

    print(f"\n{'='*60}")
    print(f"  Attempt {attempt+1}: APP-{_app_id} — {_company} / {_title}")
    print(f"  Platform: {_platform}  |  URL: {_url[:80]}")
    print(f"  Pre-computing screener answers...")
    print(f"{'='*60}")

    _base = build_candidate_answers(_app_id, job_title=_title, company=_company,
                                    resume_path=_cand["resume_path"] or "")
    _sc = precompute_screener_answers(
        app_id=_app_id, job_title=_title, company=_company,
        clean_jd=_cand["clean_jd"] or _cand["raw_jd"] or "",
        candidate_answers=_base, cache_dir=_folder,
    )
    _answers = build_candidate_answers(_app_id, job_title=_title, company=_company,
                                       resume_path=_cand["resume_path"] or "",
                                       screener_answers=_sc.answers)
    print(f"  {len(_answers)} answers ready (llm_called={_sc.llm_called})")

    print(f"\n  Launching Chrome and filling form...")
    print(f"  Chrome will STOP before Submit — review and submit manually.")

    _result = auto_submit_portal(
        _app_id, _answers,
        url=_url,
        folder_path=_folder,
        pause_before_submit=True,
    )

    _err = (_result.error or "").lower()
    _skip_signals = (
        "closed", "no longer", "no longer accepting", "not found",
        "could not find submit", "form may have multiple", "no apply button",
        "404", "page not found",
        "no continue/submit button", "smartapply: no",
    )
    if _result.mfa_detected:
        print(f"  Skipping (login/MFA required — not logged in to this portal)")
        continue
    if _result.error and any(s in _err for s in _skip_signals):
        print(f"  Skipping (job unavailable or no form): {_result.error[:100]}")
        continue

    if _result.error:
        print(f"  Error: {_result.error[:100]}")
        # non-skip errors: still break and report
    app_id = _app_id
    break

if app_id is None:
    print("\n  All candidates failed or were unavailable.")
    conn.close()
    sys.exit(1)

print("\n" + "="*60)
print(f"  Review mode ended for APP-{app_id}")
print(f"  - If you manually submitted: run the command below to record it")
print(f"  - If you abandoned: no action needed")
print()
print("  To mark as SUBMITTED manually, run:")
print(f"  python -c \"import sqlite3,datetime; c=sqlite3.connect('job_agent.db'); "
      f"c.execute(\\\"UPDATE applications SET state='SUBMITTED',submitted_at=? WHERE id={app_id}\\\", "
      f"(datetime.datetime.utcnow().isoformat(),)); c.commit(); print('done')\"")
print("="*60)

conn.close()
