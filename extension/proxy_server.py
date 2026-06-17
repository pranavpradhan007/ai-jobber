"""
JobberAI local proxy — uses Claude Code CLI instead of direct Anthropic API calls.

Calls `claude -p "prompt"` so your Claude Code subscription covers all usage.
No console.anthropic.com credits needed. No API key needed.

Usage:
    python extension/proxy_server.py
    (or double-click start_proxy.bat)
"""
from __future__ import annotations
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

PORT = int(os.environ.get("JOBBERAI_PROXY_PORT", "3747"))

# Find the claude CLI
def _find_claude() -> str:
    for candidate in ["claude", r"C:\Users\hp\AppData\Roaming\npm\claude.cmd",
                      r"C:\Program Files\nodejs\claude.cmd"]:
        try:
            r = subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "claude"  # fall back, let it fail with a clear error


CLAUDE_BIN = _find_claude()


def call_claude_cli(system_prompt: str, user_prompt: str, max_tokens: int = 1500) -> str:
    """Pipe prompt to `claude -p` via a temp file to avoid Windows 8191-char CLI limit."""
    combined = f"{system_prompt}\n\n---\n\n{user_prompt}"

    tmp_path = None
    try:
        # Write prompt to temp file — avoids "command line too long" on Windows
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(combined)
            tmp_path = f.name

        with open(tmp_path, "r", encoding="utf-8") as stdin_f:
            result = subprocess.run(
                [CLAUDE_BIN, "--model", "claude-haiku-4-5-20251001", "-p"],
                stdin=stdin_f,
                capture_output=True,
                text=True,
                timeout=90,
                encoding="utf-8",
                errors="replace",
            )

        if result.returncode != 0:
            err = (result.stderr.strip() or result.stdout.strip())[:400]
            raise RuntimeError(f"claude CLI error (exit {result.returncode}): {err}")
        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        raise RuntimeError("claude CLI timed out after 90s")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def anthropic_response(text: str) -> dict:
    """Wrap plain text in the shape the extension's parseBatchResponse expects."""
    return {
        "id": "proxy-cli",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[proxy] {fmt % args}")

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "cli": CLAUDE_BIN}).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/gmail/count-receipts":
            # Use claude CLI (which has Gmail MCP) to count application receipt emails.
            # Narrow query: inbox only, specific receipt subjects, exclude newsletters/updates.
            system = "You have access to Gmail via MCP. Search and count emails only. Return JSON only."
            prompt = (
                "Use the Gmail MCP tool to count genuine job application receipt emails in my inbox. "
                "Search query: in:inbox (subject:\"application received\" OR subject:\"thank you for applying\" OR "
                "subject:\"we received your application\" OR subject:\"application submitted\" OR "
                "subject:\"thanks for applying\") "
                "-subject:newsletter -subject:update -subject:unsubscribe "
                "newer_than:90d\n\n"
                "Count ONLY emails that confirm a specific job application was received (not promotional "
                "emails, job alerts, or newsletters). "
                "Return ONLY this JSON with no other text: {\"count\": <number>}"
            )
            try:
                text = call_claude_cli(system, prompt, 200)
                import re
                m = re.search(r'\{[^}]*"count"\s*:\s*(\d+)[^}]*\}', text)
                count = int(m.group(1)) if m else 0
                body = json.dumps({"count": count, "raw": text[:200]}).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass  # client disconnected before response — harmless
            except Exception as exc:
                self._error(500, str(exc))

        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/v1/messages"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)

        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            self._error(400, f"Invalid JSON: {e}")
            return

        system_prompt = body.get("system", "")
        messages = body.get("messages", [])
        user_prompt = messages[-1]["content"] if messages else ""
        max_tokens = body.get("max_tokens", 1500)

        try:
            text = call_claude_cli(system_prompt, user_prompt, max_tokens)
            resp_body = json.dumps(anthropic_response(text)).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client already disconnected
        except RuntimeError as exc:
            self._error(502, str(exc))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-api-key, anthropic-version")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _error(self, code: int, msg: str):
        body = json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode()
        try:
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client already disconnected — harmless


if __name__ == "__main__":
    # Quick sanity check
    print(f"Checking claude CLI at: {CLAUDE_BIN}")
    try:
        ver = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=5)
        if ver.returncode != 0:
            print(f"ERROR: claude CLI not working. Make sure Claude Code is installed.")
            sys.exit(1)
        print(f"Claude CLI: {ver.stdout.strip()}")
    except FileNotFoundError:
        print("ERROR: 'claude' command not found. Install Claude Code CLI first.")
        sys.exit(1)

    print(f"\nJobberAI proxy running on http://localhost:{PORT}")
    print("Using Claude Code CLI — no API credits needed.")
    print("Keep this window open while using the extension.\n")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")
