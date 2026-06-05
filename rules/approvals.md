# Rule: Approvals

## Two approval tracks

### Track A — Claim approval (verifier output)
Triggered when the diff-verifier finds a claim not in `source_bank` or blocked
by `usage_level`. A record is written to the `approvals` table with
`approval_type = 'claim'`. The agent **pauses generation** and waits for human
resolution. The human may:
- Add the claim to `source_bank` + `verified_facts.yaml` (unblocks it)
- Reject the claim (agent must remove it from the output)

### Track B — Submit approval (gated applications)
Every `gated` application requires an explicit APPROVE command before submission.
The morning digest email contains one entry per pending gated application.
Valid reply commands:

| Command | Effect |
|---------|--------|
| `APPROVE` | Sets `approved_by_user = 1`; state → `SUBMITTING` |
| `REJECT`  | Sets `should_apply = 0`; state → `SKIPPED` |
| `SKIP`    | State → `SKIPPED` (revisitable) |
| `EDIT`    | State → `TAILORING`; resume re-tailored |
| `SNOOZE`  | State → `SNOOZED` (re-appears next morning) |
| `DONE`    | Marks entire digest as processed |
| `MANUAL`  | Flags for manual handling; no auto-action |

## What the agent may NOT do

- Set `approved_by_user = 1` on its own.
- Interpret silence as approval.
- Advance a `gated` application past `WAITING_FOR_USER_APPROVAL` without a
  resolved `approvals` record (`status = 'approved'`).
- Auto-approve a blocked claim under any circumstance.

## Pending facts flow

When the learning module discovers a potential new personal fact:
1. It writes the fact to `pending/pending_user_verification.md` with context.
2. It does **not** add it to `source_bank` or `verified_facts.yaml`.
3. It does **not** use the fact in any generated output.
4. Only after the human explicitly approves (outside the agent) may the fact
   be promoted to `verified_facts.yaml` and seeded into `source_bank`.
