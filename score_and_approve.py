"""
Score newly DISCOVERED jobs and approve all that pass the gate.

Flow:
  1. Run overnight scorer on all DISCOVERED apps (heuristic, no LLM)
  2. Print WAITING_FOR_USER_APPROVAL apps that need approval
  3. Auto-approve all LinkedIn Easy Apply apps scoring >= MIN_SCORE

Usage:
  python score_and_approve.py
"""
import logging, os, sys, pathlib

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["JOB_AGENT_DB"] = "job_agent.db"
from dotenv import load_dotenv; load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("score_approve.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

import src.storage.folders as fm
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())
pathlib.Path("applications").mkdir(exist_ok=True)

from src.db.connection import get_connection
from src.scoring.heuristic_scorer import make_heuristic_scorer
from src.verifier.bank_extractor import make_bank_extractor
from src.resume.rephrase import mock_rephraser_clean
from src.runners.overnight import run_overnight

MIN_SCORE = 60.0   # approve LinkedIn Easy Apply apps at or above this score

conn = get_connection("job_agent.db")

# How many DISCOVERED apps?
disc_count = conn.execute(
    "SELECT COUNT(*) FROM applications WHERE state='DISCOVERED'"
).fetchone()[0]
print(f"\nDISCOVERED apps to score: {disc_count}")

if disc_count > 0:
    scorer    = make_heuristic_scorer(conn)
    extractor = make_bank_extractor(conn)

    stats = run_overnight(
        conn,
        scorer_fn=scorer,
        rephraser_fn=mock_rephraser_clean,
        extractor_fn=extractor,
        candidate_name="Pranav Tushar Pradhan",
        dry_run=False,
    )
    print(f"\nScoring done: scored={stats.scored} skipped={stats.skipped} "
          f"tailored={stats.tailored} gated={stats.gated}")
else:
    print("Nothing to score.")

# Show and auto-approve high-scoring LinkedIn Easy Apply apps
waiting = conn.execute("""
    SELECT a.id, a.score, a.approved_by_user, j.company, j.title, j.platform, j.url
    FROM applications a JOIN jobs j ON a.job_id = j.id
    WHERE a.state = 'WAITING_FOR_USER_APPROVAL'
    ORDER BY a.score DESC
""").fetchall()

print(f"\nWAITING_FOR_USER_APPROVAL apps: {len(waiting)}")
approved_ids = []
for r in waiting:
    already = "✓" if r["approved_by_user"] else " "
    score_str = f"{r['score']:.1f}" if r['score'] is not None else "n/a"
    print(f"  [{already}] APP-{r['id']} [{r['platform']}] score={score_str} "
          f"— {r['company']} / {r['title'][:50]}")

    # Auto-approve LinkedIn Easy Apply jobs above threshold
    if (r["platform"] == "linkedin"
            and r["score"] >= MIN_SCORE
            and not r["approved_by_user"]):
        conn.execute(
            "UPDATE applications SET approved_by_user=1 WHERE id=?", (r["id"],)
        )
        approved_ids.append(r["id"])
        print(f"    → auto-approved (LinkedIn Easy Apply, score={r['score']:.1f})")

if approved_ids:
    conn.commit()
    print(f"\nAuto-approved {len(approved_ids)} LinkedIn apps: {approved_ids}")
else:
    print("\nNo new auto-approvals.")

print("\nRun: python run_fill_only.py")
conn.close()
