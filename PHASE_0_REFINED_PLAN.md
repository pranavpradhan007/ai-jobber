# Job-Agent V1 — Phase 0 Refined Plan

## 1. Purpose

V1 is a **local-laptop job application agent** that, while you sleep, discovers
job postings, scores them against your profile, tailors a resume (truthfully),
verifies every claim, packages applications, and either submits them automatically
(`auto_safe` tier) or parks them for your one-click morning approval (`gated` tier).
It monitors outcomes and learns per application.

**Strict out-of-scope for V1:**
- Cloud deployment (V3)
- Outreach / cold-email (V2)
- LinkedIn scraping
- CAPTCHA or MFA bypass (hand-off only)
- Generating claims not present in `source_bank`

---

## 2. Core principles

| Principle | Rule |
|-----------|------|
| **Truthfulness** | Every claim in every output sentence must trace to a `source_bank` item |
| **No silent failures** | Every caught error is logged with context; swallowed exceptions are gate failures |
| **Deterministic tests** | LLM steps tested against recorded/mocked responses |
| **State machine integrity** | Illegal transitions rejected; `SUBMITTING` requires `verifier_passed=1` AND (`auto_safe=1` OR `approved_by_user=1`) |
| **Secrets in vault only** | No credential ever appears in logs, CSVs, or non-vault files |
| **Human gate on `gated` tier** | `approved_by_user` must be set before any `gated` job submits |

---

## 3. Technology stack

| Layer | Choice | Reason |
|-------|--------|--------|
| Language | Python 3.11+ | Broad ecosystem, simple scripting |
| Database | SQLite 3 (WAL mode) | Zero-server, sufficient for local V1 |
| ORM / DB layer | Raw sqlite3 + typed accessors | No heavyweight ORM needed |
| LLM calls | Anthropic SDK (claude-sonnet-4-6) | Fit scoring, rephrase, classification |
| DOCX | python-docx | Resume generation |
| PDF | weasyprint or docx2pdf | PDF from DOCX |
| Tests | pytest | Standard, simple |
| Browser | playwright (sync) | Pre-fill only, no evasion |
| Gmail | google-api-python-client | Send/parse |
| Secrets | python-dotenv + .env (gitignored) | Local vault |

---

## 4. Directory tree (target state after Sprint 0)

```
job-agent/                          ← repo root (E:\job search\)
├── schema.sql                      ← canonical DB schema
├── PHASE_0_REFINED_PLAN.md         ← this file
├── CLAUDE.md                       ← agent working agreement
├── Makefile                        ← make smoke / make regression / make test
├── pyproject.toml                  ← dependencies + tool config
├── .env.example                    ← vault template (never .env itself)
│
├── rules/
│   ├── truthfulness.md
│   ├── safety.md
│   ├── versions.md
│   └── approvals.md
│
├── .claude/
│   └── settings.json               ← hook configuration
│
├── hooks/                          ← dev-session git/pre-tool hooks
│   ├── secret_scanner.py
│   └── protected_files_guard.py
│
├── src/
│   ├── __init__.py
│   ├── db/                         ← Sprint 1: DB layer + typed accessors
│   ├── queues/                     ← Sprint 1: work_queue poller + leases
│   ├── storage/                    ← Sprint 1+2: folder mgmt + bank loaders
│   ├── verifier/                   ← Sprint 2: diff-verifier + hard gate
│   ├── parsing/                    ← Sprint 3: JD parser + jd_hash
│   ├── keywords/                   ← Sprint 3: keyword extractor
│   ├── scoring/                    ← Sprint 3: fit scorer + score gate
│   ├── resume/                     ← Sprint 4: tailoring + DOCX/PDF
│   ├── essays/                     ← Sprint 4 stub → Sprint 5
│   ├── gmail/                      ← Sprint 5: send/parse (mocked in tests)
│   ├── approvals/                  ← Sprint 5: reply parser
│   ├── browser/                    ← Sprint 5+7: auto_safe submit + pre-fill
│   ├── monitoring/                 ← Sprint 6: outcome classifier
│   └── learning/                   ← Sprint 8: per-app notes + pending facts
│
├── tests/
│   ├── conftest.py
│   ├── smoke/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│       ├── sample_job.json
│       ├── sample_source_bank.sql
│       ├── sample_resume.docx
│       ├── fake_gmail_thread.json
│       └── portal_html/
│           ├── workday_form.html
│           └── greenhouse_form.html
│
├── applications/                   ← per-job folders created at runtime
│   └── YYYY-MM/
│       └── {company}_{title}_{id}/
│           ├── resume.docx
│           ├── resume.pdf
│           ├── resume_diff.md
│           ├── scorecard.json
│           ├── application_answers.json
│           └── verifier_report.json
│
├── knowledge_base/
│   └── profile/                    ← seed files for source_bank
│       ├── verified_facts.yaml     ← PROTECTED — never auto-written
│       ├── metrics.yaml
│       ├── tools.yaml
│       ├── skills.yaml
│       └── credentials.yaml
│
├── learning/                       ← per-app structured notes
│   ├── portal_quirks.md
│   ├── question_patterns.md
│   └── keyword_mappings.md
│
└── pending/
    └── pending_user_verification.md   ← new facts awaiting your approval
```

