"""
Standalone entrypoint for the continuous job-agent loop.
Run this at startup — it handles discovery, scoring, tailoring,
submission, and digest emails on a continuous schedule.

Usage:
  python run_continuous_loop.py
  python run_continuous_loop.py --pipeline-interval 15   # check every 15 min
  python run_continuous_loop.py --once                   # single cycle
"""
import argparse
import logging
import os
import pathlib
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

os.environ["JOB_AGENT_DB"] = "job_agent.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("continuous.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

import src.storage.folders as fm
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())
pathlib.Path("applications").mkdir(exist_ok=True)

from src.db.connection import get_connection
conn = get_connection("job_agent.db")

parser = argparse.ArgumentParser(description="Job-agent continuous loop")
parser.add_argument("--pipeline-interval", type=int, default=30)
parser.add_argument("--discover-interval", type=int, default=240)
parser.add_argument("--once", action="store_true")
args = parser.parse_args()

from src.runners.continuous import run_continuous

print("\n" + "="*60)
print("  Job Agent — Continuous Loop")
print(f"  Pipeline every {args.pipeline_interval} min | Discovery every {args.discover_interval} min")
print("  Ctrl-C to stop")
print("="*60 + "\n")

run_continuous(
    conn,
    pipeline_interval=args.pipeline_interval,
    discover_interval=args.discover_interval,
    once=args.once,
    digest_recipient=os.environ.get("YOUR_EMAIL_ADDRESS", "pranavpradhan00721@gmail.com"),
)
