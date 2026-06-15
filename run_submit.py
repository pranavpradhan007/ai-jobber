"""Submit all READY_TO_SUBMIT apps via Phase 2 of the overnight runner."""
import os, sys, pathlib, logging

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["JOB_AGENT_DB"] = "job_agent.db"
from dotenv import load_dotenv; load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s -- %(message)s",
    handlers=[
        logging.FileHandler("loop.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

import src.storage.folders as fm
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())

from src.db.connection import get_connection
from src.scoring.heuristic_scorer import make_heuristic_scorer
from src.verifier.bank_extractor import make_bank_extractor
from src.resume.rephrase import mock_rephraser_clean
from src.runners.overnight import run_overnight

conn = get_connection("job_agent.db")
scorer    = make_heuristic_scorer(conn)
extractor = make_bank_extractor(conn)

print("Submitting READY_TO_SUBMIT apps via overnight Phase 2...")
stats = run_overnight(
    conn,
    scorer_fn=scorer,
    rephraser_fn=mock_rephraser_clean,
    extractor_fn=extractor,
    candidate_name="Pranav Tushar Pradhan",
    dry_run=False,
)
print(f"Done: scored={stats.scored} submitted={stats.submitted} skipped={stats.skipped} failed={stats.failed}")
conn.close()
