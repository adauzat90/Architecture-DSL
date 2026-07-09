# ADR 0001 — Repo-native developer harness

- Status: Accepted
- Date: 2026-07-09

## Context

Barndsl has multiple surfaces that must stay aligned: compiler, diagnostics, examples, scoring, exports, LSP, playground, and pi agent tools. If helper logic only lives in an external agent extension, CI and humans cannot rely on it; if it only lives in ad-hoc shell commands, agents cannot consume it deterministically.

## Decision

Keep developer/agent helpers in `src/barndsl/devtools.py` as stdlib-only, JSON-shaped functions. Expose them through `barndsl dev ...` CLI commands and let pi custom tools call the same repo-native implementation.

## Consequences

- Humans, CI, and agents share one source of truth for audit, rule probes, diagnostic diffs, gallery gates, LSP smoke tests, impact targets, feature checks, fixture catalogs, and diagnostic matrices.
- Tool output should remain deterministic JSON by default; Markdown output is allowed for generated docs/checklists.
- Pi extension code should be a thin transport/formatting layer, not the place where barndsl rules are implemented.
- New harness features should usually start in `devtools.py`, then be wired into `src/barndsl/cli.py`, tests, docs, and optionally `.pi/extensions/barndsl-harness.ts`.
