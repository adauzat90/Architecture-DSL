# `barndsl lsp` — a stdlib Language Server

Status: **design approved** · not yet implemented
Prereqs shipped: sub-2ms compiles, `Issue.file/part/accepted` fields, `fmt`
(comment-preserving), the parts scanner (`scan_parts`), `DSL_REFERENCE`,
`rename_room` in the surgical-edit engine.

## 1. Goal

Bring the compiler-as-teacher experience to any LSP editor — VS Code, Neovim,
Helix, Zed — without the playground: live teaching diagnostics as you type,
hover docs for every statement, completions that know your room ids and part
files, go-to-definition across `use` boundaries, format-on-save, and the same
quick-fixes the playground's Apply button offers.

**Non-goals (v1):** incremental text sync (full-document sync is fine at 2ms
compiles), semantic tokens, workspace-wide background indexing, a packaged
VS Code extension (`.vsix`), TCP/socket transports, multi-root workspaces.

## 2. Core decisions

1. **Stdlib only.** No `pygls`, no dependency. LSP is JSON-RPC 2.0 over stdio
   with `Content-Length` framing — ~60 lines of transport code. barndsl's
   zero-dependency ethos holds.
2. **Compile per change, synchronously.** Measured 1.9 ms per full compile of
   the two-story gallery plan. `textDocument/didChange` (full sync,
   `TextDocumentSyncKind.Full = 1`) simply recompiles and republishes. Before
   compiling, drain any already-buffered `didChange` for the same document and
   keep only the newest (cheap coalescing without timers).
3. **Features are pure functions; the protocol is a shell.** Every capability
   lives in `src/barndsl/lsp.py` as a testable function taking
   `(text, base_dir, position…)` and returning plain dicts shaped like LSP
   types. The stdio loop only frames, dispatches, and replies. Tests hit the
   pure functions directly; one subprocess test proves the wire format.
4. **`base_dir` from the document URI.** `file:///…/plan.barn` →
   `dirname` — `use` resolution, the parts scanner, and part-file
   diagnostics all work exactly as in `compile_file`. Untitled buffers get
   `base_dir=None` and the existing `USE_UNRESOLVED` teaching diagnostic.

## 3. Capabilities (v1)

### 3.1 Diagnostics (`textDocument/publishDiagnostics`)
Map each `Issue` to an LSP Diagnostic:
- `range`: `(line-1, col-1)`–`(line-1, end_col-1)`; missing col → whole line;
  missing line → line 0. LSP is 0-based UTF-16; barndsl sources are
  overwhelmingly ASCII but the prime glyphs (′ ″) are BMP so UTF-16 == code
  points; document the assumption.
- `severity`: ERROR→1, WARNING→2, INFO→3, **accepted→4 (Hint)** with
  `tags: [1] (Unnecessary)` NOT set (it dims text) — accepted findings keep
  their `(accepted: "reason")` suffix in the message instead.
- `code`: the registry code; `codeDescription` omitted (no public URL);
  `source`: `"barndsl"`.
- `message`: message + `\n` + hint (hints are the teaching voice — they
  belong in the squiggle popup).
- **Part-internal findings** (Issue.file set): attach
  `relatedInformation = [{location: {uri: file://<part>, range: <part line>},
  message: "declared here"}]` and anchor the primary range at the host's first
  `use` line for that part (same fallback the playground uses).

### 3.2 Hover (`textDocument/hover`)
- On a statement keyword (first word of the line): the statement's entry from
  `DSL_REFERENCE` (grammar line + its comment block), fenced as markdown.
- On an identifier that resolves in the compiled plan: a fact card —
  room: `bed — bedroom · 14′ × 11′ · 154 sq ft · level 0`; fixture/device:
  kind + room + position; instance alias: part path + transform + bbox;
  stamped id (`m.bed`): the card plus `stamped from parts/master_suite.barn`.
- On a diagnostic code inside an accept pragma comment: the registry title +
  explanation (the `barndsl explain` text).

