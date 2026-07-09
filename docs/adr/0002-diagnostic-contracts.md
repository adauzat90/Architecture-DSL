# ADR 0002 — Diagnostics are registry-backed, matrix-audited contracts

- Status: Accepted
- Date: 2026-07-09

## Context

Validation rules are part of the user-facing language. A diagnostic code appears in CLI output, LSP quick fixes, playground guidance, tests, accepted-deviation pragmas, examples, and agent prompts. Renaming or adding a rule without registry/docs/tests makes the system harder to repair and easier for agents to misuse.

## Decision

Treat each diagnostic code as a contract:

- The code must be registered in `src/barndsl/diagnostics.py` with severity, category, owner, title, explanation, and actionable hint.
- Emitters should use stable literal codes where practical so static harness tools can locate them.
- Tests should include a firing case and a satisfied case for behavior changes.
- Example/gallery impact should be reviewed before tightening or loosening a rule.
- Use `barndsl dev diag-matrix` to inspect registry, category/owner, emitter, test, docs, and example breadcrumbs before changing a rule.

## Consequences

- Some dynamic or compatibility diagnostics may show `no_literal_emitter`; that is an investigation prompt, not automatically a failure.
- The generated `docs/DIAGNOSTIC_MATRIX.md` is a navigation artifact. Regenerate it after substantial diagnostic work rather than hand-editing rows.
- New rules should start with `barndsl dev rule-scaffold CODE`, iterate with `barndsl dev rule-probe`, and finish with `barndsl dev audit` plus targeted tests/gallery checks.
