"""
Continuous job discovery + application loop.

Runs forever: discover new Easy Apply jobs, immediately apply to them, repeat.
Discovery and apply share the single Chrome session (port 9222).

Usage:
    python run_continuous.py

Ctrl-C to stop.
"""
import logging
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

# Single-instance guard — exit immediately if another copy is already running.
_LOCK_PATH = pathlib.Path("job_agent_continuous.lock")
_lock_fh = None
try:
    import msvcrt as _msvcrt
    _lock_fh = open(_LOCK_PATH, "w")
    _msvcrt.locking(_lock_fh.fileno(), _msvcrt.LK_NBLCK, 1)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()
except OSError:
    print("Another run_continuous.py instance is already running — exiting.")
    sys.exit(0)
except Exception:
    pass  # Non-Windows fallback: proceed without lock

from dotenv import load_dotenv
load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["JOB_AGENT_DB"] = "job_agent.db"
os.environ["CHROME_CDP_PORT"] = "9222"

import src.storage.folders as fm
fm.APPLICATIONS_BASE = str(pathlib.Path("applications").resolve())
pathlib.Path("applications").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("continuous.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("continuous")

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BROWSER_PROFILE = str(pathlib.Path("browser_profile").resolve())
CDP_PORT = 9222


def _cdp_alive() -> bool:
    try:
        urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _ensure_chrome() -> bool:
    if _cdp_alive():
        return True
    subprocess.Popen([
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={BROWSER_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(15):
        time.sleep(1)
        if _cdp_alive():
            logger.info("Chrome CDP ready on port %d", CDP_PORT)
            return True
    logger.error("Chrome did not start")
    return False


def run_discovery() -> int:
    """Discover jobs via Indeed and external feeds. Returns count added."""
    from src.db.connection import get_connection
    from src.discovery.feeds import import_cached_feeds

    logger.info("=== DISCOVERY PASS starting ===")
    conn = get_connection("job_agent.db")
    feeds = import_cached_feeds(conn)
    conn.commit()
    conn.close()
    added = feeds.get("added", 0) if isinstance(feeds, dict) else 0
    logger.info("Discovery: imported %d new jobs from feeds", added)
    return added


def run_apply() -> dict:
    """Score, tailor, and apply to all DISCOVERED jobs. Returns stats dict."""
    from src.db.connection import get_connection
    from src.scoring.heuristic_scorer import make_heuristic_scorer
    from src.verifier.bank_extractor import make_bank_extractor
    from src.resume.rephrase import mock_rephraser_clean
    from src.runners.overnight import run_overnight

    conn = get_connection("job_agent.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM applications WHERE state IN "
        "('DISCOVERED','SCORED','TAILORING','RESUME_VERIFIED','PACKAGING','READY_TO_SUBMIT')"
    )
    pending = cur.fetchone()[0]
    conn.close()

    if pending == 0:
        logger.info("Apply: no jobs in pipeline to process")
        return {}

    logger.info("=== APPLY PASS starting — %d jobs in pipeline ===", pending)
    conn = get_connection("job_agent.db")
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
    conn.close()
    rate_limited = getattr(stats, "rate_limited", 0)
    logger.info(
        "Apply: scored=%d submitted=%d gated=%d failed=%d skipped=%d rate_limited=%d",
        stats.scored, stats.submitted, stats.gated, stats.failed, stats.skipped, rate_limited,
    )
    return {"submitted": stats.submitted, "scored": stats.scored, "rate_limited": rate_limited}


def main() -> None:
    logger.info("=== Continuous job agent starting ===")

    if not _ensure_chrome():
        logger.error("Cannot start Chrome — aborting")
        sys.exit(1)

    total_submitted = 0
    pass_num = 0

    while True:
        pass_num += 1
        logger.info("--- Pass %d ---", pass_num)

        try:
            # Apply to any already-discovered jobs first (fast path)
            if not _ensure_chrome():
                logger.warning("Chrome unavailable for apply — skipping apply phase")
            else:
                apply_stats = run_apply()
                total_submitted += apply_stats.get("submitted", 0)
                if apply_stats.get("rate_limited", 0) > 0:
                    logger.info(
                        "rate-limited — will retry next pass"
                    )

            # Ensure Chrome is up before discovery (apply may have left it in odd state)
            if not _ensure_chrome():
                logger.warning("Chrome unavailable for discovery — skipping discovery phase")
                time.sleep(30)
                continue

            # Then discover more
            added = run_discovery()

            # If new jobs found, apply immediately
            if added > 0:
                if _ensure_chrome():
                    apply_stats2 = run_apply()
                    total_submitted += apply_stats2.get("submitted", 0)
                    if apply_stats2.get("rate_limited", 0) > 0:
                        logger.info(
                            "rate-limited — will retry next pass"
                        )

            logger.info("Pass %d complete. Total submitted this session: %d", pass_num, total_submitted)

            logger.info("Sleeping 60s before next pass...")
            time.sleep(60)

        except KeyboardInterrupt:
            logger.info("Stopped by user. Total submitted: %d", total_submitted)
            break
        except Exception as exc:
            logger.error("Pass %d error: %s — retrying in 30s", pass_num, exc, exc_info=True)
            time.sleep(30)
            if not _ensure_chrome():
                logger.error("Chrome lost and couldn't restart — waiting 60s")
                time.sleep(60)


if __name__ == "__main__":
    main()
