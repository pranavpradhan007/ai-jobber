-- ============================================================
-- Job-Agent V1 — SQLite Schema
-- All boolean columns: 0 = false, 1 = true
-- All timestamps: ISO-8601 UTC text
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- jobs: one row per unique posting (deduped by jd_hash)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER  PRIMARY KEY AUTOINCREMENT,
    source            TEXT     NOT NULL,                        -- 'manual', 'indeed', 'linkedin', ...
    url               TEXT     NOT NULL UNIQUE,
    jd_hash           TEXT     UNIQUE,                          -- SHA-256 of clean_jd
    company           TEXT,
    title             TEXT,
    location          TEXT,
    remote            INTEGER  NOT NULL DEFAULT 0,
    raw_jd            TEXT,                                     -- original scraped text
    clean_jd          TEXT,                                     -- stripped, normalised
    has_screener      INTEGER  NOT NULL DEFAULT 0,              -- requires extra Q&A
    login_required    INTEGER  NOT NULL DEFAULT 0,
    platform          TEXT,                                     -- 'workday','greenhouse','lever','ashby','email','api','custom'
    email_apply_addr  TEXT,                                     -- set when platform='email'
    discovered_at     TEXT     NOT NULL DEFAULT (datetime('now')),
    created_at        TEXT     NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- applications: one per job we decide to pursue
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS applications (
    id                      INTEGER  PRIMARY KEY AUTOINCREMENT,
    job_id                  INTEGER  NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    state                   TEXT     NOT NULL DEFAULT 'DISCOVERED',
    submit_tier             TEXT,                               -- 'auto_safe' | 'gated'
    should_apply            INTEGER  NOT NULL DEFAULT 1,        -- 0 → skip downstream work
    score                   REAL,
    scorecard_path          TEXT,
    resume_path             TEXT,
    resume_pdf_path         TEXT,
    resume_diff_path        TEXT,
    cover_letter_path       TEXT,
    application_answers_path TEXT,
    verifier_passed         INTEGER  NOT NULL DEFAULT 0,
    approved_by_user        INTEGER  NOT NULL DEFAULT 0,
    auto_safe               INTEGER  NOT NULL DEFAULT 0,
    receipt                 TEXT,
    submitted_at            TEXT,
    folder_path             TEXT,
    monitoring_status       TEXT,
    color_status            TEXT,                               -- 'gray','red','yellow','green'
    notes                   TEXT,
    created_at              TEXT     NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT     NOT NULL DEFAULT (datetime('now'))
);

-- Trigger: keep updated_at fresh
CREATE TRIGGER IF NOT EXISTS trg_applications_updated
    AFTER UPDATE ON applications
    FOR EACH ROW
BEGIN
    UPDATE applications SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- ------------------------------------------------------------
-- state_transitions: immutable audit trail
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_transitions (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER  NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_state       TEXT,
    to_state         TEXT     NOT NULL,
    reason           TEXT,
    actor            TEXT     NOT NULL DEFAULT 'system',
    transitioned_at  TEXT     NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- work_queue: lease-based task queue
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_queue (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER  NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    task_type        TEXT     NOT NULL,    -- 'score','tailor','verify','submit','monitor'
    payload          TEXT,                 -- JSON blob
    state            TEXT     NOT NULL DEFAULT 'pending',  -- 'pending','running','done','dead'
    attempts         INTEGER  NOT NULL DEFAULT 0,
    max_attempts     INTEGER  NOT NULL DEFAULT 3,
    lease_owner      TEXT,
    lease_expires_at TEXT,
    available_at     TEXT     NOT NULL DEFAULT (datetime('now')),
    created_at       TEXT     NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT     NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS trg_work_queue_updated
    AFTER UPDATE ON work_queue
    FOR EACH ROW
BEGIN
    UPDATE work_queue SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- ------------------------------------------------------------
-- source_bank: verified personal facts (truthfulness anchor)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_bank (
    id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    item_type    TEXT     NOT NULL,   -- 'metric','tool','skill','credential','title','company','keyword'
    content      TEXT     NOT NULL,
    source_ref   TEXT,                -- e.g. 'resume_2024.pdf:p2', 'linkedin_profile', 'verified_facts.yaml'
    usage_level  TEXT     NOT NULL DEFAULT 'resume',
                                      -- 'resume' | 'cover_letter' | 'screener' | 'private_never_submit'
    notes        TEXT,
    created_at   TEXT     NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_type, content)
);

-- ------------------------------------------------------------
-- claims: per-sentence verifier output
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER  NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    surface          TEXT     NOT NULL,   -- 'resume','cover_letter','screener'
    sentence         TEXT     NOT NULL,
    claim_text       TEXT     NOT NULL,
    claim_type       TEXT,               -- 'metric','tool','company','title','credential','keyword'
    source_bank_id   INTEGER  REFERENCES source_bank(id),
    allowed          INTEGER  NOT NULL DEFAULT 0,
    needs_approval   INTEGER  NOT NULL DEFAULT 0,
    created_at       TEXT     NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- approvals: human-gated decisions
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER  NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    claim_id         INTEGER  REFERENCES claims(id),
    approval_type    TEXT     NOT NULL,   -- 'claim' | 'submit'
    status           TEXT     NOT NULL DEFAULT 'pending',  -- 'pending','approved','rejected','snoozed'
    requested_at     TEXT     NOT NULL DEFAULT (datetime('now')),
    resolved_at      TEXT,
    resolved_by      TEXT,               -- 'user' | 'system'
    command          TEXT                -- raw reply command: APPROVE/REJECT/EDIT/SKIP/SNOOZE/DONE/MANUAL
);

