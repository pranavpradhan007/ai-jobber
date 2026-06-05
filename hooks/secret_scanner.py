#!/usr/bin/env python3
"""
secret_scanner.py — PreToolUse hook
Blocks any file-write that contains patterns resembling plaintext credentials.
Exit 0 = allow. Exit 2 = block (Claude Code interprets 2 as "blocked by hook").
"""
import sys
import re
import os
import json

# Patterns that indicate an assigned credential value.
# Each pattern: key = suspicious label, value = regex that matches the full assignment line.
PATTERNS = [
    # Generic key=value assignments
    re.compile(
        r'(?i)(password|passwd|secret|token|api[_\-]?key|oauth|private[_\-]?key'
        r'|access[_\-]?key|client[_\-]?secret|bearer|auth[_\-]?token'
        r'|refresh[_\-]?token|credentials?)\s*[=:]\s*["\']?[A-Za-z0-9+/\-_]{8,}["\']?'
    ),
    # AWS-style keys
    re.compile(r'(?i)AKIA[0-9A-Z]{16}'),
    # Generic long hex/base64 that looks like a secret (heuristic)
    re.compile(r'(?i)(secret|token|key)\s*=\s*["\'][0-9a-f]{32,}["\']'),
]

EXEMPT_EXTENSIONS = {'.md', '.txt', '.rst', '.example', '.sample'}
EXEMPT_FILENAMES  = {'.env.example', 'secret_scanner.py'}


def is_exempt(path: str) -> bool:
    basename = os.path.basename(path)
    if basename in EXEMPT_FILENAMES:
        return True
    _, ext = os.path.splitext(basename)
    return ext in EXEMPT_EXTENSIONS


def scan_content(content: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for pat in PATTERNS:
            if pat.search(line):
                hits.append(f"  line {lineno}: {line.rstrip()[:120]}")
                break
    return hits


def main():
    # The hook receives the target file path as $CLAUDE_TOOL_INPUT_FILE_PATH.
    # For Write/Edit the hook is called BEFORE the write; we scan the proposed content
    # passed via stdin (Claude Code pipes the full JSON tool input to stdin).
    tool_input_path = sys.argv[1] if len(sys.argv) > 1 else ""

    if is_exempt(tool_input_path):
        sys.exit(0)

    # Read proposed content from stdin (Claude Code sends JSON tool input)
    raw_stdin = sys.stdin.read()
    if not raw_stdin.strip():
        sys.exit(0)

    try:
        tool_input = json.loads(raw_stdin)
        # For Write tool: field is 'content'
        # For Edit tool: fields are 'new_string' (and 'old_string')
        candidates = []
        for field in ("content", "new_string"):
            if field in tool_input:
                candidates.append(tool_input[field])
    except (json.JSONDecodeError, TypeError):
        # Not JSON — scan raw stdin as plain text
        candidates = [raw_stdin]

    all_hits = []
    for text in candidates:
        if isinstance(text, str):
            all_hits.extend(scan_content(text))

    if all_hits:
        print("SECRET_SCANNER: BLOCKED — potential plaintext credential detected.", file=sys.stderr)
        print(f"File: {tool_input_path}", file=sys.stderr)
        for h in all_hits[:10]:
            print(h, file=sys.stderr)
        print(
            "\nMove credentials to .env and reference via os.environ.",
            file=sys.stderr
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
