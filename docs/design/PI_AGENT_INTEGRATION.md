# Evaluation: integrating the Pi coding agent into barndsl

**Question.** Would adopting the [Pi coding agent](https://pi.dev)
([`earendil-works/pi`](https://github.com/earendil-works/pi)) improve barndsl's
agentic workflow — specifically, would it give us "better control over the
context"?

**Answer, up front: no — not as a dependency.** Pi is a strong project, but it
solves a problem barndsl's agent doesn't have, in a language barndsl isn't
written in. The parts of the *idea* that are worth acting on are cheaper to do
Python-native. Details below, plus the changes that would actually deliver the
"better context control" the request is after.

## What Pi is

Pi is a TypeScript agent toolkit, structured as layered npm packages:

- `@earendil-works/pi-ai` — a unified multi-provider LLM API (Anthropic,
  OpenAI, Google, xAI, …) with streaming, tool calling, and cross-provider
  context handoff.
- `@earendil-works/pi-agent-core` — the agent loop: tool calling plus state /
  session management.
- `@earendil-works/pi-coding-agent` — a full interactive coding-agent CLI with
  built-in file/shell tools and session persistence.
- `@earendil-works/pi-tui` — a terminal UI library.

Its headline feature — the thing that maps to "control over the context" — is
**context engineering** for long, tool-heavy sessions:

- a `context` extension event to *rewrite messages before the LLM sees them*;
- `session_before_compact` to customise summarisation when the window fills;
- `tool_call` gating to intercept tool invocations;
- a "context mode" MCP server that keeps raw tool output out of the window
  (they cite ~98% reduction, 315 KB → 5.4 KB).

Every one of those features exists to fight **context-window pressure** in
open-ended coding sessions that read files, run shells, and accumulate long
transcripts.

## What barndsl's "agent" actually is

`src/barndsl/agent.py` is **not** a coding agent. It is a bounded,
single-purpose compile-fix loop:

```
write DSL  →  compile  →  critique (optional)  →  revise
```

Concretely (see `BarndoAgent`):

- It makes plain Anthropic Python-SDK calls — `messages.stream` to generate
  DSL, `messages.parse` to get a structured `CritiqueSpec`. No tool calling.
- It never touches the filesystem or runs shell commands.
- Each turn's context is tiny and fully bounded: `brief` + *one* prior DSL
  source + *one* compiler report. It comfortably fits `max_tokens=8000`.
- The loop is capped (`max_iterations`, default 3) and terminates on
  `result.ok and critique.satisfied`.
- The domain feedback is deterministic and structured: the compiler emits
  line/column, error code, caret, and a fix hint (`validation.py`,
  `diagnostics.py`), and the critique is folded into that same diagnostic
  stream as `INFO`.

That last point is the crux: **barndsl already has excellent context control.**
The context isn't a sprawling transcript to be compacted — it's a small,
precisely-shaped payload where the compiler's diagnostics *are* the steering
signal. There is nothing to compress and no tool sprawl to gate.

## Why Pi is a poor fit here

1. **Language mismatch (the big one).** barndsl is pure Python, `pip install -e`,
   one runtime dependency (`pydantic`), CI gated on `mypy`/`ruff`/`pytest`. Pi
   is TypeScript/Node. Integrating means either (a) rewriting the agent in TS
   and shipping a Node build (npm, a second toolchain, a second CI lane), or
   (b) driving Pi as a Node subprocess over IPC. Both bolt a whole runtime onto
   a package whose selling point is that the engine needs *no* extras and the
   agent needs only the Anthropic SDK. That is a large, permanent operational
   cost.

2. **Scope mismatch.** Pi is a *general coding agent* — file tools, shell,
   sessions, a TUI. barndsl's agent is a constrained generate/critique loop with
   no tools. Adopting Pi means either importing machinery we'd never use, or
   re-architecting the loop into a tool-driven agent purely to justify the
   dependency. That's tail-wagging-dog.

3. **The problem Pi solves doesn't exist here.** Compaction, message rewriting,
   `session_before_compact`, the 98%-reduction context mode — all target window
   overflow. barndsl's prompt is a brief plus one DSL plus one report. There is
   no overflow. "Better control over the context" via Pi would be buying a
   forklift to move a single box.

4. **It would dilute the current strength.** The tight coupling between the
   compiler's structured diagnostics and the next prompt is the design's best
   idea. Routing that through a general agent harness adds indirection without
   adding signal.

## What the request is *actually* reaching for — and the Python-native way to get it

"Better control over the context" is a reasonable instinct; it's just that the
high-leverage versions are all in-language and cheap:

- **Prompt caching for the static system prompt.** `_GENERATE_SYSTEM` embeds the
  full `DSL_REFERENCE` + design rules on every call. Mark it as a cached block
  (Anthropic `cache_control`) — immediate cost/latency win, zero architecture
  change. *This is the single best "context" improvement available.*

- **Tool use, if we want genuine agency.** If the goal is to let the model
  *drive* — call `compile`, `explain <CODE>`, or `layout` mid-conversation
  instead of running a fixed loop — do it with Anthropic tool-use directly, or a
  lightweight Python agent lib. **PydanticAI** is the natural pick: the project
  already depends on pydantic and already returns a pydantic `CritiqueSpec`, so
  the schemas and the mental model carry straight over. This gives Pi-style
  agentic control without leaving Python.

- **Explicit context assembly / few-shot injection.** If context ever *does*
  grow — multi-plan sessions, few-shotting from `examples/gallery/`, longer
  critique histories — the fix is a small Python helper that assembles exactly
  the messages we want (cap history, summarise superseded iterations, inject
  only the *relevant* gallery plan). This is the same idea as Pi's `context`
  rewrite hook, done in ~30 lines with no dependency.

- **Borrow, don't bind.** Pi's genuinely good concepts — a message-rewrite seam
  before the LLM, and treating context as something you *assemble* rather than
  accumulate — are worth stealing conceptually. We don't need the package to
  apply them.

## When Pi *would* make sense

If the ambition were a different product — an interactive, chat-driven coding
agent that edits `.barn` files on disk, runs the `barndsl` CLI as a tool,
persists sessions, and works across many files — then a coding-agent harness
earns its keep. But even then, the pragmatic path is not "embed a TS framework
into a Python package." It's to expose barndsl as a clean CLI/tool (it already
has `--json` on `compile`/`build`) and let an existing agent — Claude Code, or
Pi run standalone — orchestrate it from outside. barndsl stays the sharp,
Python-native tool; the agent stays the agent. No integration required.

## Recommendation

- **Do not** take a dependency on the Pi coding agent. Language + scope mismatch,
  and it targets a context-window problem barndsl's agent doesn't have.
- **Do** add prompt caching to the static system prompt — the real, immediate
  "context" win.
- **If** more agency is wanted, evolve `agent.py` toward tool use with the
  Anthropic SDK or PydanticAI (pydantic-native, minimal), keeping the
  compiler's diagnostics as the steering signal.
- **Keep** barndsl a clean CLI/library so any external agent (including Pi,
  standalone) can drive it — that, not embedding, is the right seam for a full
  coding-agent experience later.

## Sources

- Pi coding agent — <https://github.com/earendil-works/pi>, <https://pi.dev>
- "What I learned building an opinionated and minimal coding agent" (Mario
  Zechner) — <https://mariozechner.at/posts/2025-11-30-pi-coding-agent/>
- Current implementation — `src/barndsl/agent.py`, `src/barndsl/validation.py`,
  `src/barndsl/diagnostics.py`
