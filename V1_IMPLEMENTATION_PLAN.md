# Job-Agent V1 — Implementation Plan for Claude Code (Corporate-Hierarchy Model)

This is the **V1 (local laptop) build only**. No cloud (V3), no outreach (V2),
no LinkedIn, no CAPTCHA/MFA bypass. It assumes `schema.sql` and
`PHASE_0_REFINED_PLAN.md` already exist in the repo.

You (Claude Code) will execute this by **adopting a corporate engineering org**.
Each sprint is owned by a role. **No sprint may begin until the previous
sprint's QA gate (smoke + full cumulative regression) has passed and the Release
Manager has signed off.** That gate is a hard blocker, not a suggestion.

---

## 1. The org chart (roles you adopt)

| Role | Owns | Never does |
|------|------|-----------|
| **CTO / Orchestrator** | Phase sequencing, gate enforcement, status summaries, refusing illegal state | Write resume claims; submit applications |
| **Eng Manager (EM)** | Decompose each sprint into tickets; track Definition of Done | Skip a QA gate |
| **Infra Engineer** | DB, queue+leases, state machine, CSV export, folders, logging | — |
| **Trust & Safety Eng** | `source_bank`, constrained generation, diff-verifier, hard gate | Approve unsupported claims |
| **Scoring Engineer** | Fit scoring, keyword extraction, score gate | Let low-fit jobs through |
| **Resume Engineer** | Retrieval + bounded rephrase, DOCX/PDF, diff output | Compose claims from scratch |
| **Integrations Eng** | Overnight runner, morning batch digest, Gmail send/parse, `auto_safe` submit | Submit `gated` jobs without approval |
| **Monitoring Engineer** | Gmail outcome classification, status/color updates | — |
| **Browser Automation Eng** | Pre-fill `gated` jobs into a parked session | Solve CAPTCHA/MFA; evade anti-bot |
| **Learning Engineer** | One structured note per application; pattern files | Promote new personal facts to verified |
| **QA Engineer** (recurring) | Smoke + regression after **every** sprint; owns the cumulative suite | Pass a gate with a failing test |
| **Security Engineer** (recurring) | Secret scanning, vault-refs-only audit; reviews any sprint touching secrets/Gmail/browser | — |
| **Release Manager** (recurring) | Verifies exit criteria, signs the gate, authorizes next sprint | Sign off with red tests |

When you start a sprint, **announce the role you are wearing** and act within its
"never does" constraints. The CTO/Orchestrator persona is your default between
roles.

---

## 2. Sprint ceremony (the loop you repeat for every sprint)

```
1. EM        — restate objective; expand tickets; confirm scope is V1-only
2. <Owner>   — build tickets; commit small; no silent failures (log every error)
3. Security  — (if sprint touches secrets/Gmail/browser) audit before QA
4. QA        — run SMOKE (fast happy path). If red, back to step 2.
5. QA        — run REGRESSION (all prior sprints' suites). If red, back to step 2.
6. Release Mgr — verify exit criteria one by one; sign off OR reject with reasons
7. Learning  — write a short retro note (what broke, what to watch) to memory.md
8. CTO       — only now: advance to next sprint
```

**Rule:** you may not write code for sprint N+1 while any test in sprint N's
cumulative suite is red. State this explicitly at each gate.

---

## 3. Standing test policy

- **Smoke** = a fast, narrow happy-path check proving the sprint's new capability
  works end-to-end in isolation. Seconds to run. Lives in `tests/smoke/`.
- **Regression** = re-run **every prior sprint's** unit + integration suite to
  prove nothing broke. Grows each sprint (see the matrix in §5). Lives under
  `tests/unit/`, `tests/integration/`, `tests/<domain>/`.
- **Fixtures**: build reusable fixtures early — a sample parsed job, a seeded
  `source_bank`, a fake Gmail thread, a sample Workday/Greenhouse form HTML.
  Store under `tests/fixtures/`. Never hit a live portal or live Gmail in tests.
- **No silent failures**: every caught error is logged with context and surfaces
  in the sprint smoke output. A swallowed exception is a test failure.
