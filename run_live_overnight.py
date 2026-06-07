"""Run the overnight pipeline on the newly imported real jobs."""
import logging
import os
import sys

from dotenv import load_dotenv
load_dotenv()  # load .env into os.environ before anything else

# Ensure UTF-8 output on Windows (avoids UnicodeEncodeError with arrow chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["JOB_AGENT_DB"] = "job_agent.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("job_agent.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

from src.db.connection import get_connection
from src.scoring.heuristic_scorer import make_heuristic_scorer
from src.verifier.bank_extractor import make_bank_extractor
from src.resume.rephrase import mock_rephraser_clean
from src.runners.overnight import run_overnight
from src.gmail.client import MCPGmailClient
from src.gmail.digest import build_digest, is_quiet_hours
import src.storage.folders as fm

# Use the real applications/ folder this time (not temp)
import pathlib
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())
pathlib.Path("applications").mkdir(exist_ok=True)

conn = get_connection("job_agent.db")
scorer    = make_heuristic_scorer(conn)
extractor = make_bank_extractor(conn)

stats = run_overnight(
    conn,
    scorer_fn=scorer,
    rephraser_fn=mock_rephraser_clean,
    extractor_fn=extractor,
    candidate_name="Pranav Tushar Pradhan",
    dry_run=False,   # REAL run — will submit auto_safe, gate gated
)

print("\n=== OVERNIGHT RESULTS ===")
print(f"  scored    {stats.scored}")
print(f"  skipped   {stats.skipped}")
print(f"  tailored  {stats.tailored}")
print(f"  submitted {stats.submitted}")
print(f"  gated     {stats.gated}")
print(f"  failed    {stats.failed}")
if stats.errors:
    for e in stats.errors:
        print(f"  ERROR: {e}")

print("\n=== APPLICATION STATES ===")
for row in conn.execute(
    "SELECT a.id, j.company, j.title, a.state, a.score, a.submit_tier "
    "FROM applications a JOIN jobs j ON j.id=a.job_id "
    "ORDER BY a.id"
):
    print(f"  APP-{row['id']}: {row['company'][:30]:30s} | {row['state']:35s} | score={row['score'] or 'n/a'} | tier={row['submit_tier']}")

print("\n=== SENDING DIGEST ===")
recipient = os.environ.get("DIGEST_RECIPIENT") or os.environ.get("YOUR_EMAIL_ADDRESS", "")
if recipient:
    if is_quiet_hours():
        from datetime import datetime
        print(f"  Quiet hours active — digest suppressed until 08:00")
    else:
        gmail = MCPGmailClient()
        digest_id = build_digest(conn, recipient=recipient, gmail_client=gmail)
        if digest_id:
            print(f"  Digest sent to {recipient}")
            print(f"  Digest ID: {digest_id}")
        else:
            print("  No gated applications — digest skipped")
else:
    print("  DIGEST_RECIPIENT not set in .env — skipping digest")

conn.close()
