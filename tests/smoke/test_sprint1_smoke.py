"""
Sprint 1 smoke test — end-to-end happy path:
  create job → create application → walk DISCOVERED…READY_TO_SUBMIT
  → export both CSVs → confirm folder exists.
"""
import os
import csv
import pytest

from src.db.connection import init_db
from src.db.jobs import create_job
from src.db.applications import create_application, update_application
from src.db.state_machine import transition
from src.storage.folders import create_application_folder
from src.cli import _export_view

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.mark.smoke
def test_sprint1_full_pipeline(tmp_path):
    """
    Full Sprint 1 pipeline smoke:
      job → app → DISCOVERED→SCORED→TAILORING→RESUME_VERIFIED→PACKAGING→READY_TO_SUBMIT
      → folder created → both CSVs exported.
    """
    schema_path = os.path.join(REPO_ROOT, "schema.sql")
    db_path = str(tmp_path / "sprint1.db")
    conn = init_db(db_path, schema_path)

    # Override applications base to tmp_path so we don't pollute the real folder
    import src.storage.folders as folder_mod
    orig_base = folder_mod.APPLICATIONS_BASE
    folder_mod.APPLICATIONS_BASE = str(tmp_path / "applications")
    try:
        _run_pipeline(conn, tmp_path)
    finally:
        folder_mod.APPLICATIONS_BASE = orig_base

    conn.close()


def _run_pipeline(conn, tmp_path):
    # 1. Create job
    job = create_job(
        conn,
        source="manual",
        url="https://acme.com/jobs/senior-engineer",
        company="Acme Corp",
        title="Senior Software Engineer",
        location="Remote",
        remote=1,
        clean_jd="We are looking for a senior engineer with Python experience.",
        platform="workday",
    )
    assert job.id is not None

    # 2. Create application
    app = create_application(conn, job_id=job.id)
    assert app.state == "DISCOVERED"

    # 3. Walk the state machine
    for to_state in ["SCORED", "TAILORING", "RESUME_VERIFIED", "PACKAGING", "READY_TO_SUBMIT"]:
        transition(conn, app.id, to_state, reason=f"smoke test → {to_state}")

    # Verify current state
    from src.db.applications import get_application
    app = get_application(conn, app.id)
    assert app.state == "READY_TO_SUBMIT"

    # 4. Create folder
    folder = create_application_folder(conn, app.id)
    assert os.path.isdir(folder), f"Expected folder at {folder}"

    # Verify folder_path persisted to DB
    app = get_application(conn, app.id)
    assert app.folder_path is not None
    assert "acme" in app.folder_path.lower() or str(app.id) in app.folder_path

    # 5. Export CSVs
    data_csv = str(tmp_path / "data.csv")
    applied_csv = str(tmp_path / "applied.csv")
    _export_view(conn, "v_data_csv", data_csv)
    _export_view(conn, "v_applied_csv", applied_csv)

    assert os.path.exists(data_csv)
    assert os.path.exists(applied_csv)

    with open(data_csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme Corp"
    assert rows[0]["state"] == "READY_TO_SUBMIT"

    # applied_csv should be empty (not yet submitted)
    with open(applied_csv, newline="", encoding="utf-8") as fh:
        applied_rows = list(csv.DictReader(fh))
    assert len(applied_rows) == 0

    # 6. Verify all transitions logged
    from src.db.state_machine import get_transitions
    transitions = get_transitions(conn, app.id)
    assert len(transitions) == 5
    assert transitions[-1]["to_state"] == "READY_TO_SUBMIT"
