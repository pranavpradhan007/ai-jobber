"""
Approval reply parser.
Parses Gmail reply text into structured commands and applies them to applications.

Expected format (one command per line):
  APP-{id} APPROVE
  APP-{id} REJECT
  APP-{id} SKIP
  APP-{id} EDIT
  APP-{id} SNOOZE
  DONE
  MANUAL

'DONE' and 'MANUAL' are digest-level commands (no app ID required).
"""
from __future__ import annotations
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.db.applications import get_application, update_application
from src.db.state_machine import transition

logger = logging.getLogger(__name__)

VALID_COMMANDS = {"APPROVE", "REJECT", "SKIP", "EDIT", "SNOOZE", "DONE", "MANUAL"}

_LINE_RE = re.compile(
    r"(?i)(?:APP-(\d+)\s+)?(" + "|".join(VALID_COMMANDS) + r")\b"
)


@dataclass
class ParsedCommand:
    app_id: Optional[int]
    command: str
    raw_line: str


@dataclass
class ApplyResult:
    app_id: Optional[int]
    command: str
    success: bool
    error: Optional[str] = None


def parse_reply(text: str) -> list[ParsedCommand]:
    """Parse a reply email body into a list of ParsedCommands."""
    commands = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_RE.search(line)
        if m:
            app_id_str, cmd = m.group(1), m.group(2).upper()
            app_id = int(app_id_str) if app_id_str else None
            commands.append(ParsedCommand(
                app_id=app_id,
                command=cmd,
                raw_line=line,
            ))
    return commands


def apply_commands(
    conn: sqlite3.Connection,
    commands: list[ParsedCommand],
) -> list[ApplyResult]:
    """
    Apply parsed commands to the database.
    Returns a list of ApplyResult for each command.
    No command may set approved_by_user=1 without being APPROVE.
    """
    results = []
    for cmd in commands:
        try:
            result = _apply_one(conn, cmd)
        except Exception as exc:
            logger.error(
                "failed to apply command %s app_id=%s: %s",
                cmd.command, cmd.app_id, exc,
            )
            result = ApplyResult(
                app_id=cmd.app_id, command=cmd.command,
                success=False, error=str(exc),
            )
        results.append(result)
    return results


def _apply_one(conn: sqlite3.Connection, cmd: ParsedCommand) -> ApplyResult:
    if cmd.command in ("DONE", "MANUAL") and cmd.app_id is None:
        logger.info("digest-level command: %s", cmd.command)
        return ApplyResult(app_id=None, command=cmd.command, success=True)

    if cmd.app_id is None:
        return ApplyResult(
            app_id=None, command=cmd.command, success=False,
            error="APP-id required for this command",
        )

    app = get_application(conn, cmd.app_id)

    if cmd.command == "APPROVE":
        # Only valid from WAITING_FOR_USER_APPROVAL
        if app.state != "WAITING_FOR_USER_APPROVAL":
            raise ValueError(
                f"APPROVE requires state=WAITING_FOR_USER_APPROVAL, got {app.state}"
            )
        update_application(conn, cmd.app_id, approved_by_user=1)
        # Record resolution in approvals table
        conn.execute(
            """
            UPDATE approvals SET status='approved', resolved_at=datetime('now'),
            resolved_by='user', command='APPROVE'
            WHERE application_id=? AND approval_type='submit' AND status='pending'
            """,
            (cmd.app_id,),
        )
        conn.commit()
        # The actual SUBMITTING transition happens in the runner (needs verifier check)
        logger.info("APPROVE app_id=%d — approved_by_user set", cmd.app_id)

    elif cmd.command in ("REJECT", "SKIP"):
        to_state = "SKIPPED"
        transition(conn, cmd.app_id, to_state,
                   reason=f"user command: {cmd.command}",
                   actor="user")

    elif cmd.command == "EDIT":
        transition(conn, cmd.app_id, "TAILORING",
                   reason="user requested re-tailor",
                   actor="user")

    elif cmd.command == "SNOOZE":
        transition(conn, cmd.app_id, "SNOOZED",
                   reason="user snoozed",
                   actor="user")

    elif cmd.command in ("DONE", "MANUAL"):
        logger.info("app-level %s for app_id=%d", cmd.command, cmd.app_id)

    return ApplyResult(app_id=cmd.app_id, command=cmd.command, success=True)
