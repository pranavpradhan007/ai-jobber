"""
Auto-submit mode: iterates all approved applications, fills and submits each one,
then marks it as SUBMITTED in the DB.

Skips closed/unavailable jobs and moves on to the next candidate.

Usage:
  python run_fill_only.py
"""
import logging
import os
import sys
import pathlib
import datetime

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

# ── Pick all approved apps to fill ───────────────────────────────────────────
print("\n" + "="*60)
print("  Auto-submit: selecting all approved apps")
print("="*60)

candidates = conn.execute("""
    SELECT a.id, a.score, a.folder_path, a.resume_path,
           j.company, j.title, j.platform, j.url, j.clean_jd, j.raw_jd
    FROM applications a JOIN jobs j ON a.job_id=j.id
    WHERE a.state IN ('WAITING_FOR_USER_APPROVAL','SKIPPED')
      AND a.approved_by_user = 1
      AND a.id NOT IN (3, 47, 48, 50, 56, 93, 125)
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
    LIMIT 20
""").fetchall()

if not candidates:
    print("  No approved apps found.")
    sys.exit(0)

for r in candidates:
    print(f"  Candidate: APP-{r['id']} [{r['platform']}] score={r['score']} — {r['company']} / {r['title']}")

from src.browser.auto_submit import auto_submit_portal, build_candidate_answers
from src.browser.screener_engine import precompute_screener_answers

submitted_ids = []
skipped_ids = []
error_ids = []

_SKIP_SIGNALS = (
    "closed", "no longer", "no longer accepting", "not found",
    "could not find submit", "form may have multiple", "no apply button",
    "404", "page not found",
    "no continue/submit button", "smartapply: no",
    "could not complete after", "easy apply button not found",
    "job is closed", "not accepting",
    "modal did not open",  # LinkedIn job uses external apply, not Easy Apply
)

for _cand in candidates:
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
    print(f"  APP-{_app_id} — {_company} / {_title}")
    print(f"  Platform: {_platform}  |  URL: {_url[:80]}")
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
    print(f"  {len(_answers)} answers ready")

    _result = auto_submit_portal(
        _app_id, _answers,
        url=_url,
        folder_path=_folder,
        pause_before_submit=False,
    )

    _err = (_result.error or "").lower()

    if _result.mfa_detected:
        print(f"  Skipping (login/MFA required)")
        skipped_ids.append(_app_id)
        continue

    if _result.error and any(s in _err for s in _SKIP_SIGNALS):
        print(f"  Skipping (unavailable): {_result.error[:100]}")
        skipped_ids.append(_app_id)
        # Permanently mark as SKIPPED for signals that won't ever resolve
        _PERMANENT_SKIP = ("modal did not open", "no longer accepting",
                           "easy apply button not found", "job is closed")
        if any(p in _err for p in _PERMANENT_SKIP):
            conn.execute("UPDATE applications SET state='SKIPPED' WHERE id=?", (_app_id,))
            conn.commit()
            print(f"  Marked APP-{_app_id} as SKIPPED in DB (permanent)")
        continue

    if _result.error:
        print(f"  Error: {_result.error[:100]}")
        error_ids.append(_app_id)
        continue

    if _result.success:
        # Mark as SUBMITTED in DB
        conn.execute(
            "UPDATE applications SET state='SUBMITTED', submitted_at=? WHERE id=?",
            (datetime.datetime.utcnow().isoformat(), _app_id),
        )
        conn.commit()
        submitted_ids.append(_app_id)
        print(f"  SUBMITTED APP-{_app_id} — {_company} / {_title}")
    else:
        print(f"  Not submitted (result.success=False, error={_result.error})")
        error_ids.append(_app_id)

print("\n" + "="*60)
print(f"  Run complete")
print(f"  Submitted: {len(submitted_ids)} — {submitted_ids}")
print(f"  Skipped:   {len(skipped_ids)} — {skipped_ids}")
print(f"  Errors:    {len(error_ids)} — {error_ids}")
print("="*60)

conn.close()
