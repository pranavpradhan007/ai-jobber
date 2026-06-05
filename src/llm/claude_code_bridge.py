"""
Claude Code LLM bridge.

When USE_CLAUDE_CODE_FOR_LLM=true, the Python agent queues LLM tasks
as action manifests for Claude Code to execute via MCP, avoiding
plaintext API keys in the Python environment.

Each task writes:
  1. Request manifest to gmail_actions/pending/
  2. Claude Code executes via MCP
  3. Result written to gmail_actions/results/
  4. Python reads result, cleans up manifest

No ANTHROPIC_API_KEY needed. No secrets in .env.
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_ACTIONS_DIR = Path("gmail_actions")


def should_use_claude_code() -> bool:
    """Check if Claude Code should handle LLM tasks."""
    return os.environ.get("USE_CLAUDE_CODE_FOR_LLM", "").lower() == "true"


def queue_llm_task(
    task_type: str,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    timeout_seconds: int = 30,
) -> Optional[str]:
    """
    Queue an LLM task for Claude Code to execute.
    Returns the task_id if successful, else None.

    task_type: "rephrase" | "score" | "extract" | "classify"
    prompt: full prompt text
    model: which Claude model to use
    timeout_seconds: how long to wait for result

    Returns raw response text (parse as needed by caller).
    """
    task_id = f"{task_type}_{uuid4().hex[:8]}_{_ts()}"
    pending_dir = _ACTIONS_DIR / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "action":      "call_llm",
        "task_type":   task_type,
        "task_id":     task_id,
        "model":       model,
        "prompt":      prompt,
        "created_at":  datetime.now(timezone.utc).isoformat() + "Z",
        "status":      "pending",
        "instruction": (
            f"Use the {model} model to process the prompt. "
            "Return the raw LLM response as-is. "
            f"Then call: from src.llm.claude_code_bridge import write_llm_result; "
            f"write_llm_result('{task_id}', response_text)"
        ),
    }

    req_path = pending_dir / f"{task_id}.json"
    req_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Queued LLM task %s (%s)", task_id, task_type)

    # Wait for result (with timeout)
    result = _wait_for_result(task_id, timeout_seconds)
    if result:
        req_path.unlink()  # consume manifest
    return result


def write_llm_result(task_id: str, response_text: str) -> None:
    """Called by Claude Code after executing an LLM task."""
    results_dir = _ACTIONS_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{task_id}.json"
    path.write_text(
        json.dumps({
            "task_id":     task_id,
            "response":    response_text,
            "written_at":  datetime.now(timezone.utc).isoformat() + "Z",
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("LLM result written: %s", task_id)


def _wait_for_result(task_id: str, timeout_seconds: int) -> Optional[str]:
    """Poll for LLM result file."""
    results_dir = _ACTIONS_DIR / "results"
    result_path = results_dir / f"{task_id}.json"

    start = time.time()
    while time.time() - start < timeout_seconds:
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                return data.get("response", "")
            except Exception as exc:
                logger.warning("Failed to read result %s: %s", task_id, exc)
                return None
        time.sleep(0.5)

    logger.warning("LLM task %s timed out after %d seconds", task_id, timeout_seconds)
    return None


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
