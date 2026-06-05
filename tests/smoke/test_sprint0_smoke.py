"""
Sprint 0 smoke tests.
Verify: test runner is live, schema applies cleanly, hooks fire correctly.
"""
import os
import sys
import subprocess
import sqlite3
import tempfile
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ── 1. Test runner is alive ──────────────────────────────────────────────────

@pytest.mark.smoke
def test_runner_alive():
    """Trivial: pytest collected and executed this test."""
    assert True


# ── 2. Schema applies to a blank DB ─────────────────────────────────────────

@pytest.mark.smoke
def test_schema_applies(tmp_path):
    """schema.sql must execute without error on a fresh database."""
    from src.db.connection import init_db
    schema_path = os.path.join(REPO_ROOT, "schema.sql")
    db_path = str(tmp_path / "smoke_test.db")
    conn = init_db(db_path, schema_path)

    # Verify expected tables exist
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cur.fetchall()}
    expected = {
        "jobs", "applications", "state_transitions",
        "work_queue", "source_bank", "claims",
        "approvals", "monitoring_events", "audit_log",
    }
    missing = expected - tables
    assert not missing, f"Missing tables after schema apply: {missing}"
    conn.close()


@pytest.mark.smoke
def test_views_exist(tmp_path):
    """v_data_csv and v_applied_csv views must be present."""
    from src.db.connection import init_db
    schema_path = os.path.join(REPO_ROOT, "schema.sql")
    db_path = str(tmp_path / "smoke_views.db")
    conn = init_db(db_path, schema_path)

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    )
    views = {row[0] for row in cur.fetchall()}
    assert "v_data_csv" in views
    assert "v_applied_csv" in views
    conn.close()


@pytest.mark.smoke
def test_schema_idempotent(tmp_path):
    """Applying schema.sql twice must not raise (IF NOT EXISTS guards)."""
    from src.db.connection import init_db
    schema_path = os.path.join(REPO_ROOT, "schema.sql")
    db_path = str(tmp_path / "smoke_idempotent.db")
    conn = init_db(db_path, schema_path)
    conn.close()
    # Second apply
    conn2 = init_db(db_path, schema_path)
    conn2.close()


# ── 3. secret_scanner hook blocks planted credential ─────────────────────────

@pytest.mark.smoke
def test_secret_scanner_blocks_plaintext_password(tmp_path):
    """
    secret_scanner.py must exit 2 when given content with a plaintext password.
    """
    scanner = os.path.join(REPO_ROOT, "hooks", "secret_scanner.py")
    target = str(tmp_path / "bad_config.py")

    malicious_content = 'DB_PASSWORD = "SuperSecret123!"\n'
    import json
    tool_input = json.dumps({"content": malicious_content})

    result = subprocess.run(
        [sys.executable, scanner, target],
        input=tool_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (blocked), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )
    assert "BLOCKED" in result.stderr or "SECRET_SCANNER" in result.stderr


@pytest.mark.smoke
def test_secret_scanner_allows_clean_content(tmp_path):
    """secret_scanner.py must exit 0 for benign content."""
    scanner = os.path.join(REPO_ROOT, "hooks", "secret_scanner.py")
    target = str(tmp_path / "clean.py")

    clean_content = 'DB_HOST = "localhost"\nDB_PORT = 5432\n'
    import json
    tool_input = json.dumps({"content": clean_content})

    result = subprocess.run(
        [sys.executable, scanner, target],
        input=tool_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (allowed), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )


@pytest.mark.smoke
def test_secret_scanner_allows_example_files(tmp_path):
    """.env.example is exempt from scanning."""
    scanner = os.path.join(REPO_ROOT, "hooks", "secret_scanner.py")
    target = str(tmp_path / ".env.example")

    content_with_placeholder = 'GMAIL_TOKEN=your_token_here\n'
    import json
    tool_input = json.dumps({"content": content_with_placeholder})

    result = subprocess.run(
        [sys.executable, scanner, target],
        input=tool_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


# ── 4. protected_files_guard blocks verified_facts.yaml ─────────────────────

@pytest.mark.smoke
def test_protected_files_guard_blocks_verified_facts():
    """protected_files_guard.py must exit 2 for verified_facts.yaml."""
    guard = os.path.join(REPO_ROOT, "hooks", "protected_files_guard.py")
    target = os.path.join(REPO_ROOT, "knowledge_base", "profile", "verified_facts.yaml")

    result = subprocess.run(
        [sys.executable, guard, target],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (blocked), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )
    assert "BLOCKED" in result.stderr or "PROTECTED" in result.stderr


@pytest.mark.smoke
def test_protected_files_guard_allows_other_files(tmp_path):
    """Other files must not be blocked."""
    guard = os.path.join(REPO_ROOT, "hooks", "protected_files_guard.py")
    target = str(tmp_path / "some_file.yaml")

    result = subprocess.run(
        [sys.executable, guard, target],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


# ── 5. CLAUDE.md and rules files are present ────────────────────────────────

@pytest.mark.smoke
def test_required_files_present():
    required = [
        "CLAUDE.md",
        "schema.sql",
        "PHASE_0_REFINED_PLAN.md",
        os.path.join("rules", "truthfulness.md"),
        os.path.join("rules", "safety.md"),
        os.path.join("rules", "versions.md"),
        os.path.join("rules", "approvals.md"),
        os.path.join("hooks", "secret_scanner.py"),
        os.path.join("hooks", "protected_files_guard.py"),
        os.path.join(".claude", "settings.json"),
        "Makefile",
        "pyproject.toml",
    ]
    missing = [f for f in required if not os.path.exists(os.path.join(REPO_ROOT, f))]
    assert not missing, f"Required Sprint 0 files missing: {missing}"
