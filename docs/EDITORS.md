# Editor setup — `barndsl lsp`

`barndsl lsp` is a stdlib Language Server (JSON-RPC 2.0 over stdio, zero
dependencies). It brings the compiler-as-teacher experience — live diagnostics
as you type, hover docs, id-aware completions, go-to-definition across `use`
boundaries, format-on-save and the playground's quick-fixes — to any LSP editor.

Design and capability list: [`docs/design/lsp.md`](design/lsp.md).

Smoke-test your install before wiring an editor:

```bash
barndsl lsp --check      # prints the negotiated capabilities, exits 0
```

The command reads/writes on stdin/stdout, so the launch command every editor
needs is simply `barndsl lsp` (or `python -m barndsl.cli lsp`). Associate the
`.barn` extension with a language id of `barndsl`.

## What you get

| Capability | LSP method | Notes |
|---|---|---|
| Live diagnostics | `textDocument/publishDiagnostics` | compile-per-change; accepted (audited) deviations show as Hints, part-internal findings link to the part file |
| Hover | `textDocument/hover` | statement grammar at a keyword, a fact card on a room / stamped id / instance alias, the registry explanation on an accept-pragma code |
| Completion | `textDocument/completion` | statements, room ids (incl. stamped `alias.id`), room types, the fixture catalog, wall directions, alarm kinds, `use "…"` part paths, accept codes |
| Formatting | `textDocument/formatting` | the comment-preserving `fmt`; refuses (no edits) on parse errors |
| Go to definition | `textDocument/definition` | a room id → its statement; a stamped id → the `use` line *and* the part file's room; a `use "…"` path → the part file |
| Document symbols | `textDocument/documentSymbol` | the plan outline: rooms and stamped instances (`▣ alias`) |
| Code actions | `textDocument/codeAction` | insert a hint's suggested line; *Accept CODE (audited deviation)* appends the suppression pragma |
| Rename | `textDocument/rename` | rename a room id everywhere (rejects stamped members — rename those in the part file) |

Full-document sync only (compiles are ~2 ms, so incremental sync buys nothing).

## VS Code

There is no packaged `.vsix`; use a generic LSP-client extension. With
[`llllvvuu.vscode-generic-lsp`](https://marketplace.visualstudio.com/) or the
[`ama.generic-lsp`] families, add to your `settings.json`:

```jsonc
{
  "files.associations": { "*.barn": "barndsl" },
  "genericLsp.servers": {
    "barndsl": {
      "command": "barndsl",
      "args": ["lsp"],
      "languages": ["barndsl"],
      "rootPatterns": ["*.barn"]
    }
  }
}
```

(The exact settings key depends on the generic-LSP extension you pick — every one
takes a `command`/`args` pair; use `barndsl lsp`.)

## Neovim (0.8+, built-in `vim.lsp`)

No plugin needed — start the server on `.barn` buffers with an autocmd:

```lua
vim.filetype.add({ extension = { barn = "barndsl" } })

vim.api.nvim_create_autocmd("FileType", {
  pattern = "barndsl",
  callback = function(args)
    vim.lsp.start({
      name = "barndsl",
      cmd = { "barndsl", "lsp" },
      root_dir = vim.fs.dirname(vim.api.nvim_buf_get_name(args.buf)),
    })
  end,
})
```

Format on save (optional):

```lua
vim.api.nvim_create_autocmd("BufWritePre", {
  pattern = "*.barn",
  callback = function() vim.lsp.buf.format() end,
})
```

## Helix

Add the language and server to `~/.config/helix/languages.toml`:

```toml
[language-server.barndsl]
command = "barndsl"
args = ["lsp"]

[[language]]
name = "barndsl"
scope = "source.barn"
file-types = ["barn"]
roots = []
language-servers = ["barndsl"]
auto-format = true
```

Check it loaded with `hx --health barndsl`.

## Zed

Zed speaks LSP; register `barndsl lsp` as the language server for `.barn` files
in your Zed configuration (the command is the same `barndsl lsp`). See the Zed
docs for the current per-language server binding syntax.

## Troubleshooting

- `barndsl lsp --check` prints the capabilities and exits — if that works, the
  binary is on your `PATH` and the server negotiates correctly; any remaining
  issue is in the editor's client wiring.
- Diagnostics that reference a part file (`use "parts/…"`) resolve `use` paths
  **relative to the open file's directory**, exactly like `barndsl compile`. Open
  the plan from its own folder (or a workspace rooted there) so parts resolve.
- The server logs malformed frames and internal errors to **stderr** and keeps
  running; check your editor's LSP log if something looks stuck.
