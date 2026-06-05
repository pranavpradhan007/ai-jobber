# Rule: Truthfulness

## Core invariant

**Every claim in every agent-generated sentence must trace to an item in
`source_bank`.** There are no exceptions.

A "claim" is any:
- Metric or quantified result (e.g. "reduced latency by 40%")
- Tool, technology, or framework name (e.g. "Kubernetes", "dbt")
- Job title or seniority level
- Company name (as former employer)
- Credential, degree, or certification
- Keyword that implies a specific technical capability

## What the diff-verifier checks

For each output sentence it:
1. Extracts all claims of the above types (using the LLM extractor, mocked in tests).
2. Looks each claim up in `source_bank` (exact or fuzzy match).
3. Checks that `usage_level` permits the target surface:
   - `private_never_submit` → **blocked on every surface, no exception**
   - `resume` → permitted on `resume`, `cover_letter`, `screener`
   - `cover_letter` → permitted on `cover_letter`, `screener` only
   - `screener` → permitted on `screener` only
4. If a claim has no bank item → `allowed = 0`, `needs_approval = 1`, written to `claims`.
5. If the claim is high-stakes (metric or credential) → creates a pending `approvals` record.
6. Sets `verifier_passed = 1` **only when every claim in the batch is allowed**.

## Hard gate

The state machine calls `verifier_hard_gate(application_id)` before any
`SUBMITTING` transition. It queries `claims` for the application and raises
`VerifierGateError` if any row has `allowed = 0`. This is uncircumventable.

## What the agent may NOT do

- Generate metrics, tools, titles, or credentials that are not in `source_bank`.
- Rephrase a sentence in a way that changes the factual meaning of a claim.
- Approve its own blocked claims (only the human can resolve via the approvals flow).
- Promote items from `pending/pending_user_verification.md` to `source_bank`
  or `verified_facts.yaml` without explicit user instruction.