### 3.3 Completion (`textDocument/completion`)
Context-keyed on the text before the cursor (reuse/extract the playground's
autocomplete context rules rather than inventing new ones — check how the
editor's autocomplete decides context and share tables where practical):
- line start → statement keywords;
- after `door` / `open` / `in` / an id slot → room ids (including stamped
  `alias.id`) from the last good compile;
- after `wall` → `N S E W`; after `fixture` → the 21-kind catalog; after
  `alarm` → smoke/co/smoke_co; window/door kinds in their slots;
- after `use "` → part relpaths from `scan_parts(base_dir)`;
- after `# barndsl: accept ` → registry codes.
Items carry `detail` (one-line doc) and `kind` (Keyword/Value/File/Class).

### 3.4 Formatting (`textDocument/formatting`)
`format_source` (the comment-preserving normalizer) as a single full-document
`TextEdit`. Refuse (empty edit list) when the source has parse errors, matching
`fmt`'s CLI contract.

### 3.5 Definition (`textDocument/definition`)
- Room/fixture/device id reference → its declaring statement line in the same
  document.
- Stamped id `m.bed` → **two locations**: the `use` line (host) and the
  `room bed` line inside the part file (`file://` URI) — editors show a picker.
- Part path inside a `use` string → the part file.

### 3.6 Document symbols (`textDocument/documentSymbol`)
Outline from the last good compile: plan name at root; rooms (with ranges from
their statement lines), openings, fixtures group, electrical group, notes,
instances (`▣ alias`). Hierarchical `DocumentSymbol[]`.

### 3.7 Code actions (`textDocument/codeAction`)
Port the playground's quick-fix heuristic to a shared Python function
(`quickfix_snippet(hint) -> str | None`: a backticked snippet whose first word
is a statement keyword and which carries no placeholder). For each diagnostic
in range with a snippet: a QuickFix action inserting the snippet as a fresh
line after the diagnostic's line. Plus one action per WARNING/INFO diagnostic:
**"Accept CODE (audited deviation)"** appending
`  # barndsl: accept CODE ""` to the offending line — the LSP twin of the
pragma workflow. The playground's `quickFixSnippet` JS should be regenerated
from or verified against the shared function to prevent drift (a test pinning
both behaviors on the same fixtures is enough).

### 3.8 Rename (`textDocument/rename`) — optional, include if clean
On a room id: run the `rename_room` surgical edit; return a `WorkspaceEdit`
replacing the full document (the edit engine already rewrites every
reference). `prepareRename` rejects non-room identifiers and stamped members
(teaching message: rename inside the part file). If the edit engine's rename
doesn't map cleanly, defer the whole capability — do not half-do it.

## 4. Server shape

```
src/barndsl/lsp.py
  read_message(stream) / write_message(stream, obj)   # framing
  class DocumentStore                                  # uri -> (text, version)
  class Server: dispatch table, initialize/shutdown/exit lifecycle
  pure feature functions: diagnostics(), hover(), completions(),
    definition(), symbols(), code_actions(), rename()
barndsl lsp        # CLI subcommand: Server(stdin.buffer, stdout.buffer).run()
```

- `initialize` → advertise exactly the capabilities above (full sync, no
  workspace folders needed; store `rootUri` only as a fallback base_dir).
- Unknown methods → JSON-RPC "method not found" for requests, silently ignored
  for notifications. `$/cancelRequest` ignored (everything is fast).
- Compile results cached per document version: hover/completion/definition/
  symbols reuse the last compile instead of recompiling per request.
- Malformed frames or JSON must not kill the server: log to stderr, continue.

## 5. Docs

`docs/EDITORS.md`: wiring for VS Code (via a generic LSP client extension,
with the settings JSON), Neovim (`vim.lsp.start` autocmd snippet), and Helix
(`languages.toml`). A `barndsl lsp --check` flag that prints the negotiated
capabilities and exits (smoke test for editor configs).

## 6. Test plan

- Framing: round-trip odd sizes, split reads, unicode content, bad JSON.
- Lifecycle: initialize/initialized/shutdown/exit ordering; unknown method.
- Every feature function: house-style pytest on fixtures — diagnostics mapping
  (severity, ranges, accepted→Hint, part relatedInformation), hover cards,
  each completion context, definition targets incl. cross-file, symbols tree,
  quick-fix + accept actions, formatting parity with `fmt`, rename parity with
  the playground's rename edit.
- One end-to-end subprocess test: spawn `barndsl lsp`, initialize, open a doc
  with a known warning, assert the publishDiagnostics notification, hover,
  then shut down cleanly. Keep it under a few seconds.
- The playground/JS quick-fix parity test (§3.7).

## 7. Alternatives considered

- **pygls / lsprotocol dependency** — rejected: barndsl ships zero runtime
  dependencies; the needed protocol subset is small and stable.
- **Debounced async compiles** — rejected: 2ms compiles make coalescing-by-
  draining sufficient; no threads, no timers, no races.
- **TCP transport** — deferred: stdio covers every target editor.
- **Publishing diagnostics for closed part files** — deferred: related
  information on the host's diagnostics covers the workflow; pushing to
  unopened URIs surprises editors.
