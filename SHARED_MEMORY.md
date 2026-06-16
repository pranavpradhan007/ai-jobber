# JobberAI — Shared Session Memory

Claude Code appends an entry here at the end of every session. Most-recent first.

---

## 2026-06-15 (session 2) — Auto Apply persistence + country/demographic fixes

**What was done:**
- `content.js` — Auto Apply: added `verifyAndRetryEmpty()` — after fill, re-scans DOM for empty required fields, retries with profile/memory/Claude answers, **blocks submit** if required fields still empty; shows which fields need manual input. `fillCurrentPage()` now returns `claudeAnswers` so retry pass can use them.
- `autofill.js` — Added `getFieldDomValue(field)`, `isFieldRequired(field)`, `resolveAnswerForRetry(field, …, claudeAnswers)` helper functions used by the retry loop.
- `autofill.js` / `form_detector.js` — Fixed "what is your location?" country select: label extraction now strips nested select/input text so options list doesn't pollute label; `address_country` now mapped in LABEL_ALIASES + QUESTION_BANK; `fillSelect()` auto-detects 50+ option country dropdowns and selects "United States".
- `autofill.js` — `fillSelect()` fallback: if value doesn't match any option but select has 50+ options including "United States", selects it (prevents "New York" being set on a country dropdown).
- `sidepanel.js` + `content.js` — Manual edit push-to-DOM: when user edits a field value in sidepanel and blurs, sends `FILL_FIELD` to content script which re-fills the actual form element (select, radio, input) — so sidepanel edits actually stick.
- `proxy_server.py` — Tighter Gmail receipt count query: `in:inbox`, exact `subject:` phrases, `-subject:newsletter -subject:update -subject:unsubscribe` exclusions to reduce 201→actual count.

**Key pending issues:**
- Pronouns: `he/him` showing as "true" (checkbox), `she/her`/`they/them`/`xe/xem` sent to Claude → empty strings returned (correct — don't check those). Visually check if he/him checkbox is actually checked in DOM.
- Lever Google Places current location field: `fillAutocomplete()` types city name → waits 1.4s → clicks `.pac-item`. Still inconsistent if Places API is slow.
- Resume file upload: not handled (file input needs DataTransfer API).

---

## 2026-06-15 — Chrome Extension build (Phase 1 complete)

**What was done:**
- Shut down and fully purged LinkedIn: deleted linkedin_browser.py, linkedin_email.py, linkedin_apply.py, run_linkedin_discovery.py, startup_job_agent.bat, JobAgent.bat (boot startup), 499 LinkedIn jobs/apps from DB, 238 application folders. LinkedIn caused an account restriction warning.
- Fixed 3 apps stuck in TAILORING state (398, 489, 494) by resetting to DISCOVERED in DB and adding startup recovery code to overnight.py + generic exception handler.
- Removed LinkedIn EA logic from auto_submit.py, continuous.py, run_continuous.py.
- **Built JobberAI Chrome Extension** — full Phase 1 under `E:/job search/extension/`:
  - `manifest.json` — MV3, all_urls, sidePanel, identity
  - `content/form_detector.js` — direct port of `_JS_EXTRACTOR` (Shadow DOM, Workday-safe)
  - `content/autofill.js` — full LABEL_ALIASES + QUESTION_BANK port, 4-tier resolution, React-friendly fill
  - `content/login_handler.js` — auto-login/signup on any portal, deterministic passwords per domain
  - `content/email_verify.js` — Gmail API polling for verify links + OTP extraction
  - `content/content.js` — main orchestrator, status overlay, AUTOFILL_START/NEXT_PAGE/SUBMIT_FORM handlers
  - `background/service_worker.js` — Claude Drafter → Claude Reviewer → Auto Reviewer, learning memory, Gmail proxy, ATS checker, cover letter generator, rate-limit management
  - `popup/` — Autofill button, proxy status check, stats
  - `sidepanel/` — editable field review, ATS keyword gap, cover letter generator, Next/Submit buttons
  - `options/` — API key, profile editor, resume upload (PDF→base64), memory viewer, applications tracker
  - `data/profile.json` — copy of application_answers.json
  - `icons/` — placeholder PNGs (16/48/128)
  - `proxy_server.py` + `start_proxy.bat` — local HTTP proxy that reads ANTHROPIC_API_KEY from .env, so extension uses Claude Code's key without storing it in Chrome

**API key setup:**
- Extension uses a local proxy (`extension/proxy_server.py`) on `localhost:3747`
- Proxy reads `ANTHROPIC_API_KEY` from `.env`
- User runs `extension/start_proxy.bat` once before using the extension
- No API key stored in Chrome — all stays local

**How to load extension:**
1. `chrome://extensions` → Developer mode ON → Load unpacked → select `E:/job search/extension`
2. Run `extension/start_proxy.bat` (keep window open)
3. Add `ANTHROPIC_API_KEY=sk-ant-...` to `.env`
4. Go to any job application → click JobberAI icon → Autofill

**Remaining (Phase 2/3):**
- Gmail OAuth token flow for email verification (currently needs manual token paste in options)
- Memory consolidation (every 50 fills, Claude dedupes/improves memory)
- Cover letter fill-to-field connector
- Full test on Greenhouse/Ashby/Workday pages

---
