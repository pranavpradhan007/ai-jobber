# Job-Agent V1 — Working Agreement

## What this is

A local-laptop job application agent. While you sleep it discovers, scores,
tailors, verifies, and submits job applications. Gated applications wait for
your one-click morning approval. It monitors outcomes and learns per application.

## Version split

| Version | Scope |
|---------|-------|
| **V1 (this repo)** | Local laptop. No cloud, no outreach, no LinkedIn. |
| V2 (future) | Outreach / cold-email |
| V3 (future) | Cloud deployment |

**Claude Code must never write V2/V3 features into V1 code.**

## Hard rules (violations = immediate gate failure)

1. **No invented claims.** Every sentence in every generated output must trace
   to a `source_bank` item. The diff-verifier enforces this structurally.

2. **No CAPTCHA/MFA bypass.** Encountering either triggers a hand-off state
   (`WAITING_FOR_CAPTCHA`, `WAITING_FOR_MFA`), never an auto-solve attempt.

3. **No auto-submit on `gated` jobs.** A `gated` application requires
   `approved_by_user = 1` before `SUBMITTING`. The state machine enforces this.

4. **No plaintext secrets.** All credentials live in `.env` (gitignored).
   `secret_scanner.py` blocks violations before any file is written.

5. **No writes to `verified_facts.yaml`.** Only the human writes this file.
   `protected_files_guard.py` enforces this. New discovered facts go to
   `pending/pending_user_verification.md`.

6. **No swallowed exceptions.** Every caught error is logged with context.
   A swallowed exception is treated as a test failure at the QA gate.

7. **No sprint may begin while any prior test is red.** The Release Manager
   must sign off before advancing.

## Sprint ceremony

```
1. EM        — restate objective; expand tickets; confirm V1-only scope
2. <Owner>   — build; commit small; log every error
3. Security  — (if sprint touches secrets/Gmail/browser) audit before QA
4. QA        — run SMOKE. If red → back to step 2.
5. QA        — run REGRESSION (all prior suites). If red → back to step 2.
6. Release Mgr — verify exit criteria; sign off OR reject with reasons
7. Learning  — retro note to memory
8. CTO       — advance to next sprint
```

## Test policy

- `make smoke`      — fast happy-path check, seconds to run
- `make regression` — full cumulative suite for all prior sprints
- `make test`       — smoke + regression

LLM steps are tested against mocked/recorded responses. Live model calls are
labeled `[LIVE]` and never run in CI. No test ever hits a live portal or Gmail.

## Directory quick-reference

```
src/         — production code only (no test imports)
tests/       — smoke/, unit/, integration/, fixtures/
hooks/       — dev-session hook scripts
rules/       — non-negotiable policy files
knowledge_base/profile/ — seed data for source_bank (verified_facts.yaml protected)
applications/ — runtime artifact folders (YYYY-MM/company_title_id/)
learning/     — per-application notes (auto-written by agent)
pending/      — facts awaiting your approval (agent writes here, not verified_facts)
```

## Key files

| File | Purpose |
|------|---------|
| `schema.sql` | Canonical SQLite schema — all tables, views, indexes |
| `PHASE_0_REFINED_PLAN.md` | Architecture, state machine, scoring rubric |
| `rules/truthfulness.md` | Claim verification policy |
| `rules/safety.md` | Submission + secret policy |
| `rules/versions.md` | V1/V2/V3 boundary |
| `rules/approvals.md` | Human approval flow |
| `hooks/secret_scanner.py` | Blocks plaintext credentials |
| `hooks/protected_files_guard.py` | Blocks writes to verified_facts.yaml |