---

## 5. Truthfulness architecture

```
knowledge_base/profile/verified_facts.yaml   ← ground truth (you write, protected)
        │
        ▼ seed script (Sprint 2)
source_bank table
        │
        ▼ retrieval API (enforces usage_level)
resume tailoring (Sprint 4) ──► bounded rephrase (LLM, mocked in tests)
        │
        ▼ diff-verifier (Sprint 2)
claims table  ──► allowed=1 → continue
              ──► allowed=0, needs_approval=1 → approvals table → human
              ──► allowed=0, high-stakes → BLOCKS state transition
        │
        ▼ hard gate helper
verifier_passed = 1  (only when ALL claims allowed)
        │
        ▼ state machine (Sprint 1)
SUBMITTING  (only reachable with verifier_passed=1)
```

---

## 6. State machine

### Valid states

```
DISCOVERED
SCORED
SKIPPED
TAILORING
RESUME_VERIFIED
PACKAGING
READY_TO_SUBMIT
WAITING_FOR_USER_APPROVAL
SUBMITTING
SUBMITTED
WAITING_FOR_CAPTCHA
WAITING_FOR_MFA
WAITING_FOR_GMAIL_VERIFICATION
MONITORING
INTERVIEW_SCHEDULED
OA_RECEIVED
REJECTED
FAILED
SNOOZED
DEAD
```

### Legal transitions

```python
LEGAL_TRANSITIONS = {
    "DISCOVERED":                 {"SCORED", "SKIPPED"},
    "SCORED":                     {"TAILORING", "SKIPPED"},
    "TAILORING":                  {"RESUME_VERIFIED", "FAILED"},
    "RESUME_VERIFIED":            {"PACKAGING"},
    "PACKAGING":                  {"READY_TO_SUBMIT"},
    "READY_TO_SUBMIT":            {"SUBMITTING", "WAITING_FOR_USER_APPROVAL"},
    "WAITING_FOR_USER_APPROVAL":  {"SUBMITTING", "SKIPPED", "TAILORING", "SNOOZED"},
    "SUBMITTING":                 {"SUBMITTED", "WAITING_FOR_CAPTCHA",
                                   "WAITING_FOR_MFA", "WAITING_FOR_GMAIL_VERIFICATION",
                                   "FAILED"},
    "SUBMITTED":                  {"MONITORING"},
    "MONITORING":                 {"INTERVIEW_SCHEDULED", "OA_RECEIVED",
                                   "REJECTED", "MONITORING"},
    "WAITING_FOR_CAPTCHA":        {"SUBMITTING", "FAILED"},
    "WAITING_FOR_MFA":            {"SUBMITTING", "FAILED"},
    "WAITING_FOR_GMAIL_VERIFICATION": {"SUBMITTING", "FAILED"},
    "SNOOZED":                    {"WAITING_FOR_USER_APPROVAL"},
    "FAILED":                     {"DEAD"},
    "INTERVIEW_SCHEDULED":        set(),
    "OA_RECEIVED":                set(),
    "REJECTED":                   set(),
    "SKIPPED":                    set(),
    "DEAD":                       set(),
}

# SUBMITTING invariant (checked in addition to LEGAL_TRANSITIONS):
# verifier_passed = 1  AND  (auto_safe = 1  OR  approved_by_user = 1)
```

