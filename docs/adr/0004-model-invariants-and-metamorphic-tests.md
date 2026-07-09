# ADR 0004 — Preserve model invariants with metamorphic tests

- Status: Accepted
- Date: 2026-07-09

## Context

Many barndsl regressions are not isolated to one parser branch or validator. A change to coordinates, ID stamping, fragment compilation, formatting, or emission can break LSP navigation, direct edits, exports, and agent repair loops even when narrow unit tests still pass.

## Decision

Document cross-cutting model contracts in `docs/MODEL_INVARIANTS.md` and maintain metamorphic tests for the highest-value fixed points:

- formatter idempotence: `fmt(fmt(src)) == fmt(src)`;
- whole-plan emission fixed points;
- fragment emission fixed points;
- flattened composition preserving resolved/stamped room geometry;
- diagnostic registry category/owner coverage.

## Consequences

- Architecture changes that intentionally alter these contracts should update the invariants doc, affected ADRs, and tests together.
- Tests should assert stable properties rather than brittle full-output snapshots unless a snapshot is intentionally normalized.
- New composition, formatter, emitter, or diagnostic-registry features should add at least one metamorphic assertion when possible.
