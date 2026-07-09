# ADR 0003 — Maintain curated .barn fixtures for feature and rule work

- Status: Accepted
- Date: 2026-07-09

## Context

Large ad-hoc plans in tests are expensive to understand and easy to make brittle. Barndsl already has valuable examples covering whole plans, composed plans, headerless parts, nested/parametric `use`, multi-level loft/stair layouts, accepted diagnostics, and export parity. Agents need a quick way to choose the smallest fixture that exercises a behavior.

## Decision

Maintain a generated fixture catalog with `barndsl dev fixtures` and keep `docs/FIXTURE_CATALOG.md` as the human-readable index. Prefer cataloged fixtures for tests and agent prompts before inventing new large plans.

## Consequences

- Tests should use the smallest fixture that proves the behavior: inline minimal source for parser/diagnostic probes, composed hosts for stamped-id behavior, parts for fragment semantics, and `examples/gallery/lshape.barn` for export parity.
- Adding a new representative example should update the catalog purpose/role selection in `src/barndsl/devtools.py` if it becomes a canonical fixture.
- The catalog should compile fragments as fragments, so headerless parts do not create false whole-plan diagnostics.
