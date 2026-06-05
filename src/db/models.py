"""
Typed dataclass models matching schema.sql rows.
These are value objects — no DB calls here.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    id: int
    source: str
    url: str
    jd_hash: Optional[str] = None
    company: Optional[str] = None
    title: Optional[str] = None
    location: Optional[str] = None
    remote: int = 0
    raw_jd: Optional[str] = None
    clean_jd: Optional[str] = None
    has_screener: int = 0
    login_required: int = 0
    platform: Optional[str] = None
    email_apply_addr: Optional[str] = None
    discovered_at: Optional[str] = None
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Job":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass
class Application:
    id: int
    job_id: int
    state: str
    submit_tier: Optional[str] = None
    should_apply: int = 1
    score: Optional[float] = None
    scorecard_path: Optional[str] = None
    resume_path: Optional[str] = None
    resume_pdf_path: Optional[str] = None
    resume_diff_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    application_answers_path: Optional[str] = None
    verifier_passed: int = 0
    approved_by_user: int = 0
    auto_safe: int = 0
    receipt: Optional[str] = None
    submitted_at: Optional[str] = None
    folder_path: Optional[str] = None
    monitoring_status: Optional[str] = None
    color_status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Application":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass
class WorkQueueItem:
    id: int
    application_id: int
    task_type: str
    state: str
    attempts: int
    max_attempts: int
    payload: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    available_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "WorkQueueItem":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass
class StateTransition:
    id: int
    application_id: int
    from_state: Optional[str]
    to_state: str
    reason: Optional[str]
    actor: str
    transitioned_at: str

    @classmethod
    def from_row(cls, row) -> "StateTransition":
        return cls(**{k: row[k] for k in row.keys()})