- **Determinism**: LLM-calling steps are tested against recorded/mocked responses
  so regression is reproducible. Live-model calls are exercised only in manual
  smoke, clearly labeled.
- **Coverage gate**: each sprint's new `src/` modules need unit tests before the
  Release Manager signs off. No module ships untested.

---

## 4. Kickoff prompt (paste this into Claude Code first)

> You are building V1 of the job-agent project (local laptop only; no cloud, no
> outreach, no LinkedIn, no CAPTCHA/MFA bypass). Read `schema.sql`,
> `PHASE_0_REFINED_PLAN.md`, and everything in `rules/`. Adopt the corporate
> engineering org defined in the V1 implementation plan: you will wear a named
> role per sprint, announce the role, and respect its constraints. Execute
> sprints strictly in order. After each sprint run the QA gate — smoke tests,
> then the full cumulative regression suite — and do not begin the next sprint
> until every test passes and you have produced a Release Manager sign-off
> listing each exit criterion as met. Treat any swallowed error or skipped test
> as a gate failure. Start with Sprint 0 and stop at its gate for my review.

---

## 5. Sprints

Every sprint below uses the same shape: **Owner · Objective · Tickets ·
Deliverables · Smoke · Regression scope · Exit gate**, plus a ready-to-paste
sprint prompt.

---

### Sprint 0 — Repo skeleton & working agreement
**Owner:** CTO / Orchestrator (support: EM, Security)

**Objective:** Lay the repo, the rules, and the test harness so every later
sprint has a home and a gate.

**Tickets:** create repo tree from the plan; write `CLAUDE.md` (goals, version
split, the hard rules); write `rules/{truthfulness,safety,versions,approvals}.md`;
`.claude/settings.json`; the **2** dev-session hooks (`secret_scanner.py`,
`protected_files_guard.py`); set up `pytest`, `tests/{smoke,unit,integration,fixtures}/`,
and a `make test` / `make smoke` / `make regression` runner.

**Deliverables:** repo tree, `CLAUDE.md`, rules, two hooks, working test runner
that reports green on an empty suite.

**Smoke:** `make smoke` runs and reports "0 tests, OK"; secret_scanner hook
blocks a deliberately-planted fake password in a scratch file.

**Regression scope:** none yet.

**Exit gate:** tree matches plan; both hooks fire on a planted secret/protected-file
edit; test runner executes. Release Manager signs off.

> **Prompt:** Wear the CTO/Orchestrator hat. Create the V1 repo skeleton exactly
> per PHASE_0_REFINED_PLAN.md §7, write CLAUDE.md and the four rules files, add
> the two dev-session hooks, and stand up a pytest harness with make targets for
> smoke/regression. Prove the secret_scanner hook blocks a planted plaintext
> password and the protected_files_guard blocks an edit to verified_facts.yaml.
> Stop at the gate.

---

### Sprint 1 — Core infrastructure (no model calls)
**Owner:** Infra Engineer (support: QA, Release Mgr)

**Objective:** The deterministic spine: DB, queue, state machine, exports.

**Tickets:** DB layer applying `schema.sql` + typed accessors; `work_queue`
poller with lease acquisition, lease-expiry reclaim, exponential backoff via
`available_at`, `max_attempts → dead`; **state machine as one transition
function** that rejects illegal transitions and enforces the `SUBMITTING`
invariant (`verifier_passed=1` AND (`auto_safe` OR `approved_by_user=1`)),
logging every change to `state_transitions`; per-job folder creation under
`applications/YYYY-MM/`; `export-csv` reading `v_data_csv` / `v_applied_csv`;
cost + audit logging.

**Deliverables:** `src/db`, `src/queues`, `src/storage`, `export-csv` command.

**Smoke:** create a job → create an application → walk DISCOVERED…READY_TO_SUBMIT
→ export both CSVs → confirm folder exists.

**Regression scope:** Sprint 0 + this sprint.

**Exit gate:** illegal transition is rejected; a `SUBMITTING` attempt with
`verifier_passed=0` is rejected; queue reclaims a lease after expiry; both CSVs
export from views; every transition appears in `state_transitions`.