-- ------------------------------------------------------------
-- monitoring_events: inbound email → outcome classification
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring_events (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER  NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    event_type       TEXT     NOT NULL,
                              -- 'receipt'|'rejection'|'oa'|'interview'|'positive_reply'|'unknown'
    email_subject    TEXT,
    email_snippet    TEXT,
    confidence       REAL,
    raw_excerpt      TEXT,    -- kept verbatim when confidence < threshold
    gmail_thread_id  TEXT,
    created_at       TEXT     NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- audit_log: cost + action trail
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER  REFERENCES applications(id),
    action           TEXT     NOT NULL,
    model            TEXT,
    input_tokens     INTEGER  NOT NULL DEFAULT 0,
    output_tokens    INTEGER  NOT NULL DEFAULT 0,
    cost_usd         REAL     NOT NULL DEFAULT 0.0,
    details          TEXT,               -- JSON
    created_at       TEXT     NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- VIEWS
-- ------------------------------------------------------------

-- Full data export (all applications)
CREATE VIEW IF NOT EXISTS v_data_csv AS
SELECT
    j.company,
    j.title,
    j.location,
    j.remote,
    j.url,
    j.platform,
    j.has_screener,
    a.id                AS application_id,
    a.state,
    a.submit_tier,
    a.score,
    a.should_apply,
    a.verifier_passed,
    a.approved_by_user,
    a.monitoring_status,
    a.color_status,
    a.folder_path,
    a.created_at,
    a.submitted_at
FROM applications a
JOIN jobs j ON a.job_id = j.id;

-- Applied-only export
CREATE VIEW IF NOT EXISTS v_applied_csv AS
SELECT
    j.company,
    j.title,
    j.location,
    j.remote,
    j.url,
    j.platform,
    a.id                AS application_id,
    a.state,
    a.submit_tier,
    a.score,
    a.receipt,
    a.resume_path,
    a.resume_diff_path,
    a.monitoring_status,
    a.color_status,
    a.submitted_at
FROM applications a
JOIN jobs j ON a.job_id = j.id
WHERE a.state IN ('SUBMITTED', 'MONITORING', 'INTERVIEW_SCHEDULED', 'OA_RECEIVED');

-- ------------------------------------------------------------
-- INDEXES
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_applications_state       ON applications(state);
CREATE INDEX IF NOT EXISTS idx_applications_job_id      ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_work_queue_state_avail   ON work_queue(state, available_at);
CREATE INDEX IF NOT EXISTS idx_work_queue_app_type      ON work_queue(application_id, task_type);
CREATE INDEX IF NOT EXISTS idx_state_transitions_app    ON state_transitions(application_id);
CREATE INDEX IF NOT EXISTS idx_claims_application       ON claims(application_id);
CREATE INDEX IF NOT EXISTS idx_monitoring_application   ON monitoring_events(application_id);
CREATE INDEX IF NOT EXISTS idx_audit_application        ON audit_log(application_id);
CREATE INDEX IF NOT EXISTS idx_source_bank_type_level   ON source_bank(item_type, usage_level);
