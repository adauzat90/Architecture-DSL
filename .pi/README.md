# pi harness additions for Architecture-DSL

This project-local pi extension is auto-discovered from `.pi/extensions/` after the project is trusted and pi is reloaded.

Tools added by `.pi/extensions/barndsl-harness.ts`:

- `barndsl_compile` — UTF-8-safe `barndsl compile`, with JSON/metrics/coords options.
- `barndsl_inspect` — resolved geometry/free-span dump for spatial repairs; supports room/free-span/adjacency filters.
- `barndsl_explain` — diagnostic-code explanation lookup.
- `barndsl_render_preview` — builds SVG/PNG/PDF artifacts under `.pi/artifacts/` by default.
- `barndsl_test` — pytest runner with `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, and `PYTHONPATH=src`.
- `barndsl_trace_analyze` — summarizes agent JSONL traces.
- `barndsl_edit` — structured `.barn` source edits via `barndsl.edits`.
- `barndsl_repo_audit` — checks rule/feature wiring drift: diagnostic registry, statement keywords, DSL reference, and artifacts.
- `barndsl_rule_probe` — compiles inline source and asserts expected/absent diagnostic codes.
- `barndsl_diagnostic_diff` — compares diagnostic-code counts across plan sets.
- `barndsl_gallery_gate` — lightweight examples/gallery diagnostic gate.
- `barndsl_doctor` — one-shot maintainability gate: audit, gallery scan, strict LSP smoke, impact targets/tests, and optional export parity.
- `barndsl_feature_check` — verifies a feature/statement is wired across parser/docs/playground/LSP/tests.
- `barndsl_export_parity` — smoke-tests SVG/glTF/IFC/packet exports for a plan using the repo-native `barndsl dev export-parity` implementation.
- `barndsl_lsp_smoke` — smoke-tests LSP diagnostics, hover, completions, formatting, code actions, symbols, and composed-id behavior.
- `barndsl_impact_tests` — maps changed files to likely pytest targets and can run them.
- `barndsl_rule_scaffold` — checklist and optional test skeleton for a new diagnostic rule.
- `barndsl_feature_scaffold` — checklist artifact for a new DSL feature/statement.

Also adds `/barndsl-status` as a quick extension/CLI smoke check.

The same helpers are also available to non-pi tooling through `barndsl dev ...` subcommands (`audit`, `rule-probe`, `diag-diff`, `gallery-gate`, `lsp-smoke`, `doctor`, `feature-check`, `impact`, `export-parity`, `rule-scaffold`, `feature-scaffold`).

Reload pi with `/reload` after pulling these files. If Python resolution is unusual, set `BARNDSL_PYTHON` to the interpreter that has the repo's dev dependencies installed; otherwise the extension prefers `.venv`, then pyenv-win's active `python.exe`, then `python` on PATH.
