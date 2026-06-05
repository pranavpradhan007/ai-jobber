# Rule: Safety

## Submission safety

### `auto_safe` tier (email / API / simple form)
- Submit automatically after `verifier_passed = 1`.
- Log the receipt; set state → `SUBMITTED`.
- Never require human approval (by design — these are low-risk paths).

### `gated` tier (screener / login-required / Workday / Greenhouse / Lever / Ashby)
- **Never submit without `approved_by_user = 1`.**
- State transitions to `WAITING_FOR_USER_APPROVAL`.
- The morning digest email presents all pending gated applications.
- Only after the user replies APPROVE (and `approved_by_user` is set) may the
  agent proceed to `SUBMITTING`.
- The state machine's `SUBMITTING` invariant enforces this structurally:
  ```
  assert verifier_passed == 1
  assert auto_safe == 1 OR approved_by_user == 1
  ```

## CAPTCHA and MFA

- **The agent never attempts to solve CAPTCHA or MFA.**
- On detection: transition to `WAITING_FOR_CAPTCHA` or `WAITING_FOR_MFA`.
- Notify the user; halt all further automation for that application.
- No browser automation code may call a CAPTCHA-solving service or simulate
  "I am not a robot" interaction.

## Secret / credential policy

- All credentials (Gmail OAuth tokens, API keys, SMTP passwords) live **only**
  in `.env` (which is gitignored) or in an OS keychain.
- Code reads them via `os.environ` / `python-dotenv` — never hardcoded strings.
- `secret_scanner.py` runs as a pre-tool hook and blocks any file-write containing
  patterns matching `password`, `token`, `api_key`, `secret`, `oauth`, etc.
  followed by an assignment with a non-empty value.
- Logs, CSV exports, `audit_log`, and `state_transitions` must never contain
  credential values. This is verified by the security audit each sprint.

## Anti-bot evasion

- The browser automation module may not use:
  - Random mouse jitter or delay injection intended to defeat bot detection
  - User-agent spoofing beyond a realistic browser UA
  - Canvas or WebGL fingerprint masking
  - Any library whose primary purpose is anti-bot evasion (e.g. puppeteer-extra-plugin-stealth)
- Standard Playwright Chromium with a real user session is permitted.
- Rate limits must be respected: minimum 2-second delay between form field fills.
