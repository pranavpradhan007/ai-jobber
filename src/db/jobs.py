"""
Typed accessor layer for the `jobs` table.
"""
from __future__ import annotations
import hashlib
import sqlite3
import logging
from typing import Optional

from .models import Job

logger = logging.getLogger(__name__)


def create_job(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str,
    company: Optional[str] = None,
    title: Optional[str] = None,
    location: Optional[str] = None,
    remote: int = 0,
    raw_jd: Optional[str] = None,
    clean_jd: Optional[str] = None,
    has_screener: int = 0,
    login_required: int = 0,
    platform: Optional[str] = None,
    email_apply_addr: Optional[str] = None,
    easy_apply: int = 0,
) -> Job:
    """Insert a new job row. Returns the created Job. Raises on duplicate URL."""
    jd_hash = _hash_jd(clean_jd) if clean_jd else None

    cur = conn.execute(
        """
        INSERT INTO jobs
            (source, url, jd_hash, company, title, location, remote,
             raw_jd, clean_jd, has_screener, login_required, platform,
             email_apply_addr, easy_apply)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (source, url, jd_hash, company, title, location, remote,
         raw_jd, clean_jd, has_screener, login_required, platform,
         email_apply_addr, easy_apply),
    )
    conn.commit()
    job_id = cur.lastrowid
    logger.info("created job id=%d url=%s", job_id, url)
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: int) -> Job:
    cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"Job {job_id} not found")
    return Job.from_row(row)


def get_job_by_url(conn: sqlite3.Connection, url: str) -> Optional[Job]:
    cur = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,))
    row = cur.fetchone()
    return Job.from_row(row) if row else None


def get_job_by_hash(conn: sqlite3.Connection, jd_hash: str) -> Optional[Job]:
    cur = conn.execute("SELECT * FROM jobs WHERE jd_hash = ?", (jd_hash,))
    row = cur.fetchone()
    return Job.from_row(row) if row else None


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> Job:
    """Update arbitrary columns on a job row."""
    if not fields:
        return get_job(conn, job_id)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return get_job(conn, job_id)


def _hash_jd(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
