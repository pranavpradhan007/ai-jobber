"""
Gmail client abstraction.

Three implementations:
  MockGmailClient      — deterministic in-memory mock (tests only)
  MCPGmailClient       — production: bridges to Claude Code's MCP Gmail hook
  RealGmailClient      — legacy OAuth path (kept for reference, not used)

Architecture:
  Gmail calls from Python write action manifests to disk.
  Claude Code reads them, executes via MCP Gmail tools, and writes results back.
  No OAuth credentials are stored or required.
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACTIONS_DIR = _REPO_ROOT / "gmail_actions"


@dataclass
class GmailMessage:
    thread_id: str
    message_id: str
    sender: str
    subject: str
    body: str
    snippet: str = ""


# ---------------------------------------------------------------------------
# MockGmailClient — tests
# ---------------------------------------------------------------------------

class MockGmailClient:
    """Deterministic in-memory mock. Never used in production."""

    def __init__(self):
        self.sent: list[dict] = []
        self._inbox: list[GmailMessage] = []

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Optional[list[str]] = None,
    ) -> str:
        msg_id = f"mock_msg_{len(self.sent) + 1:04d}"
        self.sent.append({
            "to": to, "subject": subject, "body": body,
            "attachments": attachments or [], "message_id": msg_id,
        })
        logger.debug("MockGmail sent to=%s subject=%s id=%s", to, subject, msg_id)
        return msg_id

    def get_inbox(self, query: str = "") -> list[GmailMessage]:
        if query:
            return [m for m in self._inbox
                    if query.lower() in m.body.lower()
                    or query.lower() in m.subject.lower()]
        return list(self._inbox)

    def preset_inbox(self, messages: list[GmailMessage]) -> None:
        self._inbox = list(messages)


# ---------------------------------------------------------------------------
# MCPGmailClient — production (Claude Code MCP bridge)
# ---------------------------------------------------------------------------

class MCPGmailClient:
    """
    Production Gmail client.

    Send path:
      Python writes a pending action to gmail_actions/pending/.
      Claude Code reads it, calls MCP create_draft, and moves it to
      gmail_actions/done/ with the draft_id attached.
      Because MCP only exposes create_draft (no direct send), digest emails
      become drafts that Claude Code surfaces to the user for one-click send.

    Receive path:
      Python writes a search-request manifest.
      Claude Code calls MCP search_threads / get_thread, writes results to
      gmail_actions/results/.
      Python then reads the result file.
    """

    def __init__(self, actions_dir: Optional[str] = None):
        self._dir = Path(actions_dir) if actions_dir else _ACTIONS_DIR
        (self._dir / "pending").mkdir(parents=True, exist_ok=True)
        (self._dir / "done").mkdir(parents=True, exist_ok=True)
        (self._dir / "results").mkdir(parents=True, exist_ok=True)

    # ── Send ─────────────────────────────────────────────────────────────────

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Optional[list[str]] = None,
    ) -> str:
        """
        Send email directly via Gmail SMTP when GMAIL_APP_PASSWORD is set.
        Falls back to queuing a create_draft manifest for Claude Code if not.

        To enable direct send:
          1. In your Google Account: Security > 2-Step Verification > App passwords
          2. Create an app password for "Mail"
          3. Add to .env:  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
        """
        app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
        sender = os.environ.get("YOUR_EMAIL_ADDRESS", "").strip()

        if app_password and sender:
            return self._smtp_send(sender, to, subject, body, app_password)

        # Fallback: queue draft manifest for Claude Code to execute via MCP
        action_id = _timestamp_id("draft")
        manifest = {
            "action":      "create_draft",
            "action_id":   action_id,
            "to":          [to],
            "subject":     subject,
            "body":        body,
            "attachments": attachments or [],
            "created_at":  _now(),
            "status":      "pending",
        }
        path = self._dir / "pending" / f"{action_id}.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        logger.warning(
            "MCPGmail: GMAIL_APP_PASSWORD not set — queued draft instead of sending. "
            "action_id=%s to=%s", action_id, to,
        )
        return action_id

    def _smtp_send(
        self,
        sender: str,
        to: str,
        subject: str,
        body: str,
        app_password: str,
    ) -> str:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = to
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, app_password)
            smtp.send_message(msg)

        msg_id = _timestamp_id("smtp")
        logger.info("MCPGmail: sent via SMTP to=%s subject=%r id=%s", to, subject[:60], msg_id)
        return msg_id

    # ── Receive ───────────────────────────────────────────────────────────────

    def get_inbox(self, query: str = "") -> list[GmailMessage]:
        """
        Write a pending 'search_threads' request and block until Claude Code
        writes the result file (timeout: 0 in this call — returns empty list
        immediately if results aren't cached yet).

        For the overnight runner flow:
          1. Python calls get_inbox(query) → writes pending request
          2. Claude Code executes the MCP search, writes results file
          3. Python calls get_inbox(query) again → reads cached results
        """
        req_id = _stable_id("search", query)
        result_path = self._dir / "results" / f"{req_id}.json"

        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                messages = [GmailMessage(**m) for m in data.get("messages", [])]
                logger.info(
                    "MCPGmail: loaded %d messages from results cache query=%r",
                    len(messages), query,
                )
                return messages
            except Exception as exc:
                logger.warning("MCPGmail: failed to read results cache: %s", exc)

        # No cached results yet — write the request for Claude Code to pick up
        manifest = {
            "action":     "search_threads",
            "request_id": req_id,
            "query":      query,
            "created_at": _now(),
            "status":     "pending",
        }
        req_path = self._dir / "pending" / f"{req_id}.json"
        if not req_path.exists():
            req_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            logger.info(
                "MCPGmail: queued search request request_id=%s query=%r",
                req_id, query,
            )
        return []

    # ── Status helpers ────────────────────────────────────────────────────────

    def pending_actions(self) -> list[dict]:
        """Return all unprocessed action manifests."""
        actions = []
        for f in (self._dir / "pending").glob("*.json"):
            try:
                actions.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return sorted(actions, key=lambda a: a.get("created_at", ""))

    def mark_done(self, action_id: str, result: dict) -> None:
        """Move an action from pending → done after MCP execution."""
        src = self._dir / "pending" / f"{action_id}.json"
        if src.exists():
            data = json.loads(src.read_text(encoding="utf-8"))
            data.update({"status": "done", "result": result,
                         "completed_at": _now()})
            dst = self._dir / "done" / f"{action_id}.json"
            dst.write_text(json.dumps(data, indent=2), encoding="utf-8")
            src.unlink()
            logger.info("MCPGmail: marked done action_id=%s", action_id)

    def write_search_results(self, request_id: str, messages: list[dict]) -> None:
        """Write search results so get_inbox() can read them next call."""
        result_path = self._dir / "results" / f"{request_id}.json"
        result_path.write_text(
            json.dumps({"request_id": request_id, "messages": messages,
                        "written_at": _now()}, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "MCPGmail: wrote %d search results for request_id=%s",
            len(messages), request_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _timestamp_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{ts}"

def _stable_id(prefix: str, key: str) -> str:
    import hashlib
    h = hashlib.md5(key.encode()).hexdigest()[:8]
    return f"{prefix}_{h}"


# ---------------------------------------------------------------------------
# RealGmailClient — legacy OAuth (not used; kept for reference)
# ---------------------------------------------------------------------------

class RealGmailClient:
    """
    OAuth-based client (google-api-python-client).
    Not used — Gmail goes through Claude Code MCP.
    Kept here in case a standalone (non-Claude-Code) path is ever needed.
    """

    def __init__(self, credentials_path: str, token_path: str):
        raise NotImplementedError(
            "RealGmailClient requires Google OAuth credentials. "
            "Use MCPGmailClient instead — Gmail is handled via Claude Code MCP."
        )

    def send_email(self, *args, **kwargs):
        raise NotImplementedError

    def get_inbox(self, *args, **kwargs):
        raise NotImplementedError
