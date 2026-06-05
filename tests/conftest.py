"""
Shared pytest fixtures available to all test suites.
Fixtures that require live services are marked live=True and excluded from CI.
"""
import os
import sqlite3
import tempfile
import pytest

from src.db.connection import get_connection, init_db

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="session")
def schema_path():
    return os.path.join(REPO_ROOT, "schema.sql")


@pytest.fixture
def tmp_db(schema_path, tmp_path):
    """Return a fresh in-memory or temp-file SQLite connection seeded from schema.sql."""
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path, schema_path)
    yield conn
    conn.close()


@pytest.fixture
def seeded_db(tmp_db):
    """tmp_db with source_bank pre-seeded from tests/fixtures/sample_source_bank.yaml."""
    from src.storage.bank_loader import seed_from_yaml
    seed_from_yaml(tmp_db, os.path.join(FIXTURES_DIR, "sample_source_bank.yaml"))
    return tmp_db
