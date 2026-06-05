"""
MCP Gmail executor.

This module is called BY Claude Code (not by standalone Python).
It reads pending action manifests from gmail_actions/pending/,
executes them via Claude Code's MCP Gmail tools, and marks them done.

Usage (from a Claude Code conversation):
    from src.gmail.mcp_executor import get_pending_actions, summarise_pending
    summarise_pending()   # shows what needs to be executed

Claude Code then calls the appropriate MCP tools based on the manifest type
and calls mcp_client.mark_done(action_id, result) to complete the action.

No credentials. No OAuth. This file never imports google-api-python-client.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from src.gmail.client import MCPGmailClient, GmailMessage

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACTIONS_DIR = _REPO_ROOT / "gmail_actions"


def get_pending_actions(actions_dir: Optional[str] = None) -> list[dict]:
    """Return all pending Gmail action manifests, oldest first."""
    client = MCPGmailClient(actions_dir)
    return client.pending_actions()


def summarise_pending(actions_dir: Optional[str] = None) -> str:
    """
    Print a human-readable summary of pending Gmail actions.
    Call this from a Claude Code session to see what MCP calls are needed.
    """
    actions = get_pending_actions(actions_dir)
    if not actions:
        return "No pending Gmail actions."

    lines = [f"{len(actions)} pending Gmail action(s):"]
    for a in actions:
        action_type = a.get("action", "unknown")
        if action_type == "create_draft":
            lines.append(
                f"  [{a['action_id']}] CREATE DRAFT  to={a.get('to')}  "
                f"subject={a.get('subject', '')[:50]!r}"
            )
        elif action_type == "search_threads":
            lines.append(
                f"  [{a['request_id']}] SEARCH  query={a.get('query', '')!r}"
            )
        else:
            lines.append(f"  [{a.get('action_id','?')}] {action_type}")
    return "\n".join(lines)


def build_mcp_instructions(actions_dir: Optional[str] = None) -> str:
    """
    Return step-by-step MCP instructions for Claude Code to execute
    each pending action. Claude Code pastes the output into a tool call.
    """
    actions = get_pending_actions(actions_dir)
    if not actions:
        return "No actions pending."

    lines = ["Execute these Gmail actions via MCP tools:\n"]
    for a in actions:
        action_type = a.get("action", "unknown")
        if action_type == "create_draft":
            lines.append(
                f"create_draft:\n"
                f"  action_id : {a['action_id']}\n"
                f"  to        : {a.get('to', [])}\n"
                f"  subject   : {a.get('subject','')}\n"
                f"  body      : (see manifest)\n"
            )
        elif action_type == "search_threads":
            lines.append(
                f"search_threads:\n"
                f"  request_id: {a['request_id']}\n"
                f"  query     : {a.get('query','')}\n"
            )
    return "\n".join(lines)


def load_mcp_search_results(
    thread_data: list[dict],
    request_id: str,
    actions_dir: Optional[str] = None,
) -> None:
    """
    Write MCP search results so the Python pipeline can read them.
    Call after Claude Code executes a search_threads MCP tool.

    thread_data: list of dicts with keys matching GmailMessage fields.
    """
    client = MCPGmailClient(actions_dir)
    client.write_search_results(request_id, thread_data)
