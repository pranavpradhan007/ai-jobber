"""
CAPTCHA/MFA email notification + reply watcher.

When the browser encounters a CAPTCHA or MFA challenge during portal submit:
  1. Send a warning email to the user with the portal URL.
  2. Poll Gmail for a reply saying "captcha done" / "captcha completed" / "go ahead".
  3. Return True when the user confirms, False if poll times out.

The user:
  - Receives email: "⚠️ CAPTCHA needed — Acme Corp Senior Engineer [APP-42]"
  - Opens the portal URL on mobile, completes the CAPTCHA.
  - Replies to the email: "captcha completed go ahead"

Email send path: MCPGmailClient.send_email() — direct SMTP if GMAIL_APP_PASSWORD set,
else queues create_draft manifest for Claude Code.

Reply poll path: writes search manifest to gmail_actions/pending/;
Claude Code executes MCP search_threads + get_thread + writes result file;
this module reads the result.
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CAPTCHA_REPLY_PATTERNS = [
    r"\bcaptcha\b.*\b(done|completed?|finished?)\b",
    r"\b(done|completed?|finished?)\b.*\bcaptcha\b",
    r"\bgo ahead\b",
    r"\bcontinue\b",
    r"\bresume\b",
    r"\bmfa\b.*\b(done|completed?)\b",
    r"\ball (set|good|done)\b",
]

_RESULTS_DIR = Path("gmail_actions/results")


def send_captcha_notification(
    gmail_client,
    *,
    app_id: int,
    company: str,
    title: str,
    portal_url: str,
    challenge_type: str = "CAPTCHA",
) -> str:
    """
    Send a ⚠️ email to the user asking them to complete the CAPTCHA/MFA.

    Returns the action_id / message_id from MCPGmailClient.send_email().
    """
    user_email = os.environ.get("YOUR_EMAIL_ADDRESS", "").strip()
    if not user_email:
        logger.warning("captcha_notifier: YOUR_EMAIL_ADDRESS not set — cannot send notification")
        return ""

    tag = f"APP-{app_id}"
    subject = f"⚠️ {challenge_type} needed — {company} {title} [{tag}]"

    body = (
        f"Your job application agent hit a {challenge_type} challenge and needs your help.\n\n"
        f"Application: {title} at {company}\n"
        f"App ID: {app_id}\n"
        f"Portal URL: {portal_url}\n\n"
        f"Steps:\n"
        f"  1. Open this link on your mobile browser: {portal_url}\n"
        f"  2. Complete the {challenge_type} challenge.\n"
        f"  3. Reply to this email with:  captcha completed go ahead\n\n"
        f"The agent will resume the application as soon as it receives your reply.\n\n"
        f"-- ai-jobber [{tag}]"
    )

    try:
        action_id = gmail_client.send_email(to=user_email, subject=subject, body=body)
        logger.info(
            "captcha_notifier: sent %s notification for app_id=%d action_id=%s",
            challenge_type, app_id, action_id,
        )
        return action_id
    except Exception as exc:
        logger.error("captcha_notifier: send_email failed: %s", exc)
        return ""


def poll_for_captcha_reply(
    app_id: int,
    gmail_client,
    *,
    poll_interval: int = 60,
    max_wait: int = 3600,
) -> bool:
    """
    Poll Gmail for a reply confirming CAPTCHA completion.

    Writes a search manifest, then reads results written by Claude Code.
    Returns True when user reply matches a known completion pattern.
    Returns False after max_wait seconds.

    poll_interval: seconds between checks (default 60)
    max_wait: total seconds to wait before giving up (default 3600 = 1 hour)
    """
    tag = f"APP-{app_id}"
    request_id = f"captcha_reply_{app_id}"
    result_path = _RESULTS_DIR / f"{request_id}.json"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Queue the search manifest
    _queue_captcha_reply_search(gmail_client, app_id, tag, request_id)

    logger.info(
        "captcha_notifier: waiting up to %ds for user reply to [%s]", max_wait, tag
    )

    elapsed = 0
    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        # Re-queue search in case Claude Code missed the first manifest
        if elapsed % 300 == 0:
            _queue_captcha_reply_search(gmail_client, app_id, tag, request_id)

        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                reply_text = data.get("reply_text", "").lower()
                if _is_completion_reply(reply_text):
                    logger.info(
                        "captcha_notifier: user confirmed CAPTCHA for app_id=%d", app_id
                    )
                    result_path.unlink(missing_ok=True)
                    return True
                else:
                    logger.debug(
                        "captcha_notifier: reply found but not a completion: %r", reply_text[:80]
                    )
            except Exception as exc:
                logger.warning("captcha_notifier: could not read result file: %s", exc)

        logger.debug(
            "captcha_notifier: still waiting for app_id=%d (%ds elapsed)", app_id, elapsed
        )

    logger.warning(
        "captcha_notifier: timed out after %ds waiting for app_id=%d", max_wait, app_id
    )
    return False


def write_captcha_reply(app_id: int, reply_text: str) -> None:
    """
    Called by Claude Code after reading the reply thread.
    Writes the reply so poll_for_captcha_reply() can detect completion.
    """
    request_id = f"captcha_reply_{app_id}"
    result_path = _RESULTS_DIR / f"{request_id}.json"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({
            "app_id":     app_id,
            "reply_text": reply_text,
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("captcha_notifier: wrote reply for app_id=%d", app_id)


def _is_completion_reply(text: str) -> bool:
    for pattern in _CAPTCHA_REPLY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _queue_captcha_reply_search(
    gmail_client,
    app_id: int,
    tag: str,
    request_id: str,
) -> None:
    """Write a pending search manifest so Claude Code can check for the reply."""
    pending_dir = Path("gmail_actions/pending")
    pending_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = pending_dir / f"{request_id}.json"

    if manifest_path.exists():
        return  # already queued

    manifest = {
        "action":      "search_captcha_reply",
        "request_id":  request_id,
        "app_id":      app_id,
        "query":       f'subject:"{tag}" in:inbox',
        "status":      "pending",
        "instruction": (
            f"Search Gmail for replies to the CAPTCHA notification for [{tag}]. "
            f"Query: subject:\"{tag}\" in:inbox. "
            f"For each thread found, get the most recent reply from the user "
            f"(not the original notification). Extract the plain-text body. "
            f"Then call: "
            f"from src.browser.captcha_notifier import write_captcha_reply; "
            f"write_captcha_reply({app_id}, reply_text)"
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("captcha_notifier: queued reply search for app_id=%d", app_id)
