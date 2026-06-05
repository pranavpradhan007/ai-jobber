"""
Unit tests for export-csv (v_data_csv and v_applied_csv views).
"""
import csv
import os
import pytest

from src.db.jobs import create_job
from src.db.applications import create_application, update_application
from src.db.state_machine import transition
from src.cli import _export_view


def _seed_submitted_app(conn):
    job = create_job(
        conn, source="test",
        url="https://csv.test/job/1",
        company="Acme Corp",
        title="Engineer",
        location="Remote",
    )
    app = create_application(conn, job_id=job.id)
    update_application(
        conn, app.id,
        verifier_passed=1,
        auto_safe=1,
    )
    # Walk to SUBMITTED
    for (frm, to) in [
        ("DISCOVERED", "SCORED"),
        ("SCORED", "TAILORING"),
        ("TAILORING", "RESUME_VERIFIED"),
        ("RESUME_VERIFIED", "PACKAGING"),
        ("PACKAGING", "READY_TO_SUBMIT"),
        ("READY_TO_SUBMIT", "SUBMITTING"),
        ("SUBMITTING", "SUBMITTED"),
        ("SUBMITTED", "MONITORING"),
    ]:
        transition(conn, app.id, to, reason="test")
    update_application(conn, app.id, receipt="RCPT-001", submitted_at="2024-01-01")
    return app


@pytest.mark.unit
def test_v_data_csv_includes_all_applications(tmp_db, tmp_path):
    """v_data_csv must include every application regardless of state."""
    job = create_job(tmp_db, source="test", url="https://csv.test/job/2",
                     company="Beta Inc", title="Analyst")
    create_application(tmp_db, job_id=job.id)  # stays DISCOVERED

    out = str(tmp_path / "data.csv")
    count = _export_view(tmp_db, "v_data_csv", out)
    assert count >= 1
    assert os.path.exists(out)

    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) >= 1
    assert "company" in rows[0]
    assert "state" in rows[0]


@pytest.mark.unit
def test_v_applied_csv_only_submitted(tmp_db, tmp_path):
    """v_applied_csv must only contain submitted / monitoring applications."""
    _seed_submitted_app(tmp_db)

    # Also add a DISCOVERED app — must NOT appear in applied CSV
    job2 = create_job(tmp_db, source="test", url="https://csv.test/job/3",
                      company="Gamma Ltd", title="Dev")
    create_application(tmp_db, job_id=job2.id)

    out = str(tmp_path / "applied.csv")
    _export_view(tmp_db, "v_applied_csv", out)

    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    states = {r["state"] for r in rows}
    assert "DISCOVERED" not in states
    assert states.issubset(
        {"SUBMITTED", "MONITORING", "INTERVIEW_SCHEDULED", "OA_RECEIVED"}
    )


@pytest.mark.unit
def test_export_empty_db_produces_header_only_csv(tmp_db, tmp_path):
    """An empty DB must still produce a valid CSV with headers."""
    out = str(tmp_path / "empty.csv")
    count = _export_view(tmp_db, "v_applied_csv", out)
    assert count == 0
    assert os.path.exists(out)
    with open(out, newline="", encoding="utf-8") as fh:
        content = fh.read()
    # Must have at least one line (the header)
    assert len(content.strip()) > 0
