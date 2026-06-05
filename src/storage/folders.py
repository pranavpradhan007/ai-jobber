"""
Per-application artifact folder management.

Layout: applications/YYYY-MM/{company}_{title}_{app_id}/
All path separators are normalised to forward slashes in stored paths.
"""
from __future__ import annotations
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
APPLICATIONS_BASE = os.path.join(_REPO_ROOT, "applications")


def create_application_folder(
    conn: sqlite3.Connection,
    app_id: int,
) -> str:
    """
    Create the artifact folder for an application and persist the path.
    Returns the absolute folder path.
    Idempotent: if folder already exists, just returns the existing path.
    """
    cur = conn.execute(
        """
        SELECT a.id, a.folder_path, j.company, j.title
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        WHERE a.id = ?
        """,
        (app_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"Application {app_id} not found")

    # Already has a folder
    if row["folder_path"] and os.path.isdir(row["folder_path"]):
        return row["folder_path"]

    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    company = _slug(row["company"] or "unknown")
    title   = _slug(row["title"]   or "position")
    folder_name = f"{company}_{title}_{app_id}"
    folder_path = os.path.join(APPLICATIONS_BASE, month_prefix, folder_name)

    os.makedirs(folder_path, exist_ok=True)

    # Normalise to forward slashes for cross-platform consistency in DB
    stored_path = folder_path.replace("\\", "/")
    conn.execute(
        "UPDATE applications SET folder_path = ? WHERE id = ?",
        (stored_path, app_id),
    )
    conn.commit()

    logger.info("created folder app_id=%d path=%s", app_id, folder_path)
    return folder_path


def artifact_path(folder_path: str, filename: str) -> str:
    """Return the absolute path for a named artifact inside a folder."""
    return os.path.join(folder_path.replace("/", os.sep), filename)


def _slug(text: str) -> str:
    """Convert text to a safe folder-name component (max 30 chars)."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    return text[:30]
