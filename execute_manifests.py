"""Execute all pending discovery manifests in gmail_actions/pending/."""
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

from src.db.connection import get_connection
from src.discovery.indeed import import_jobs
from src.discovery.linkedin_email import write_linkedin_jobs, parse_linkedin_alert_email
from src.discovery.feeds import write_feed_results


def load_manifests(pending_dir: Path) -> list[tuple[Path, dict]]:
    """Load all pending manifests, sorted by timestamp."""
    manifests = []
    for path in sorted(pending_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifests.append((path, data))
        except Exception as e:
            logger.error(f"Failed to load manifest {path}: {e}")
    return manifests


def execute_discover_jobs(manifest: dict, conn: sqlite3.Connection) -> dict:
    """Execute the Indeed job discovery manifest."""
    logger.info("Executing discover_jobs manifest...")
    searches = manifest.get("searches", [])
    limit = manifest.get("limit", 10)

    all_jobs = []
    for idx, search_item in enumerate(searches, 1):
        search_term = search_item.get("search")
        location = search_item.get("location")
        logger.info(f"  [{idx}/{len(searches)}] Searching: '{search_term}' in {location}")

        try:
            # Import here to trigger MCP tool availability
            from mcp__claude_ai_Indeed__search_jobs import mcp__claude_ai_Indeed__search_jobs

            # This would need to be called via the MCP interface
            # For now, we'll use a placeholder that Claude Code can execute
            logger.warning(f"    Skipping (requires MCP call) — search_jobs('{search_term}', '{location}')")
        except ImportError:
            logger.warning("    MCP tools not directly callable from Python; returning placeholder")

    return {
        "action": "discover_jobs",
        "searches_processed": len(searches),
        "jobs_collected": 0,
        "status": "pending_mcp_execution"
    }


def execute_linkedin_alerts(manifest: dict, conn: sqlite3.Connection) -> dict:
    """Execute the LinkedIn alerts manifest."""
    request_id = manifest.get("request_id")
    query = manifest.get("query")
    logger.info(f"Executing LinkedIn alerts manifest (request_id={request_id})...")
    logger.warning(f"  Requires Gmail MCP calls — query: {query}")

    return {
        "action": "fetch_linkedin_job_alerts",
        "request_id": request_id,
        "status": "pending_gmail_search"
    }


def execute_feed_manifest(manifest: dict, source: str) -> dict:
    """Execute a feed fetch manifest (RemoteOK, WWR, HN)."""
    request_id = manifest.get("request_id")
    urls = manifest.get("urls", [])
    logger.info(f"Executing {source} feed manifest (request_id={request_id})...")
    logger.info(f"  URLs to fetch: {len(urls)}")
    for url in urls:
        logger.info(f"    - {url}")

    return {
        "action": "fetch_feed",
        "source": source,
        "request_id": request_id,
        "urls_to_fetch": len(urls),
        "status": "pending_webfetch"
    }


def main():
    pending_dir = Path("gmail_actions/pending")
    if not pending_dir.exists():
        logger.error(f"Pending directory not found: {pending_dir}")
        return

    manifests = load_manifests(pending_dir)
    logger.info(f"Loaded {len(manifests)} pending manifests")

    if not manifests:
        logger.info("No pending manifests to execute")
        return

    conn = get_connection()

    results = {
        "discover_jobs": [],
        "linkedin_alerts": [],
        "feeds": [],
    }

    for path, manifest in manifests:
        action = manifest.get("action")
        logger.info(f"\n--- {path.name} ---")
        logger.info(f"Action: {action}")

        try:
            if action == "discover_jobs":
                result = execute_discover_jobs(manifest, conn)
                results["discover_jobs"].append(result)
            elif action == "fetch_linkedin_job_alerts":
                result = execute_linkedin_alerts(manifest, conn)
                results["linkedin_alerts"].append(result)
            elif action == "fetch_feed":
                source = manifest.get("source")
                result = execute_feed_manifest(manifest, source)
                results["feeds"].append(result)
            else:
                logger.warning(f"Unknown action: {action}")
        except Exception as e:
            logger.error(f"Failed to execute manifest: {e}", exc_info=True)

    logger.info("\n=== EXECUTION SUMMARY ===")
    logger.info(f"Discover Jobs: {len(results['discover_jobs'])} manifests")
    logger.info(f"LinkedIn Alerts: {len(results['linkedin_alerts'])} manifests")
    logger.info(f"Feeds: {len(results['feeds'])} manifests")
    logger.info("\nManifests loaded but require MCP/WebFetch calls to execute.")
    logger.info("Claude Code should invoke the MCP tools and WebFetch as per manifest instructions.")

    conn.close()


if __name__ == "__main__":
    main()
