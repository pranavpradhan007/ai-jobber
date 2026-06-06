"""
Approval reply parser.

Parses Gmail reply text into structured commands and applies them to applications.
Designed to be sent as a plain-text email reply from a phone.

Supported formats (one command per line, case-insensitive):
  APP-{id} APPROVE
  APP-{id} REJECT
  APP-{id} SKIP
  APP-{id} EDIT "re-write the opening to emphasise RL research"
  APP-{id} SNOOZE
  APP-{id} SNOOZE 3          ← snooze for N days
  APPROVE ALL                ← approve every WAITING_FOR_USER_APPROVAL app
  REJECT ALL                 ← reject every WAITING_FOR_USER_APPROVAL app
  DONE
  MANUAL

Rules:
  - APP-id is the numeric application ID shown in the digest.
  - EDIT must have a quoted string with your tailoring instruction.
  - SNOOZE without a number defaults to 1 day.
  - Lines that don't match are silently ignored.
  - Commands are applied top-to-bottom; duplicates overwrite.
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from src.db.applications import get_application, update_application
from src.db.state_machine import transition

logger = logging.getLogger(__name__)

VALID_COMMANDS = {"APPROVE", "REJECT", "SKIP", "EDIT", "SNOOZE", "DONE", "MANUAL"}

# APP-3 APPROVE
# APP-3 EDIT "some quoted text"
# APP-3 SNOOZE 2
# APPROVE ALL  /  REJECT ALL
_CMD_RE = re.compile(
    r'(?i)'
    r'(?:APP-(\d+)\s+)?'                           # optional APP-N prefix
    r'(APPROVE|REJECT|SKIP|EDIT|SNOOZE|DONE|MANUAL)'  # command
    r'(?:\s+"([^"]*)")?'                           # optional "quoted instruction"
    r'(?:\s+(ALL|\d+))?'                           # optional ALL or numeric arg
)

_ALL_RE = re.compile(r'(?i)^(APPROVE|REJECT|SKIP)\s+ALL\s*$')


@dataclass
class ParsedCommand:
    app_id: Optional[int]
    command: str
    raw_line: str
    edit_instruction: Optional[str] = None   # text from EDIT "..."
    snooze_days: int = 1


@dataclass
class ApplyResult:
    app_id: Optional[int]
    command: str
    success: bool
    error: Optional[str] = None
    detail: Optional[str] = None


def parse_reply_natural(text: str, conn: sqlite3.Connection) -> list[ParsedCommand]:
    """
    Parse a free-form natural language reply using the LLM bridge.

    Falls back to parse_reply() if LLM is unavailable or times out.
    Requires conn so we can embed the current pending-app list in the prompt.
    """
    try:
        from src.llm.claude_code_bridge import should_use_claude_code, queue_llm_task  # noqa: PLC0415
        if not should_use_claude_code():
            return parse_reply(text, conn)
    except ImportError:
        return parse_reply(text, conn)

    # Build context: list of pending apps the user can act on
    pending_rows = conn.execute(
        "SELECT a.id, j.company, j.title FROM applications a "
        "JOIN jobs j ON a.job_id=j.id "
        "WHERE a.state='WAITING_FOR_USER_APPROVAL' AND a.approved_by_user=0"
    ).fetchall()
    app_list = "\n".join(
        f"  APP-{r['id']}: {r['company']} — {r['title']}"
        for r in pending_rows
    )

    prompt = f"""You are a job application assistant. The user replied to a digest email in natural language.
Convert their reply into structured commands. Output ONLY valid JSON — a list of command objects.

Pending applications the user can act on:
{app_list}

Valid commands:
  {{"app_id": N, "command": "APPROVE"}}
  {{"app_id": N, "command": "REJECT"}}
  {{"app_id": N, "command": "SNOOZE", "snooze_days": 1}}
  {{"app_id": N, "command": "EDIT", "edit_instruction": "their note"}}
  {{"app_id": null, "command": "APPROVE", "all": true}}
  {{"app_id": null, "command": "REJECT", "all": true}}

Rules:
- Match the user's intent to specific APP-IDs using company/title names if mentioned.
- "apply to X" or "go ahead with X" or "yes to X" = APPROVE for that company.
- "skip X" or "not X" or "don't apply to X" = REJECT.
- "remind me tomorrow" or "later" = SNOOZE.
- "approve all" or "all good" or "apply to everything" = APPROVE ALL.
- If the message is a question or general comment with no actionable command, return [].
- Never invent app_ids not in the list above.

User reply:
\"\"\"
{text}
\"\"\"

