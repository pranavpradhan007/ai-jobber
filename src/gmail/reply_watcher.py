"""
Gmail reply watcher.

Finds replies to morning digest emails and returns the raw reply text
for the approval parser to process.

Flow (MCP-bridged):
  1. Python writes a search request manifest: gmail_actions/pending/search_digest_replies_*.json
  2. Claude Code calls MCP search_threads with query "[ai-jobber] DIGEST-"
  3. Claude Code calls MCP get_thread for each matched thread
  4. Claude Code calls write_reply_results() with the extracted reply text
  5. Python reads the results and passes to parse_reply()

For each digest_id, we:
  - Search for threads with subject containing the digest_id
  - Find messages FROM the user (not from ai-jobber itself)
  - Extract only the NEW content (skip quoted previous messages)
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.gmail.client import MCPGmailClient, _now, _ACTIONS_DIR

logger = logging.getLogger(__name__)

_RESULTS_DIR = _ACTIONS_DIR / "results"
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def watch_for_replies(
    conn: sqlite3.Connection,
    *,
    gmail_client: Optional[MCPGmailClient] = None,
    digest_id: Optional[str] = None,
) -> dict[str, str]:
    """
    Queue a Gmail search request for digest replies.

    Returns a dict of {digest_id: reply_text} for any replies already
    cached in results/. Call this twice:
      1st call → queues the search request, returns {}
      2nd call (after Claude Code executes MCP) → returns the replies

    If digest_id is given, watches only that specific digest.
    Otherwise watches all unprocessed digests.
    """
    if gmail_client is None:
        gmail_client = MCPGmailClient()

    # Find unprocessed digests
    if digest_id:
        rows = conn.execute(
            "SELECT digest_id FROM digests WHERE digest_id=? AND processed=0",
            (digest_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT digest_id FROM digests WHERE processed=0 ORDER BY sent_at"
        ).fetchall()

    if not rows:
        logger.info("No unprocessed digests to watch")
        return {}

    results: dict[str, str] = {}

    for row in rows:
        did = row["digest_id"]
        result_path = _RESULTS_DIR / f"reply_{did}.json"

        if result_path.exists():
            # Results already written by Claude Code — read them
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                reply_text = data.get("reply_text", "")
                if reply_text:
                    results[did] = reply_text
                    logger.info("Loaded cached reply for %s (%d chars)", did, len(reply_text))
            except Exception as exc:
                logger.warning("Failed to read reply result for %s: %s", did, exc)
        else:
            # Queue a search request for Claude Code to execute
            query = f'subject:"[ai-jobber]" subject:"{did}"'
            _queue_search(gmail_client, did, query)

    return results


def write_reply_results(digest_id: str, reply_text: str) -> None:
    """
    Called by Claude Code after executing MCP get_thread.
    Writes the reply text so watch_for_replies() can read it next call.
    """
    result_path = _RESULTS_DIR / f"reply_{digest_id}.json"
    result_path.write_text(
        json.dumps({
            "digest_id":  digest_id,
            "reply_text": reply_text,
            "written_at": _now(),
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote reply result for %s (%d chars)", digest_id, len(reply_text))


def mark_digest_processed(conn: sqlite3.Connection, digest_id: str) -> None:
    """Mark a digest as fully processed so we don't re-parse it."""
    conn.execute(
        "UPDATE digests SET processed=1 WHERE digest_id=?",
        (digest_id,),
    )
    conn.commit()
    # Clean up result cache
    result_path = _RESULTS_DIR / f"reply_{digest_id}.json"
    if result_path.exists():
        result_path.unlink()


def _queue_search(
    client: MCPGmailClient,
    digest_id: str,
    query: str,
) -> None:
    """Write a pending search manifest for Claude Code."""
    req_id = f"search_digest_{digest_id}"
    manifest = {
        "action":     "search_digest_replies",
        "request_id": req_id,
        "digest_id":  digest_id,
        "query":      query,
        "created_at": _now(),
        "status":     "pending",
        "instruction": (
            f"Search Gmail for threads matching: {query!r}. "
            f"For each matching thread, call get_thread and extract all messages "
            f"that are replies FROM the user (not the original digest message). "
            f"Concatenate the plain-text body of the reply messages. "
            f"Then call write_reply_results('{digest_id}', reply_text)."
        ),
    }
    req_path = client._dir / "pending" / f"{req_id}.json"
    if not req_path.exists():
        req_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        logger.info("Queued digest reply search for %s", digest_id)


def extract_reply_body(full_thread_body: str) -> str:
    """
    Strip quoted previous messages (lines starting with '>') from a Gmail reply.
    Returns only the new content the user typed.
    """
    lines = []
    for line in full_thread_body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", stripped):
            break   # everything after "On X wrote:" is quoted
        lines.append(line)
    return "\n".join(lines).strip()