> **Prompt:** Wear the Infra Engineer hat. Implement Sprint 1 per the plan and
> schema.sql. Unit-test the state machine (all legal transitions + a sample of
> illegal ones + the SUBMITTING invariant), queue lease/reclaim/backoff, and CSV
> export from the views. No browser, Gmail, or LLM calls. Then run the QA gate
> (smoke + regression over Sprints 0–1) and produce a Release Manager sign-off.

---

### Sprint 2 — Source bank & constrained-generation verifier
**Owner:** Trust & Safety Engineer (support: Security, QA)

**Objective:** Make truthfulness structural before anything generates text.

**Tickets:** seed `source_bank` from `knowledge_base/profile/*` with
`metrics`/`tools`/`keywords`/`source_ref`/`usage_level`; a retrieval API that
returns only bank items legal for a given surface (enforces `usage_level`); the
**diff-verifier**: for each output sentence, locate its parent bank item and flag
any introduced metric/tool/company/title/credential not in the source → write to
`claims`, set `allowed`, and route high-stakes blocks to an approval record; the
hard gate helper used by the state machine.

**Deliverables:** `src/verifier`, `src/storage` bank loaders, seed script.

**Smoke:** feed a clean rephrase (passes) and a rephrase that injects a fake
metric and a fake tool (both blocked, `needs_approval`); a `private_never_submit`
item is refused for a resume surface.

**Regression scope:** Sprints 0–2.

**Exit gate:** unsupported claim blocked; verified claim passes; `usage_level`
enforced at retrieval; high-stakes block creates a pending approval; gate helper
refuses to set `verifier_passed=1` when any claim is `unsupported`.

> **Prompt:** Wear the Trust & Safety Engineer hat. Implement the source_bank
> seeding, the usage_level-enforcing retrieval API, and the diff-verifier exactly
> per PHASE_0_REFINED_PLAN.md §5. Use mocked text inputs (no live model) for
> deterministic tests. Prove injected metric/tool/title are blocked and
> private_never_submit cannot reach a resume. Run the QA gate over Sprints 0–2
> and produce a sign-off.

---

### Sprint 3 — Scoring & keyword extraction (score gate)
**Owner:** Scoring Engineer (support: QA)

**Objective:** Decide apply/skip cheaply, and gate volume.

**Tickets:** job parser → clean JD + `jd_hash`; keyword extractor → `hot_keywords.json`;
fit scorer (cheap model, mocked in tests) producing `scorecard.json` with the
weighted breakdown and a human-readable reason; **score gate** sets
`should_apply` and `submit_tier` (using `has_screener`/`login_required`/platform);
only jobs above threshold enqueue downstream work.

**Deliverables:** `src/parsing`, `src/keywords`, `src/scoring`.

**Smoke:** a sample JD → scorecard with explainable reason → keywords mapped to
bank items where possible → `submit_tier` correctly assigned (screener job →
`gated`, email-apply job → `auto_safe`).

**Regression scope:** Sprints 0–3.

**Exit gate:** score is explainable; below-threshold job is marked
`should_apply=0` and does **not** enqueue tailoring; `submit_tier` assignment
matches portal/screener facts.

> **Prompt:** Wear the Scoring Engineer hat. Build parsing, keyword extraction,
> and the score gate with the weighted rubric from the original brief §12.3, using
> a mocked scoring model for deterministic tests. Ensure the gate assigns
> submit_tier and blocks low-fit jobs from downstream queues. Run the QA gate over
> Sprints 0–3 and produce a sign-off.

---

### Sprint 4 — Resume tailoring
**Owner:** Resume Engineer (support: Trust & Safety, QA)

**Objective:** Tailor truthfully by retrieval + bounded rephrase, then verify.

**Tickets:** select bank bullets by hot keywords; bounded rephrase (Sonnet,
mocked in tests) that may reword but not add claims; reorder sections; render
DOCX + PDF; write `resume_diff.md`; **invoke the diff-verifier** and only then
allow `RESUME_VERIFIED`; store all artifacts in the job folder and set paths on
the application row.