Output only the JSON array, nothing else."""

    try:
        raw = queue_llm_task("parse_reply", prompt, timeout_seconds=60)
        if not raw:
            return parse_reply(text, conn)
        # Extract JSON array from response
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            return parse_reply(text, conn)
        parsed = json.loads(m.group(0))
        commands = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            cmd_str = (item.get("command") or "").upper()
            if cmd_str not in VALID_COMMANDS:
                continue
            if item.get("all"):
                rows = conn.execute(
                    "SELECT id FROM applications WHERE state='WAITING_FOR_USER_APPROVAL'"
                ).fetchall()
                for row in rows:
                    commands.append(ParsedCommand(
                        app_id=row[0], command=cmd_str, raw_line=f"{cmd_str} ALL (NL)",
                    ))
            elif item.get("app_id"):
                commands.append(ParsedCommand(
                    app_id=int(item["app_id"]),
                    command=cmd_str,
                    raw_line=f"NL: {text[:60]}",
                    edit_instruction=item.get("edit_instruction"),
                    snooze_days=int(item.get("snooze_days") or 1),
                ))
        return commands if commands else parse_reply(text, conn)
    except Exception as exc:
        logger.warning("NL reply parse failed (%s) — falling back to regex parser", exc)
        return parse_reply(text, conn)


def parse_reply(text: str, conn: sqlite3.Connection = None) -> list[ParsedCommand]:
    """Parse a reply email body into a list of ParsedCommands.

    If conn is supplied, APPROVE ALL / REJECT ALL expand into one command per
    WAITING_FOR_USER_APPROVAL application.
    """
    commands = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(">"):   # skip quoted-reply lines
            continue

        # Handle APPROVE ALL / REJECT ALL / SKIP ALL
        m_all = _ALL_RE.match(line)
        if m_all:
            cmd = m_all.group(1).upper()
            if conn is not None:
                rows = conn.execute(
                    "SELECT id FROM applications WHERE state='WAITING_FOR_USER_APPROVAL'"
                ).fetchall()
                for row in rows:
                    commands.append(ParsedCommand(
                        app_id=row[0], command=cmd, raw_line=line,
                    ))
            else:
                # Without a conn we can't expand — store as a sentinel (app_id=None)
                commands.append(ParsedCommand(app_id=None, command=cmd, raw_line=line))
            continue

        m = _CMD_RE.search(line)
        if not m:
            continue
        app_id_str, cmd, quoted, arg = m.group(1), m.group(2), m.group(3), m.group(4)
        cmd = cmd.upper()
        # Skip "APPROVE ALL" matched by _CMD_RE (already handled above)
        if arg and arg.upper() == "ALL":
            continue
        commands.append(ParsedCommand(
            app_id=int(app_id_str) if app_id_str else None,
            command=cmd,
            raw_line=line,
            edit_instruction=quoted.strip() if quoted else None,
            snooze_days=int(arg) if arg and arg.isdigit() else 1,
        ))
    return commands


def apply_commands(
    conn: sqlite3.Connection,
    commands: list[ParsedCommand],
) -> list[ApplyResult]:
    """
    Apply parsed commands to the database.
    Returns a list of ApplyResult for each command.
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
    # Digest-level commands need no app_id
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
        if app.state != "WAITING_FOR_USER_APPROVAL":
            raise ValueError(
                f"APPROVE requires WAITING_FOR_USER_APPROVAL, got {app.state}"
            )
        update_application(conn, cmd.app_id, approved_by_user=1)
        conn.execute(
            """
            UPDATE approvals SET status='approved', resolved_at=datetime('now'),
            resolved_by='user', command='APPROVE'
            WHERE application_id=? AND approval_type='submit' AND status='pending'
            """,
            (cmd.app_id,),
        )
        conn.commit()
        logger.info("APPROVE app_id=%d — approved_by_user set", cmd.app_id)
        return ApplyResult(app_id=cmd.app_id, command="APPROVE", success=True,
                           detail="approved_by_user=1; run overnight to submit")

    elif cmd.command in ("REJECT", "SKIP"):
        transition(conn, cmd.app_id, "SKIPPED",
                   reason=f"user command: {cmd.command}", actor="user")
        return ApplyResult(app_id=cmd.app_id, command=cmd.command, success=True)

    elif cmd.command == "EDIT":
        # Store instruction if provided (bare EDIT re-queues with no extra instruction)
        if cmd.edit_instruction:
            update_application(conn, cmd.app_id, edit_instruction=cmd.edit_instruction)
        reason = (
            f'user EDIT: "{cmd.edit_instruction[:60]}"'
            if cmd.edit_instruction
            else "user requested re-tailor"
        )
        transition(conn, cmd.app_id, "TAILORING", reason=reason, actor="user")
        logger.info(
            "EDIT app_id=%d instruction=%r — re-queued for tailoring",
            cmd.app_id, cmd.edit_instruction,
        )
        detail = f"re-tailoring with: {cmd.edit_instruction!r}" if cmd.edit_instruction else "re-tailoring"
        return ApplyResult(app_id=cmd.app_id, command="EDIT", success=True, detail=detail)

    elif cmd.command == "SNOOZE":
        transition(conn, cmd.app_id, "SNOOZED",
                   reason=f"user snoozed for {cmd.snooze_days} day(s)",
                   actor="user")
        logger.info("SNOOZE app_id=%d for %d day(s)", cmd.app_id, cmd.snooze_days)
        return ApplyResult(app_id=cmd.app_id, command="SNOOZE", success=True,
                           detail=f"snoozed {cmd.snooze_days} day(s)")

    elif cmd.command in ("DONE", "MANUAL"):
        logger.info("app-level %s for app_id=%d", cmd.command, cmd.app_id)
        return ApplyResult(app_id=cmd.app_id, command=cmd.command, success=True)

    return ApplyResult(app_id=cmd.app_id, command=cmd.command, success=False,
                       error=f"unknown command: {cmd.command}")
