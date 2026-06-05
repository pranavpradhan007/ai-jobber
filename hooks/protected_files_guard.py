#!/usr/bin/env python3
"""
protected_files_guard.py — PreToolUse hook
Blocks any automated write to files that only the human may edit.
Exit 0 = allow. Exit 2 = block.
"""
import sys
import os

# Paths (relative to repo root or exact basename) that are protected.
PROTECTED_PATHS = {
    "verified_facts.yaml",
    os.path.join("knowledge_base", "profile", "verified_facts.yaml"),
}

# Also protect by basename for any location
PROTECTED_BASENAMES = {
    "verified_facts.yaml",
}


def is_protected(path: str) -> bool:
    if not path:
        return False
    # Normalise separators
    norm = path.replace("\\", "/")
    basename = os.path.basename(norm)

    if basename in PROTECTED_BASENAMES:
        return True

    for p in PROTECTED_PATHS:
        if norm.endswith(p.replace("\\", "/")):
            return True

    return False


def main():
    target_path = sys.argv[1] if len(sys.argv) > 1 else ""

    if is_protected(target_path):
        print(
            f"PROTECTED_FILES_GUARD: BLOCKED — '{target_path}' is a human-only file.\n"
            "New facts must go to pending/pending_user_verification.md.\n"
            "Only the human may edit verified_facts.yaml.",
            file=sys.stderr
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