**Deliverables:** `src/resume`, `src/essays` stub for later.

**Smoke:** scored job → tailored DOCX+PDF in folder → `resume_diff.md` →
verifier report passes → application reaches `RESUME_VERIFIED` and paths recorded.

**Regression scope:** Sprints 0–4 (notably: verifier still blocks injected claims
when driven through the real tailoring path).

**Exit gate:** tailored resume generated; verifier passes; no unsupported
metric/tool/claim survives; `applied.csv` points to the resume; a forced
hallucination in rephrase is caught and blocks `RESUME_VERIFIED`.

> **Prompt:** Wear the Resume Engineer hat. Build retrieval-based tailoring with
> bounded rephrase (mocked model in tests), DOCX/PDF rendering, resume_diff.md,
> and mandatory verifier invocation before RESUME_VERIFIED. Add a test where the
> mocked model injects a fake metric and prove the gate blocks it. Run the QA gate
> over Sprints 0–4 and produce a sign-off.

---

### Sprint 5 — Overnight runner, morning batch & `auto_safe` submit
**Owner:** Integrations Engineer (support: Security, QA, Release Mgr)

**Objective:** The actual "while you sleep" pipeline + the one human gate.

**Tickets:** `run-overnight` entrypoint that drains the queue through
parse→score→tailor→verify→package for all eligible jobs; `auto_safe` submit path
for email-apply/API/simple-form jobs (after `verifier_passed=1`), capturing a
receipt; `gated` jobs → `WAITING_FOR_USER_APPROVAL`; `prep-batch` builds **one
digest email** of all gated apps (screener answers + resume links);
`check-approvals` parses your replies (APPROVE/REJECT/EDIT/SKIP/SNOOZE/DONE/MANUAL)
and advances state; Gmail send/read via API (mocked in tests); escalation paths
to `WAITING_FOR_{CAPTCHA,MFA,GMAIL_VERIFICATION}` that **hand off, never bypass**.

**Deliverables:** `src/gmail`, `src/approvals`, `src/browser` (auto_safe email/API
submit only), `run-overnight`, `prep-batch`, `check-approvals` commands.

**Smoke:** seed 3 jobs (1 `auto_safe`, 2 `gated`) → `run-overnight` → auto_safe
submitted with receipt; gated pair appears in one digest → simulate an APPROVE +
a SKIP reply → approved one submits, skipped one parks. All Gmail mocked.

**Regression scope:** Sprints 0–5 (verifier gate must still hold on the submit path).

**Exit gate:** no `gated` job submits without `approved_by_user=1`; no job submits
with `verifier_passed=0`; batch digest renders; reply parser handles every
command; CAPTCHA/MFA route to hand-off, never auto-solve. Security signs the
secrets audit (vault refs only, nothing in logs/CSV).

> **Prompt:** Wear the Integrations Engineer hat. Build the overnight runner, the
> auto_safe submit path, the single morning batch digest, and the approval reply
> parser, with Gmail fully mocked in tests. Enforce the SUBMITTING invariant on
> every path and route CAPTCHA/MFA to hand-off. Have Security audit for any
> plaintext secret leakage. Run the QA gate over Sprints 0–5 and produce a sign-off.

---

### Sprint 6 — Monitoring
**Owner:** Monitoring Engineer (support: QA)

**Objective:** Turn inbound email into application status.

**Tickets:** Gmail outcome classifier (mocked) → `monitoring_events`
(receipt/rejection/oa/interview/positive_reply/unknown); update
`monitoring_status` + `color_status`; email you on important positives only.

**Deliverables:** `src/monitoring`.

**Smoke:** feed fixture threads (a rejection, an OA, an interview invite, a
positive reply) → correct event types → red/green statuses set → positive
triggers a notification (mocked send).

**Regression scope:** Sprints 0–6.

**Exit gate:** each fixture classifies correctly; low-confidence → `unknown` with
raw excerpt kept for your review (never a confident wrong call); statuses/colors
update.