---

## 7. Submit-tier assignment rules

| Condition | Tier |
|-----------|------|
| `platform = 'email'` | `auto_safe` |
| `platform = 'api'`   | `auto_safe` |
| `has_screener = 1`   | `gated`     |
| `login_required = 1` | `gated`     |
| `platform IN ('workday','greenhouse','lever','ashby','custom')` | `gated` |
| `platform = 'email'` overrides screener flag | `auto_safe` |

---

## 8. Scoring rubric (weighted, §12.3)

| Dimension | Weight |
|-----------|--------|
| Title / seniority match | 25 % |
| Must-have skills overlap | 30 % |
| Nice-to-have skills      | 15 % |
| Industry / domain fit    | 15 % |
| Location / remote match  | 10 % |
| Salary range fit         | 5 %  |

Threshold: `score >= 60` → `should_apply = 1`; below → `should_apply = 0`, no downstream work enqueued.

---

## 9. Approval reply commands

| Command | Effect |
|---------|--------|
| `APPROVE` | `approved_by_user = 1` → advance to `SUBMITTING` |
| `REJECT`  | `should_apply = 0`, state → `SKIPPED` |
| `SKIP`    | State → `SKIPPED` (can revisit) |
| `EDIT`    | State → `TAILORING` (re-tailor) |
| `SNOOZE`  | State → `SNOOZED` |
| `DONE`    | Mark entire digest processed |
| `MANUAL`  | Flag for manual handling, no auto action |

---

## 10. Monitoring event types and color mapping

| Event | color_status |
|-------|-------------|
| `receipt`        | gray   |
| `rejection`      | red    |
| `oa`             | yellow |
| `interview`      | green  |
| `positive_reply` | green  |
| `unknown`        | gray   |

Notification to user: only on `interview` and `positive_reply`.

---

## 11. Queue task types and ownership

| task_type | Owner sprint | Notes |
|-----------|-------------|-------|
| `score`   | Sprint 3 | parse + keyword + fit score |
| `tailor`  | Sprint 4 | retrieval + rephrase + verify |
| `submit`  | Sprint 5 | auto_safe path |
| `monitor` | Sprint 6 | Gmail outcome classification |
| `prefill` | Sprint 7 | Browser pre-fill for gated |
| `learn`   | Sprint 8 | Post-application note writing |

---

## 12. Secret / credential policy

- All credentials live in `.env` (gitignored) — no exceptions.
- Code references `os.environ` or `python-dotenv` load — never inline strings.
- `secret_scanner.py` hook blocks any commit/file-write that matches credential patterns.
- `protected_files_guard.py` blocks any automated write to `verified_facts.yaml`.
- CSV exports, logs, and audit_log never contain credentials.

---

## 13. Test fixture inventory (Sprint 0+)

| Fixture file | Used by |
|---|---|
| `tests/fixtures/sample_job.json` | Sprint 1+: seeding a job row |
| `tests/fixtures/sample_source_bank.sql` | Sprint 2+: pre-seeded bank |
| `tests/fixtures/sample_resume_bullets.json` | Sprint 4+: tailoring input |
| `tests/fixtures/fake_gmail_thread.json` | Sprint 5+6: Gmail mock |
| `tests/fixtures/portal_html/workday_form.html` | Sprint 7: pre-fill |
| `tests/fixtures/portal_html/greenhouse_form.html` | Sprint 7: pre-fill |
| `tests/fixtures/scorecard_pass.json` | Sprint 3+: score gate |
| `tests/fixtures/scorecard_fail.json` | Sprint 3+: skip gate |
