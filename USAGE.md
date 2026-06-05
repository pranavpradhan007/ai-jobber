# ai-jobber — Usage Guide

> Local laptop job automation agent. Discovers jobs (Indeed), scores them,
> tailors your resume, and parks gated applications for your one-tap approval
> from any device. Auto-submits safe jobs while you sleep.

---

## Table of Contents

1. [One-time setup](#1-one-time-setup)
2. [Daily workflow](#2-daily-workflow)
3. [Discovering jobs](#3-discovering-jobs)
4. [Adding a single job manually](#4-adding-a-single-job-manually)
5. [Running the overnight pipeline](#5-running-the-overnight-pipeline)
6. [The morning digest email](#6-the-morning-digest-email)
7. [Replying from your phone](#7-replying-from-your-phone-full-command-reference)
8. [Checking application status](#8-checking-application-status)
9. [Exporting to CSV](#9-exporting-to-csv)
10. [How the pipeline works](#10-how-the-pipeline-works-end-to-end)
11. [Key files and folders](#11-key-files-and-folders)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. One-time setup

```bash
# Install dependencies
pip install -e ".[dev]"

# Copy env template (no API keys needed — Claude Code handles LLM + Gmail via MCP)
cp .env.example .env
# Edit .env to confirm your email address

# Initialise the database (auto-created on first run)
python -m job_agent status
```

**Verify everything works:**
```bash
make test        # 215 tests — all should be green
make smoke       # fast happy-path check only
```

---

## 2. Running modes

### Continuous mode (recommended — runs all day)
```bash
# Start the continuous loop — runs every 30 minutes, all day
job-agent run-loop

# Adjust interval
job-agent run-loop --interval 60       # every hour
job-agent run-loop --interval 10       # every 10 min (aggressive)

# Single cycle and exit
job-agent run-loop --once
```

Each cycle does:
1. Queue Indeed searches across the full US (Claude Code executes via MCP)
2. Score + tailor + verify all queued DISCOVERED jobs
3. Submit auto_safe jobs immediately
4. Gate jobs needing approval → send digest email
5. Check Gmail for your phone replies → APPROVE/EDIT/REJECT/SNOOZE
6. Sleep → repeat

Press **Ctrl-C** to stop cleanly after the current cycle finishes.

### Manual / one-shot mode
```bash
job-agent discover                     # queue searches
job-agent run-overnight                # one pipeline pass (no discover/approvals)
job-agent prep-batch --to you@email    # send digest email manually
job-agent check-approvals --from-gmail # process replies manually
```

### Typical day
```
08:00  run-loop starts — discovers + processes overnight queue
08:30  run-loop cycle 2 — picks up new jobs
…      (runs every 30 min all day)
12:00  you get a digest email on your phone
12:05  you reply: APP-1 APPROVE, APP-2 EDIT "..."
12:30  run-loop cycle picks up your reply → submits APP-1
18:00  shut down run-loop (Ctrl-C) or leave running overnight
```

---

## 3. Discovering jobs

### Job sources

| Source | Coverage | How |
|--------|----------|-----|
| **Indeed MCP** | ~10M US jobs, aggregates cross-posts from LinkedIn/Greenhouse/Workday/Lever | `job-agent discover` |
| **LinkedIn email alerts** | LinkedIn-exclusive postings via official alert emails | `job-agent discover-linkedin` |
| **Manual** | Any URL — LinkedIn, company page, referral | `job-agent add-job --url ...` |

### LinkedIn via Gmail job alerts (recommended)

LinkedIn sends official job alert emails to your Gmail. The agent reads them automatically via the Gmail MCP — no scraping, no API key, no ToS violation.

**One-time setup in LinkedIn (do this once):**
1. Go to [linkedin.com/jobs](https://www.linkedin.com/jobs)
2. Search for a role, e.g. `Machine Learning Engineer`
3. Click **"Set alert"** → Email → **Daily** (or "As they happen")
4. Repeat for each role you want covered:
   - `AI Research Engineer`
   - `Reinforcement Learning Engineer`
   - `Applied Scientist`
   - `LLM Engineer`
   - `Data Scientist machine learning`
   - *(any others relevant to you)*
5. LinkedIn will email `jobalerts@linkedin.com → pranavpradhan00721@gmail.com`

**The agent then handles everything automatically each cycle:**
```
run-loop cycle
  → Gmail MCP: search for LinkedIn alert emails
  → parse job title, company, location, LinkedIn job URL from each email
  → import_jobs() → DISCOVERED applications
  → pipeline scores, tailors, and gates them as normal
```

**Manual trigger:**
```bash
# Queue the Gmail fetch
job-agent discover-linkedin

# After Claude Code processes it, import the results
job-agent discover-linkedin --import-cached
```

### Discovery commands
```bash
# Default: 12 roles × 7 locations = 84 searches across the whole US
job-agent discover

# Remote-only
job-agent discover --remote-only

# Single city
job-agent discover --location "San Francisco, CA"

# Add a specific role on top of defaults
job-agent discover --role "Robotics ML Engineer"

# Specific role + specific city
job-agent discover --role "Quantitative Researcher ML" --location "Chicago, IL"
```

**Coverage: 12 roles × ~200 cities = ~2400 searches — every state, every major market.**
Rotates in batches of 60 per cycle. Full sweep completes every ~25 hours (once per day).
Remote searches always run every cycle.

**Every US state covered** (all 50 + DC), with extra cities for major tech states:
- **Remote** (every cycle)
- **California (23):** SF, San Jose, Oakland, Palo Alto, Mountain View, Sunnyvale, Santa Clara, Menlo Park, Redwood City, Fremont, LA, Santa Monica, Culver City, Irvine, San Diego, Sacramento, + more
- **Texas (10):** Austin, Dallas, Fort Worth, Houston, San Antonio, Plano, Irving, Round Rock, + more
- **New York (7):** NYC, Brooklyn, Buffalo, Rochester, Albany, Syracuse, Ithaca
- **Washington (6):** Seattle, Bellevue, Redmond, Kirkland, Tacoma, Spokane
- **Florida (10):** Miami, Tampa, Orlando, Jacksonville, Fort Lauderdale, Boca Raton, + more
- **Massachusetts (7):** Boston, Cambridge, Somerville, Waltham, Burlington, Worcester, Springfield
- **All other states:** at least 1–3 cities each, covering every market

**12 roles searched:**
Machine Learning Engineer · AI Research Engineer · Applied Scientist ML ·
Reinforcement Learning Engineer · LLM Engineer · Research Engineer ·
Data Scientist · MLOps Engineer · AI Engineer NLP · Scientific ML Engineer ·
Game AI Engineer · Robotics ML Engineer

---

## 4. Adding a single job manually

Use this when you find a job on LinkedIn, a company careers page, or anywhere else.

```bash
# Minimal — just URL + basics
job-agent add-job \
  --url "https://openai.com/careers/research-engineer-12345" \
  --company "OpenAI" \
  --title "Research Engineer" \
  --platform greenhouse

# With full job description from a file
job-agent add-job \
  --url "https://example.com/jobs/456" \
  --company "Wayve" \
  --title "ML Engineer RL" \
  --platform greenhouse \
  --has-screener \
  --location "New York, NY" \
  --jd-file job_description.txt

# Remote job with inline JD
job-agent add-job \
  --url "https://company.com/jobs/123" \
  --company "Acme AI" \
  --title "Applied Scientist" \
  --remote \
  --jd "We are looking for a Python/PyTorch engineer..."
```

**Platform options:** `workday` `greenhouse` `lever` `ashby` `email` `api` `custom`

Use `--has-screener` if the application has extra Q&A beyond the resume.
Use `--email-apply <address>` for email-apply jobs (sets platform=email automatically).

---

## 5. Running the overnight pipeline

```bash
# Standard run (process all DISCOVERED apps, submit auto_safe, gate gated)
job-agent run-overnight

# Dry run — score and tailor but do NOT submit anything
job-agent run-overnight --dry-run

# Limit how many jobs to process in one run
job-agent run-overnight --max-jobs 20

# Override candidate name in resume
job-agent run-overnight --candidate-name "Pranav Pradhan"
```

**What happens per job:**
1. Parse + hash the job description
2. Extract hot keywords matching your source_bank
3. Score against 6 dimensions (title, skills, industry, location, salary, nice-to-have)
4. If score < 60 → **SKIPPED** (nothing generated)
5. If score ≥ 60 → retrieve bullets, rephrase with Haiku, verify all claims
6. Render `resume.docx` + `resume.pdf` + `resume_diff.md`
7. **auto_safe jobs** (email/API) → submit immediately, save receipt
8. **gated jobs** (Workday/Greenhouse/screener) → park at WAITING_FOR_USER_APPROVAL

---

## 6. The morning digest email

```bash
# Send the digest for all pending gated applications
job-agent prep-batch --to pranavpradhan00721@gmail.com

# Dry run — print the email, don't send
job-agent prep-batch --to you@email.com --dry-run
```

The digest arrives in your Gmail as a **draft** (Claude Code creates it via MCP).
Open Gmail, find the draft, and send it to yourself — or review + send from the draft.

Each digest email has:
- A unique **DIGEST-{date}-{id}** reference in the subject
- One `APP-N` entry per pending job with score, platform, and apply URL
- Full reply instructions (see below)

---

## 7. Replying from your phone — full command reference

**Open the digest email on your phone, tap Reply, type your commands.**

---

### APPROVE — submit the application

```
APP-1 APPROVE
```

Marks `approved_by_user=1`. The next `run-overnight` will submit APP-1.
For email/API jobs this happens automatically. For browser-fill jobs,
Claude Code pre-fills the form and pauses at the submit button.

---

### REJECT — skip permanently

```
APP-2 REJECT
```

Transitions to SKIPPED. Will not appear in future digests.

---

### SKIP — skip for now

```
APP-3 SKIP
```

Same as REJECT but semantically "not now". Both go to SKIPPED state.

---

### EDIT — re-tailor with your instructions

```
APP-1 EDIT "lead with the JAX atmospheric modeling work, not Catan"
APP-2 EDIT "emphasise RL and adversarial robustness — this is a robotics role"
APP-3 EDIT "shorter bullets please, max 1 line each"
```

Re-queues the application for tailoring. Your quoted instruction is injected
into the rephrase prompt as an additional constraint — the verifier still blocks
any claim not in your source_bank.

**Rules for EDIT instructions:**
- Use double quotes around your instruction
- You can ask to re-order, re-emphasise, shorten, or change tone
- You cannot add facts that aren't in your profile — the verifier will block them
- Run `job-agent run-overnight` again to process the re-tailor

---

### SNOOZE — remind me later

```
APP-4 SNOOZE
APP-4 SNOOZE 3
```

Moves to SNOOZED state. Without a number = 1 day. With a number = N days.
The job will reappear in the next digest after the snooze period.

---

### Multiple commands in one reply

You can send all your decisions in a single reply:

```
APP-1 APPROVE
APP-2 EDIT "focus on data engineering and PySpark pipelines"
APP-3 REJECT
APP-4 SNOOZE 2
APP-5 APPROVE
```

One command per line. Order doesn't matter. Lines starting with `>` (quoted
original email) are automatically ignored.

---

### After replying

Claude Code reads your reply and processes it when you run:

```bash
job-agent check-approvals --from-gmail
```

Or to process a specific digest:

```bash
job-agent check-approvals --from-gmail --digest-id DIGEST-20260605-a3f9
```

Then run overnight again to submit approved jobs or re-tailor edited ones:

```bash
job-agent run-overnight
```

---

## 8. Checking application status

```bash
# Summary by state
job-agent status

# Full list in terminal (uses sqlite3 directly)
sqlite3 job_agent.db "SELECT id, state, score, submit_tier FROM applications ORDER BY created_at DESC LIMIT 20"
```

**Application states:**

| State | Meaning |
|-------|---------|
| DISCOVERED | Added to DB, not yet processed |
| SCORED | Score computed |
| TAILORING | Resume being tailored (or re-tailoring after EDIT) |
| RESUME_VERIFIED | Tailoring done, all claims verified |
| PACKAGING | Building artifacts |
| READY_TO_SUBMIT | Artifacts ready |
| WAITING_FOR_USER_APPROVAL | Gated — waiting for your APPROVE reply |
| SNOOZED | You snoozed it |
| SUBMITTING | In progress of being submitted |
| SUBMITTED | Successfully submitted, receipt saved |
| SKIPPED | Below threshold or you REJECTED/SKIPped it |
| FAILED | Verifier blocked it or an error occurred |
| WAITING_FOR_CAPTCHA | Paused — needs you to solve a CAPTCHA |
| WAITING_FOR_MFA | Paused — needs you to complete MFA |

---

## 9. Exporting to CSV

```bash
# Export all jobs being tracked
job-agent export-csv --view data --out data.csv

# Export submitted applications only
job-agent export-csv --view applied --out applied.csv
```

---

## 10. How the pipeline works end-to-end

```
DISCOVER (Indeed MCP / manual add-job)
    ↓
OVERNIGHT RUN
    ├── parse JD → extract keywords
    ├── score (6 dimensions, Haiku model)
    │       score < 60  →  SKIPPED
    │       score ≥ 60  →  continue
    ├── tailor resume
    │       retrieve matching source_bank bullets
    │       rephrase with Haiku (edit_instruction if re-tailor)
    │       verify: every claim must trace to source_bank
    │       render resume.docx + resume.pdf + resume_diff.md
    ├── auto_safe jobs  →  SUBMIT  →  receipt saved
    └── gated jobs      →  WAITING_FOR_USER_APPROVAL

MORNING DIGEST (prep-batch)
    └── Gmail draft created with APP-N entries + instructions

YOU REPLY FROM PHONE
    APP-N APPROVE / REJECT / SKIP / EDIT "..." / SNOOZE N

CHECK APPROVALS (check-approvals --from-gmail)
    ├── APPROVE  →  approved_by_user=1
    ├── EDIT     →  edit_instruction stored, re-queued for TAILORING
    ├── REJECT   →  SKIPPED
    └── SNOOZE   →  SNOOZED

OVERNIGHT RUN (again)
    ├── Approved gated jobs  →  SUBMIT
    └── EDIT jobs            →  re-tailor with instruction  →  new digest entry
```

---

## 11. Key files and folders

| Path | Purpose |
|------|---------|
| `.env` | Runtime config (email, DB path, log level) |
| `job_agent.db` | SQLite database — all state lives here |
| `schema.sql` | Canonical schema (source of truth) |
| `knowledge_base/profile/verified_facts.yaml` | Your career facts, metrics, tools — **edit this to add new verified facts** |
| `knowledge_base/profile/application_answers.json` | Default answers for portal forms (name, email, salary, sponsorship) |
| `knowledge_base/profile/resume_config.yaml` | Which resume files to use for tailoring |
| `applications/YYYY-MM/company_title_id/` | Per-job artifacts: resume.docx, resume.pdf, resume_diff.md, scorecard.json |
| `gmail_actions/pending/` | Queued MCP actions (Claude Code picks these up) |
| `gmail_actions/results/` | MCP results written back by Claude Code |
| `learning/application_log.md` | Auto-written notes per application |
| `pending/pending_user_verification.md` | New facts discovered that need your review |
| `rules/` | Hard policy files (truthfulness, safety, versions, approvals) |
| `hooks/` | Dev-session hooks (secret scanner, protected files guard) |

---

## 12. Troubleshooting

### "No valid commands found in reply"
- Make sure you're replying to the digest email (not forwarding or composing new)
- Commands must start with `APP-N` where N is the number shown in the email
- Check that quoted previous email lines (starting with `>`) are being stripped

### "APPROVE requires WAITING_FOR_USER_APPROVAL"
- The application may have already been processed or is in a different state
- Run `job-agent status` to check the current state

### "EDIT requires a quoted instruction"
- Bare `APP-1 EDIT` works (re-queues without instruction)
- `APP-1 EDIT "your note"` re-queues with your specific instruction injected

### Resume not tailored to the role
- Check `applications/.../resume_diff.md` to see which bank items were selected
- Add more specific skills/metrics to `verified_facts.yaml` for better matching
- Use `APP-N EDIT "emphasise X over Y"` to redirect tailoring

### Score seems low
- The heuristic scorer (no API) uses keyword matching — it's conservative
- With `ANTHROPIC_API_KEY` set in `.env`, the Haiku model scorer gives richer scores
- Check `applications/.../scorecard.json` for the dimension breakdown

### Gmail draft not appearing
- `prep-batch` uses `MCPGmailClient` which queues drafts for Claude Code to execute
- Check `gmail_actions/pending/` for queued draft actions
- Open a Claude Code session and run: `from src.gmail.mcp_executor import summarise_pending; print(summarise_pending())`

### Tests failing
```bash
make test        # full suite
make smoke       # fast subset only
pytest tests/unit/test_state_machine.py -v  # specific file
```