> **Prompt:** Wear the Monitoring Engineer hat. Build the Gmail outcome classifier
> against fixture threads (mocked Gmail), write monitoring_events, update status
> and color, and notify on positives only. Run the QA gate over Sprints 0–6 and
> produce a sign-off.

---

### Sprint 7 — Browser pre-fill assist (optional, `gated` only)
**Owner:** Browser Automation Engineer (support: Security, QA)

**Objective:** Park gated applications at the submit screen so your morning click
is instant. DOM/Playwright fill only — **no submit, no CAPTCHA/MFA, no evasion.**

**Tickets:** portal classifier (Workday/Greenhouse/Lever/Ashby/custom from
fixtures); field mapper from `application_answers.json`; pre-fill into a real
authenticated session left at review-and-submit; before/after screenshots;
pause+escalate on CAPTCHA/MFA/unknown field.

**Deliverables:** `src/browser` (pre-fill module).

**Smoke:** against saved fixture form HTML, map and fill fields, stop at submit,
capture screenshots; an injected CAPTCHA fixture triggers hand-off, not a solve
attempt.

**Regression scope:** Sprints 0–7.

**Exit gate:** fills a sample portal to the submit screen and stops; never clicks
submit autonomously for `gated`; CAPTCHA/MFA → escalation; no anti-bot evasion
code exists (Security confirms). Rate limits respected.

> **Prompt:** Wear the Browser Automation Engineer hat. Build pre-fill for gated
> portals using saved fixture HTML (no live portals in tests). It must fill to the
> submit screen and stop, capture screenshots, and escalate on CAPTCHA/MFA. Prove
> it never auto-submits a gated job and contains no evasion logic. Run the QA gate
> over Sprints 0–7 and produce a sign-off.

---

### Sprint 8 — Learning loop
**Owner:** Learning Engineer (support: Trust & Safety, QA)

**Objective:** Capture lessons without inventing facts.

**Tickets:** after each application, write a structured note (portal quirks, new
question patterns, blockers, keyword→fact mappings) to the `learning/` files; any
candidate new personal fact goes to `pending/pending_user_verification.md`, never
to `verified_facts.yaml`; repeated safe questions become candidate defaults only.

**Deliverables:** `src/learning`.

**Smoke:** run an application end-to-end → a learning note is written → a
discovered new personal fact lands in pending (not verified) → a repeated benign
question is proposed as a default, not auto-applied.

**Regression scope:** Sprints 0–8 (full V1 suite).

**Exit gate:** every application produces a learning summary; no new personal fact
becomes verified without your approval; the full cumulative suite is green.

> **Prompt:** Wear the Learning Engineer hat. Build the per-application learning
> note writer and the pending-facts flow. Prove a new personal fact never reaches
> verified_facts.yaml automatically. Run the **full V1 regression** (Sprints 0–8)
> and produce the final V1 sign-off.

---

## 6. Cumulative regression matrix

At each gate, **all** suites with an ✓ in that column must be green.

| Suite \ Gate | S1 | S2 | S3 | S4 | S5 | S6 | S7 | S8 |
|---|---|---|---|---|---|---|---|---|
| state machine / queue / CSV | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| verifier / source_bank | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| scoring / keywords / tier | | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| resume tailoring | | | | ✓ | ✓ | ✓ | ✓ | ✓ |
| overnight / batch / approvals / submit invariant | | | | | ✓ | ✓ | ✓ | ✓ |
| monitoring | | | | | | ✓ | ✓ | ✓ |
| browser pre-fill (no-submit / no-evasion) | | | | | | | ✓ | ✓ |
| learning / pending-facts | | | | | | | | ✓ |
| security (secrets/vault) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## 7. V1 "done" definition

V1 is complete when, with the laptop on and you asleep, the system discovers,
scores, tailors, verifies, and packages eligible jobs overnight; submits the
`auto_safe` tier unattended; parks `gated` jobs into one morning approval email;
resumes them from your replies; monitors outcomes; learns per application — and
the **entire Sprints 0–8 regression suite is green** with a Release Manager
sign-off for every gate. No silent failures, every application has an artifact
folder, every tailored resume has a passing verifier report, and no secret ever
appears outside the vault.
