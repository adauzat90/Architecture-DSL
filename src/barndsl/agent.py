"""Claude-powered agentic workflow built around the compiler.

The agent *writes architecture in the DSL*. Each round it emits DSL source, the
compiler returns diagnostics (errors + fix hints), and those diagnostics are fed
straight back as the next prompt — a compile-fix loop, exactly like a developer
iterating against compiler output:

    1. **write**    DSL source from the brief (+ prior source + diagnostics);
    2. **compile**  → plan + diagnostics (line, code, hint) + a deterministic
       0-100 design score (see :mod:`barndsl.score`) + the resolved geometry
       pack (see :mod:`barndsl.introspect`);
    3. **critique** the design for quality (optional, model-driven, anchored to
       the score evidence — with the rendered floor-plan image attached when
       the optional ``cairosvg`` raster dependency is installed, so the critic
       judges the drawing, not just the text);
    4. **revise**   feed the structured diagnostics + score + critique back,
       rewrite the DSL, repeat — until it compiles clean, the critic is
       satisfied AND the score clears ``target_score`` (or the cap is hit).

The loop hill-climbs on the score: every step is scored, and ``design()``
returns the **best-scoring** iteration, not the last — a regression on the
final round is never silently returned.

Requires ``anthropic`` and ``ANTHROPIC_API_KEY``. Install ``pip install 'barndsl[agent]'``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .compiler import DSL_REFERENCE, CompileResult, compile_source
from .compiler import _KEYWORDS as _STMT_KEYWORDS
from .introspect import plan_summary, summary_text
from .score import ScoreReport, design_score
from .validation import Issue, Severity

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"

#: Environment variable that overrides the model everywhere (CLI ``design`` and
#: the playground's ``/api/design``). Set this when the endpoint is not native
#: Anthropic — e.g. ``ANTHROPIC_BASE_URL`` points at DeepSeek's compat gateway,
#: where ``claude-opus-4-8`` does not resolve and silently returns empty output.
MODEL_ENV_VAR = "BARNDSL_MODEL"


def resolve_model(model: str | None = None) -> str:
    """The model to drive the agent with: explicit arg, else ``$BARNDSL_MODEL``, else the default.

    An explicit ``model`` argument always wins. Otherwise the ``BARNDSL_MODEL``
    environment variable is consulted (so a non-Anthropic endpoint can pick a
    model the gateway actually serves), falling back to :data:`DEFAULT_MODEL`.
    Read at call time, not import time, so setting the var before a run takes.
    """
    return model or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL


#: The per-call output-token cap for generation and critique. It bounds thinking
#: **plus** the answer, so a reasoning model that thinks heavily needs generous
#: headroom: ``deepseek-v4-pro`` can burn 8000+ tokens thinking about a single
#: plan, and at the old 8000 cap the reply hit ``stop_reason=max_tokens`` mid-
#: thought and returned no DSL at all. 32000 leaves room to think and still emit.
#: It is a cap, not a target — non-reasoning models (e.g. Opus) stop well under
#: it and pay only for what they generate.
DEFAULT_MAX_TOKENS = 32000

#: Environment variable that overrides :data:`DEFAULT_MAX_TOKENS` — raise it for a
#: model that reasons even more, or lower it to bound cost on a terse one.
MAX_TOKENS_ENV_VAR = "BARNDSL_MAX_TOKENS"


def resolve_max_tokens(max_tokens: int | None = None) -> int:
    """The per-call token cap: explicit arg, else ``$BARNDSL_MAX_TOKENS``, else the default.

    An explicit argument wins; otherwise the ``BARNDSL_MAX_TOKENS`` environment
    variable is used when it is a positive integer. Anything missing or malformed
    (non-numeric, ``<= 0``) falls back to :data:`DEFAULT_MAX_TOKENS`, so a stray
    value can never silently produce a zero/negative cap that the API would reject.
    """
    if max_tokens is not None:
        return max_tokens
    raw = os.environ.get(MAX_TOKENS_ENV_VAR)
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_TOKENS
        if value > 0:
            return value
    return DEFAULT_MAX_TOKENS

#: Below this score the design is not "done" even if the critic is satisfied.
DEFAULT_TARGET_SCORE = 90.0

#: Environment variable overriding :data:`DEFAULT_TARGET_SCORE` — the score gate
#: the loop hill-climbs toward. Lower it for a cheaper, "good enough" run.
TARGET_SCORE_ENV_VAR = "BARNDSL_TARGET_SCORE"

#: Default number of write → compile → critique → revise rounds.
DEFAULT_MAX_ITERATIONS = 3

#: Environment variable overriding :data:`DEFAULT_MAX_ITERATIONS`. More rounds =
#: more chances to converge (and more cost); fewer = faster, cheaper.
MAX_ITERATIONS_ENV_VAR = "BARNDSL_MAX_ITERATIONS"

#: Sentinel for "argument not supplied" where ``None`` is itself a meaningful
#: value — ``target_score=None`` disables the gate, so it can't double as "unset".
_UNSET: Any = object()


def resolve_target_score() -> float:
    """The default score gate: ``$BARNDSL_TARGET_SCORE`` else :data:`DEFAULT_TARGET_SCORE`.

    A malformed value (non-numeric) falls back to the default. Note ``0`` is a
    valid gate that the loop can never fall below, i.e. effectively disabled —
    the same convention the CLI's ``--target-score 0`` uses.
    """
    raw = os.environ.get(TARGET_SCORE_ENV_VAR)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_TARGET_SCORE


def resolve_max_iterations() -> int:
    """The default round cap: ``$BARNDSL_MAX_ITERATIONS`` else :data:`DEFAULT_MAX_ITERATIONS`.

    Missing or malformed (non-numeric, ``<= 0``) falls back to the default, so a
    stray value can never produce a zero/negative cap that would skip the loop.
    """
    raw = os.environ.get(MAX_ITERATIONS_ENV_VAR)
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_MAX_ITERATIONS


#: The one-liner both the CLI and the playground surface when the agent can't run.
AGENT_INSTALL_HINT = "pip install 'barndsl[agent]' and set ANTHROPIC_API_KEY"


def agent_availability() -> tuple[bool, str | None]:
    """Whether the Claude agent can run here — ``(available, reason)``.

    Two gates, checked in order: the optional ``anthropic`` package must import,
    and ``ANTHROPIC_API_KEY`` must be set. Returns ``(True, None)`` when both
    hold, otherwise ``(False, <actionable reason>)``. The key's *value* is never
    read into the reason or returned anywhere — only its presence is probed — so
    this is safe to serve to a browser. Shared by the CLI ``design`` command and
    the playground's ``/api/agent`` probe so the two never diverge.
    """
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, f"the agent extra is not installed — {AGENT_INSTALL_HINT}"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, f"ANTHROPIC_API_KEY is not set — {AGENT_INSTALL_HINT}"
    return True, None

_DESIGN_RULES = """\
DESIGN RULES the compiler enforces (write DSL that satisfies them):
- Rooms must stay inside the envelope and must not overlap. Tile the footprint
  with little waste; barndominiums are a single rectangle.
- An interior `door` only connects two rooms that SHARE A WALL. Plan adjacencies
  so every room is reachable from an `entry` through interior doors (use a
  hallway or open plan to connect spaces — never strand a room).
- Bedrooms: >= 70 sq ft, smallest side >= 7 ft, each needs an egress `window`
  (or its own `entry`) on a wall that lies on the envelope edge.
- Provide at least one bathroom, sited near the bedrooms.
- Habitable rooms (living/kitchen/dining/bedroom/office/loft) need windows
  totalling >= 8% of their floor area, so put them on exterior walls.
- At least one `entry` must be >= 2.67 ft (an egress door). Hallways >= 3 ft.
- Idiomatic barndo: open-concept living/kitchen/dining, plus a shop/garage bay.
"""

#: Placement craft distilled from the authoring guide. The grammar reference
#: says what is *legal*; this says what *works* — the anchor rule and the
#: tile-then-connect idiom are where a model that free-hands `at x,y`
#: coordinates loses whole iterations to overlap and shared-wall errors.
_PLACEMENT_CRAFT = """\
HOW TO PLACE ROOMS (craft that keeps plans compiling first try):
- PREFER RELATIVE ANCHORS over `at x,y`. Abutting rooms automatically share a
  wall — exactly what an interior `door` requires. Hand-placed coordinates are
  where overlap and no-shared-wall errors come from (one foot off, or touching
  only at a corner, shares nothing).
- THE ANCHOR RULE — an anchor sets BOTH coordinates: `east-of`/`west-of` butt
  that wall and COPY the reference room's y; `north-of`/`south-of` stack on
  that wall and COPY its x. Chaining in one direction (a row or a column) is
  safe; branching a second column off a room already off the spine collides.
  For a hallway spine, hang EVERY served room directly off the spine, one room
  deep. `align near|far|center` / `offset <n>` slide a room along the shared
  wall; two anchors (one horizontal + one vertical) pin a corner.
- TILE, THEN CONNECT. Place rooms so neighbours abut and the envelope fills
  with little waste; then add one `door` per adjacency people actually walk.
- EGRESS FIRST. Give every bedroom its exterior-wall window as you place it —
  a missing egress window is the most common hard error.
- Habitable rooms (living/kitchen/dining/bed/office) go on the PERIMETER so
  they can take real windows; bury halls, baths, closets, storage inside.
- A bath gets a `door` (privacy); use `open` for kitchen/living/dining flow.
- Doors read best swung `into` the room they serve, hinged near a corner
  (`into <room> hinge near`), and backed to the wall's end with `offset`.
"""

#: A complete plan that compiles 0 errors / 0 warnings / 0 infos and scores
#: 100/100 — pinned by a test so it can never rot against the grammar. One
#: worked example anchors the output format better than any instruction,
#: especially for non-Claude models driven through a compat gateway.
_EXAMPLE_PLAN = """\
plan "Maple Two-Bed"
envelope 51 x 30
ceiling 10
program 2 bed 1 bath

room living:  living   at 0,0            size 20 x 30
room kitchen: kitchen  east-of living    size 22 x 14
room bath:    bathroom east-of kitchen   size 9 x 14
room hall:    hallway  north-of kitchen  size 31 x 4
room bed1:    bedroom  north-of hall     size 11 x 12
room c1:      closet   east-of bed1      size 4 x 12
room bed2:    bedroom  east-of c1        size 11 x 12
room c2:      closet   east-of bed2      size 5 x 12

open living - kitchen width 8
door living - hall width 3
door hall - bath width 2.67 offset 5.83 into bath hinge near
door hall - bed1 width 2.67 offset 0.5 into bed1 hinge near
door hall - bed2 width 2.67 offset 0.5 into bed2 hinge near
door bed1 - c1 width 2.5 offset 0.5 into bed1 hinge near
door bed2 - c2 width 2.5 offset 0.5 into bed2 hinge near
entry living south width 3 offset 8
entry living west width 3 offset 24

porch front at 6,-6 size 7 x 6 covered
porch side at -6,22 size 6 x 7 covered

window living west width 14 offset 8
window kitchen south width 8 offset 6
window bath east width 4 offset 5 sill 5
window bed1 north width 4 offset 3
window bed2 north width 4 offset 3

alarm smoke in bed1
alarm smoke in bed2
alarm smoke in hall
"""

_GENERATE_SYSTEM = (
    "You are an expert residential designer specialising in barndominiums. You "
    "describe floor plans by writing source code in the barndsl architecture "
    "language, then refining it against the compiler's diagnostics until it is "
    "valid and well-designed.\n\n"
    + DSL_REFERENCE
    + "\n"
    + _DESIGN_RULES
    + "\n"
    + _PLACEMENT_CRAFT
    + "\nA COMPLETE EXAMPLE that compiles with zero errors, zero warnings, zero "
    "infos and scores 100/100 — note the relative anchors, the hall spine with "
    "every bedroom hung one room deep off it, the `open` core, the closets, and "
    "the second exterior door:\n```barn\n" + _EXAMPLE_PLAN + "```\n"
    "\nYou MUST declare the brief's program as a `program` statement derived "
    "from the brief — grammar: `program <n> bed [<m> bath] [<k> <type> ...] "
    "[area <sqft>]` (e.g. `program 3 bed 2 bath area 1800`) — so the compiler "
    "checks the plan delivers what was asked, not what you remembered.\n"
    "\nOUTPUT FORMAT (strict): reply with exactly ONE ```barn code block "
    "containing the COMPLETE plan source — every line, never a diff, a "
    "fragment, or two alternatives. No prose, headings, or commentary outside "
    "the block; if you must reason first, keep it out of the final answer."
)

_CRITIQUE_SYSTEM = (
    "You are a senior architect reviewing a barndominium plan (given as barndsl "
    "source plus the compiler's report) for design quality and livability. Judge "
    "flow and adjacencies (kitchen by dining, baths by beds, mudroom by entry), "
    "privacy, light, wasted space, and whether it is pleasant to live in — not "
    "just code-compliant. Anchor your verdict in the evidence you are given: "
    "the design score, its per-component deductions, and the diagnostics. Be "
    "constructive but exacting.\n\n" + DSL_REFERENCE
)

#: Appended to every critique prompt so the model emits JSON we can parse. Native
#: Anthropic gets the same instruction the SDK's structured output injects, so it
#: still validates via ``messages.parse``. Non-Anthropic gateways (DeepSeek) ignore
#: the SDK's injection but honour this in-prompt instruction, so they return JSON
#: (fenced or bare) that the fence-tolerant recovery path can then parse — instead
#: of the free-form prose they emit without it.
_CRITIQUE_JSON = (
    "\n\nRespond with ONLY a single JSON object — no prose or commentary around "
    "it — of exactly this shape:\n"
    '{"satisfied": true|false, "assessment": "<one-paragraph overall judgement>", '
    '"rationale": "<which diagnostics and score components justify the verdict>", '
    '"suggestions": ["<specific, actionable change>", ...]}\n'
    "Set `suggestions` to [] when satisfied is true. Give AT MOST 5 suggestions, "
    "ordered most-impactful first — five changes the designer will actually make "
    "beat a dozen that scatter the next revision."
)

#: Appended to the critique system prompt only when a rendered PNG rides along —
#: the critic must not be told an image is attached when it isn't.
_CRITIQUE_VISION = (
    "\n\nA rendered floor-plan image of this plan is attached. Judge what only "
    "a drawing shows — proportion, circulation legibility, wasted pockets, "
    "dead-end halls, facade rhythm — and your `rationale` must cite what you "
    "see in the drawing (e.g. \"the kitchen is an island unreachable from the "
    "garage\"), not only the diagnostics."
)

_FENCE_RE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n(.*?)```", re.DOTALL)

_PROGRAM_RE = re.compile(r"^\s*program\b", re.MULTILINE)


def _extract_source(text: str) -> str:
    """Pull the DSL out of a model reply.

    Three tiers, most-structured first: the longest fenced code block (the
    output contract asks for exactly one, but a chatty model sometimes fences
    an extra snippet in its commentary — longest wins); else the span from the
    first to the last line that starts with a DSL statement keyword or a ``#``
    comment, which trims the "Here is the plan:" / "This design provides…"
    prose a compat-gateway model wraps around unfenced source; else the whole
    reply. The trim only cuts leading/trailing chatter — interior lines are
    kept verbatim, so a stray mid-plan remark still surfaces as a compiler
    diagnostic on its own line rather than being silently dropped.
    """
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    lines = text.splitlines()

    def _is_statement(line: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("#"):
            return True
        first = stripped.split(None, 1)[0] if stripped else ""
        return first in _STMT_KEYWORDS

    starts = [i for i, ln in enumerate(lines) if _is_statement(ln)]
    if starts:
        return "\n".join(lines[starts[0] : starts[-1] + 1]).strip() + "\n"
    return text.strip() + "\n"


def _json_object_from_text(text: str) -> str | None:
    """Extract a JSON object substring from a model reply, or ``None``.

    Handles the two ways a model returns JSON when it isn't using native
    structured output: wrapped in a ```json fence, or embedded in prose. Prefers
    a fenced block that looks like an object, else falls back to the span from
    the first ``{`` to the last ``}``. Returns ``None`` when there is no object
    (e.g. the reply is pure prose), so the caller can degrade gracefully.
    """
    for block in _FENCE_RE.findall(text):
        block = block.strip()
        if block.startswith("{") and block.endswith("}"):
            return block
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return text[start : end + 1]
    return None


def _critique_from_text(text: str) -> CritiqueSpec | None:
    """Parse a :class:`CritiqueSpec` out of raw model text, or ``None``.

    The critique is a streamed reply the model was told to emit as a JSON object;
    this pulls the object out (fenced or embedded — see :func:`_json_object_from_text`)
    and validates it. Any failure (no JSON object, malformed, or missing fields)
    yields ``None`` rather than raising, so the caller degrades gracefully.
    """
    candidate = _json_object_from_text(text)
    if candidate is None:
        return None
    try:
        return CritiqueSpec.model_validate_json(candidate)
    except ValidationError:
        return None


def _pump_activity(stream: Any, on_activity: Callable[[str, str], None]) -> None:
    """Forward a message stream's text/thinking deltas to ``on_activity``.

    Iterated *before* ``stream.get_final_message()`` (which still returns the
    fully accumulated reply), so a UI can watch the model reason and write the
    DSL live instead of staring at a spinner until the round lands.
    ``on_activity(channel, delta)`` is called with ``channel`` one of
    ``"thinking"`` (extended-reasoning text) or ``"text"`` (the visible reply —
    the DSL being written, or the critic's JSON).

    Tolerant by design: only the two known delta shapes are forwarded, and any
    streaming hiccup is swallowed — the live feed is a nicety, never a reason to
    fail a design round (``get_final_message`` below still produces the result,
    or raises the same error it would have without streaming). A compat gateway
    (DeepSeek) that never emits ``thinking_delta`` events simply streams the
    ``text`` channel; the loop and the UI both degrade cleanly.
    """
    try:
        for event in stream:
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = getattr(event, "delta", None)
            kind = getattr(delta, "type", None)
            if kind == "text_delta":
                piece = getattr(delta, "text", "") or ""
                if piece:
                    on_activity("text", piece)
            elif kind == "thinking_delta":
                piece = getattr(delta, "thinking", "") or ""
                if piece:
                    on_activity("thinking", piece)
    except Exception:  # never let a streaming glitch abort the design loop
        pass


def render_feedback(
    result: CompileResult,
    score: ScoreReport | None = None,
    *,
    best_prior: "DesignStep | None" = None,
) -> str:
    """Render a compact, deterministic feedback block for the revision prompt.

    Structured fields from :meth:`CompileResult.to_dict` — one line per
    diagnostic (``severity CODE (room) line N: message | hint: ...``) headed by
    the score total, its non-zero per-component deductions and their cause
    lines (:attr:`ScoreReport.details` — the worst offenders, by name and
    number). When the compile produced a plan, the geometry pack from
    :func:`barndsl.introspect.plan_summary` is appended — resolved room
    rectangles with exterior walls, the door/adjacency edges, unplaced
    footprint pockets, and the free wall spans an opening can legally use — so
    the model reads coordinates off a table instead of re-deriving them from
    its own source. Token-lean by design: no source snippets, no caret art —
    the model gets fields to act on, not human formatting to scrape.

    ``best_prior`` is the best *valid* iteration recorded so far (or ``None``).
    When it is supplied and this attempt regressed against it — failed to
    compile, or scored lower — a single ``REGRESSION`` line is added under the
    header so the model reads the lost gradient as a regression and knows to
    revise from that iteration, not this broken one.
    """
    if score is None:
        score = design_score(result)
    deductions = ", ".join(f"{k} -{v:g}" for k, v in score.components.items() if v)
    lines = [
        f"Design score: {score.total:g}/100"
        + (f" — deductions: {deductions}" if deductions else " — no deductions")
    ]
    if best_prior is not None and best_prior.score is not None:
        broke = result.plan is None or bool(result.errors)
        if broke or score.total < best_prior.score.total:
            what = "does not compile" if broke else f"scored {score.total:g}"
            lines.append(
                f"REGRESSION: best valid iteration so far scored "
                f"{best_prior.score.total:g} (iteration {best_prior.iteration}); "
                f"this attempt {what} — you lost the gradient, revise from that "
                f"iteration, not this one."
            )
    for name, cause in score.details.items():
        lines.append(f"  {name} -{score.components[name]:g}: {cause}")
    for d in result.to_dict()["diagnostics"]:
        where = f" ({d['room']})" if d["room"] else ""
        loc = f" line {d['line']}" if d["line"] else ""
        line = f"{d['severity']} {d['code']}{where}{loc}: {d['message']}"
        if d["hint"]:
            line += f" | hint: {d['hint']}"
        lines.append(line)
    if result.plan is not None:
        lines.append(summary_text(plan_summary(result.plan)))
    return "\n".join(lines)


def _plan_png(result: CompileResult) -> bytes | None:
    """Render the compiled plan to PNG bytes for the multimodal critique.

    Best-effort and silent by design: returns ``None`` (never raises) when the
    compile produced no plan or the optional ``cairosvg`` raster dependency is
    missing or fails — the critique then runs text-only, exactly as before.
    cairosvg stays optional (``pip install 'barndsl[raster]'``), the same guard
    :func:`barndsl.render.save_render` applies.
    """
    if result.plan is None:
        return None
    try:
        import cairosvg

        from .render import render_svg

        return cairosvg.svg2png(bytestring=render_svg(result.plan).encode("utf-8"))
    except Exception:
        return None


class CritiqueSpec(BaseModel):
    """The model's design-quality review."""

    satisfied: bool = Field(
        description="True only if the plan is genuinely good and needs no changes."
    )
    assessment: str = Field(description="One-paragraph overall judgement.")
    rationale: str = Field(
        description=(
            "The concrete evidence for the verdict: which diagnostics and score "
            "components (by code/name) justify satisfied being true or false."
        )
    )
    suggestions: list[str] = Field(
        default_factory=list, description="Specific, actionable changes. Empty if satisfied."
    )


@dataclass
class DesignStep:
    iteration: int
    source: str
    result: CompileResult
    critique: CritiqueSpec | None = None
    score: ScoreReport | None = None


def _best_step(history: list[DesignStep]) -> DesignStep:
    """The highest-scoring step; ties go to the later iteration."""
    return max(
        history,
        key=lambda s: (s.score.total if s.score is not None else -1.0, s.iteration),
    )


def _best_valid_step(history: list[DesignStep]) -> DesignStep | None:
    """The highest-scoring step that actually *compiled* (a plan, no errors).

    Ties go to the later iteration. Returns ``None`` when nothing valid has been
    recorded yet — used both to head off a regression (the "best prior valid"
    reference in the feedback) and to pick a sound source to revise from when the
    latest attempt failed to compile.
    """
    valid = [
        s
        for s in history
        if s.result.plan is not None and not s.result.errors and s.score is not None
    ]
    if not valid:
        return None
    return max(valid, key=lambda s: (s.score.total if s.score else 0.0, s.iteration))


@dataclass
class DesignResult:
    source: str
    result: CompileResult
    history: list[DesignStep] = field(default_factory=list)

    @property
    def plan(self):
        return self.result.plan

    @property
    def iterations(self) -> int:
        return len(self.history)

    @property
    def best_iteration(self) -> int:
        """Which iteration won (its source/result are what this carries)."""
        if not self.history:
            return 0
        return _best_step(self.history).iteration

    @property
    def score(self) -> ScoreReport:
        """The winning step's score report (recomputed if it wasn't stored)."""
        if self.history:
            best = _best_step(self.history)
            if best.score is not None:
                return best.score
        return design_score(self.result)


class BarndoAgent:
    """Drives the write → compile → score → critique → revise loop."""

    def __init__(self, model: str | None = None, client=None, max_tokens: int | None = None):
        # ``None`` resolves via $BARNDSL_MODEL then DEFAULT_MODEL, so the endpoint
        # (Anthropic vs. a compat gateway) can pick a model it actually serves.
        self.model = resolve_model(model)
        # Output-token cap for generation and critique; big enough for a reasoning
        # model to think and still emit (see resolve_max_tokens / DEFAULT_MAX_TOKENS).
        self.max_tokens = resolve_max_tokens(max_tokens)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "The agent requires the 'anthropic' package. "
                    "Install with: pip install 'barndsl[agent]'"
                ) from exc
            # Point the agent at a custom endpoint (proxy, gateway, or a
            # self-hosted Anthropic-compatible API) via ANTHROPIC_BASE_URL.
            # Unset → the SDK default (https://api.anthropic.com).
            base_url = os.environ.get("ANTHROPIC_BASE_URL") or None
            self._client = anthropic.Anthropic(base_url=base_url)
        return self._client

    # -- single steps ------------------------------------------------------

    def write_source(
        self,
        brief: str,
        prior: str | None = None,
        diagnostics: str | None = None,
        seed: str | None = None,
        on_activity: Callable[[str, str], None] | None = None,
    ) -> str:
        prompt = f"Design brief:\n{brief}\n"
        if seed and not prior:
            prompt += (
                "\nA deterministic layout solver produced this dimensionally "
                "sound draft from the brief — rooms tile the envelope, interior "
                "doors sit on shared walls, and egress/daylight windows are "
                "placed. Start from it and improve the DESIGN (flow, adjacencies, "
                "proportion, light, wasted space); do NOT start from scratch and "
                "do not regress its geometry:\n```barn\n" + seed + "```\n"
            )
        if prior:
            prompt += f"\nYour previous DSL:\n```barn\n{prior}```\n"
        if diagnostics:
            prompt += (
                "\nCompiler feedback on it (one line per diagnostic; the design "
                "score at the top is the number you are maximising — 100 is a "
                "clean, well-designed plan, and each deduction names where the "
                "points went). Fix EVERY error, address warnings where "
                "reasonable, and raise the score:\n\n" + diagnostics + "\n"
            )
        if prior or seed:
            prompt += (
                "\nReturn the COMPLETE revised source (every line — never a diff "
                "or a fragment) as one ```barn block. Make the smallest revision "
                "that fixes the diagnostics: keep every line that already works, "
                "and do not restructure or rename rooms unless a diagnostic "
                "demands it."
            )
        else:
            prompt += (
                "\nReturn the complete plan source as one ```barn block."
            )

        # One silent retry on an empty reply, then fail loudly. An empty body is
        # what a compat gateway returns when the model id doesn't resolve, and
        # what a reasoning model returns when it burns the whole token cap
        # thinking — both are configuration problems the caller must see, not a
        # blank plan the loop grinds against for max_iterations rounds.
        for attempt in (1, 2):
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_GENERATE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
            ) as stream:
                if on_activity is not None:
                    _pump_activity(stream, on_activity)
                msg = stream.get_final_message()
            text = "".join(b.text for b in msg.content if b.type == "text")
            if text.strip():
                return _extract_source(text)
            logger.warning(
                "Model %r returned an empty reply (attempt %d, stop_reason=%s).",
                self.model, attempt, getattr(msg, "stop_reason", None),
            )
        raise RuntimeError(
            f"Model {self.model!r} returned no text twice in a row "
            f"(stop_reason={getattr(msg, 'stop_reason', None)!r}). If you are on "
            "a non-Anthropic gateway (ANTHROPIC_BASE_URL), check that "
            "BARNDSL_MODEL names a model the gateway actually serves, and that "
            "BARNDSL_MAX_TOKENS leaves a reasoning model room to think AND emit."
        )

    def critique(
        self,
        result: CompileResult,
        score: ScoreReport | None = None,
        on_activity: Callable[[str, str], None] | None = None,
    ) -> CritiqueSpec:
        if score is None:
            score = design_score(result)
        prompt = (
            "Review this barndominium plan.\n\n"
            f"DSL source:\n```barn\n{result.source}```\n\n"
            f"Score and diagnostics (your evidence):\n{render_feedback(result, score)}\n\n"
            "Assess the design and list concrete improvements. Set satisfied=true "
            "only if it compiles clean AND is a genuinely good layout, and in "
            "`rationale` cite the specific diagnostics and score components that "
            "justify your verdict."
            + _CRITIQUE_JSON
        )
        # Give the critic eyes: attach the rendered plan when it can be
        # rasterised, and only then claim (in the system prompt) that it was.
        png = _plan_png(result)
        system = _CRITIQUE_SYSTEM
        content: str | list = prompt
        if png is not None:
            system = _CRITIQUE_SYSTEM + _CRITIQUE_VISION
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode("ascii"),
                    },
                },
                {"type": "text", "text": prompt},
            ]
        # Stream the critique (like write_source). A non-streaming call
        # (``messages.parse``) is refused by the SDK once max_tokens is high on a
        # slow reasoning model — "Streaming is required for operations that may
        # take longer than 10 minutes" — and a reasoning model (deepseek-v4-pro)
        # needs that headroom to think. The model is told to emit a JSON object
        # (via ``_CRITIQUE_JSON``), which we parse fence-tolerantly, so this works
        # the same on native Anthropic and on compat gateways (DeepSeek) that
        # ignore Anthropic structured output. Any failure — no parseable JSON, or a
        # network/API error — degrades to a neutral verdict (with a warning) so the
        # compile-score-revise loop keeps running on any endpoint.
        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                thinking={"type": "adaptive"},
            ) as stream:
                if on_activity is not None:
                    _pump_activity(stream, on_activity)
                msg = stream.get_final_message()
        except Exception as exc:
            logger.warning(
                "Design critique call failed on model %r (%s: %s) — continuing "
                "without the critic; the loop still compiles, scores and revises.",
                self.model, type(exc).__name__, str(exc)[:200],
            )
            return CritiqueSpec(
                satisfied=result.ok,
                assessment="(critique skipped: the critique call failed)",
                rationale="",
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        crit = _critique_from_text(text)
        if crit is None:
            logger.warning(
                "Critique returned no parseable JSON on model %r — continuing "
                "without the critic.", self.model,
            )
            return CritiqueSpec(
                satisfied=result.ok,
                assessment="(critique skipped: no parseable critique returned)",
                rationale="",
            )
        return crit

    # -- full loop ---------------------------------------------------------

    def design(
        self,
        brief: str,
        max_iterations: int | None = None,
        critique: bool = True,
        on_step=None,
        target_score: float | None = _UNSET,
        seed_with_solver=None,
        *,
        seed_source: str | None = None,
        cancel: Callable[[], bool] | None = None,
        on_phase: Callable[[str, int], None] | None = None,
        on_activity: Callable[[str, int, str, str], None] | None = None,
    ) -> DesignResult:
        """Run the write → compile → score → critique → revise loop.

        ``max_iterations`` and ``target_score`` default to the environment-backed
        values (:func:`resolve_max_iterations` / :func:`resolve_target_score`, i.e.
        ``$BARNDSL_MAX_ITERATIONS`` / ``$BARNDSL_TARGET_SCORE`` or the built-in
        defaults) when left unset. An explicit ``target_score=None`` still
        *disables* the gate — the ``_UNSET`` sentinel is what selects the default,
        so ``None`` keeps its distinct "no gate" meaning.

        ``seed_with_solver`` opts into seeding the loop from the deterministic
        layout engines (:mod:`barndsl.layout`/:mod:`barndsl.layout2`). ``design``
        itself only receives a free-text ``brief``, which the solver can't
        consume, so the caller passes an explicit solver program: a
        :class:`~barndsl.layout2.LayoutBrief2`, a
        :class:`~barndsl.layout.LayoutBrief`, or a textual brief (parsed as a v2
        brief). The best solver candidate is recorded as **iteration 0** — the
        floor best-iteration-wins must beat — and its DSL is handed to the first
        generation prompt as a dimensionally sound draft to refine rather than a
        blank page. Falsy (the default) leaves the loop exactly as it was; if the
        solver produces nothing that compiles, seeding is silently skipped.

        ``seed_source`` (keyword-only) turns the loop into a **refinement**: the
        caller's current DSL is fed to round 1 as the "previous" source with its
        own diagnostics, so "make the kitchen bigger" revises that plan instead
        of starting from a blank page. Unlike the solver seed it is *not* scored
        as a competing iteration — refinement intentionally reshapes the design
        per the new brief, and must not be vetoed by best-iteration-wins if the
        edit trades a point of score for the user's request. Default ``None``
        leaves the loop unchanged.

        ``cancel`` (keyword-only) is polled once at the top of every round; when
        it returns true the loop stops and hands back the best iteration so far.
        ``on_phase`` (keyword-only) is called with ``(phase, round)`` — phase one
        of ``"writing" | "compiling" | "critiquing"`` — as each round advances,
        so a UI can narrate the loop between the coarser ``on_step`` results.
        ``on_activity`` (keyword-only) is finer still: called with ``(phase,
        round, channel, delta)`` for each streamed token chunk of the writing and
        critiquing LLM calls — ``phase`` is ``"writing"`` or ``"critiquing"`` and
        ``channel`` is ``"thinking"`` (the model's reasoning) or ``"text"`` (the
        DSL being written / the critic's reply). It lets a UI show the agent
        think and write live instead of waiting for the round to land. All three
        default ``None`` (no behaviour change, no calls).
        """
        # Resolve the environment-backed defaults (an explicit arg always wins;
        # target_score=None stays "gate disabled" — only _UNSET means "default").
        if max_iterations is None:
            max_iterations = resolve_max_iterations()
        if target_score is _UNSET:
            target_score = resolve_target_score()

        history: list[DesignStep] = []
        source: str | None = None
        feedback: str | None = None

        # Candidate 0: the deterministic solver's best plan, if one was requested
        # and it compiles. Recorded as iteration 0 so best-iteration-wins can
        # return it, and its source seeds the first generation prompt.
        solver_seed: str | None = None
        if seed_with_solver:
            seed_step = _solver_seed_step(seed_with_solver)
            if seed_step is not None:
                history.append(seed_step)
                solver_seed = seed_step.source
                if on_step:
                    on_step(seed_step)

        # Refinement: revise the caller's current plan. Prime round 1 with it as
        # the "prior" source plus its diagnostics so the first write is a revision.
        if seed_source is not None:
            source = seed_source
            seed_result = compile_source(seed_source, name=None)
            _fold_program_nudge(seed_result)
            feedback = render_feedback(seed_result)

        # A 2-arg (channel, delta) adapter that stamps each streamed token with
        # the phase and round the design-level ``on_activity`` contract carries.
        # ``None`` when no activity was requested, so ``write_source``/``critique``
        # never iterate the stream (today's callers pay nothing).
        def activity_for(phase: str, rnd: int) -> Callable[[str, str], None] | None:
            if on_activity is None:
                return None
            return lambda channel, delta: on_activity(phase, rnd, channel, delta)

        for i in range(1, max_iterations + 1):
            if cancel is not None and cancel():
                break
            if on_phase is not None:
                on_phase("writing", i)
            source = self.write_source(
                brief, prior=source, diagnostics=feedback, seed=solver_seed,
                on_activity=activity_for("writing", i),
            )
            if on_phase is not None:
                on_phase("compiling", i)
            result = compile_source(source, name=None)
            # The program nudge is folded before scoring: it is deterministic
            # (a pure function of the source), so the score stays a contract —
            # and a missing `program` line now costs points the loop can win back.
            _fold_program_nudge(result)
            score = design_score(result)
            crit = None
            if critique and result.plan is not None:
                if on_phase is not None:
                    on_phase("critiquing", i)
                crit = self.critique(result, score, on_activity=activity_for("critiquing", i))
            # Fold the architect's review into the diagnostic stream as INFO, so
            # design feedback travels the same channel as the compiler's errors.
            # (After scoring: the critique is model-driven, the score is not.)
            _fold_critique(result, crit)

            step = DesignStep(i, source, result, crit, score)
            history.append(step)
            if on_step:
                on_step(step)

            done = result.ok and (crit is None or crit.satisfied)
            # Gate mechanically: the critic's "satisfied" alone can't end the
            # loop while the score says there are points on the table.
            if target_score is not None and score.total < target_score:
                done = False
            if done or i == max_iterations:
                break
            # Flag a regression against the best valid iteration *before* this one,
            # so a broken or lower-scoring round reads as a regression.
            feedback = render_feedback(
                result, score, best_prior=_best_valid_step(history[:-1])
            )
            # When the latest attempt failed to compile, revise from the best
            # valid source instead of stranding the model on non-compiling code.
            # The feedback must stay coherent with the source it is shown
            # against: the prompt captions it as "feedback on it", so pair the
            # best valid source with ITS OWN diagnostics and quote the broken
            # attempt's fatal lines separately, clearly attributed.
            if result.plan is None or result.errors:
                best_valid = _best_valid_step(history)
                if best_valid is not None:
                    source = best_valid.source
                    fatal = []
                    for d in result.to_dict()["diagnostics"]:
                        if d["severity"] != "error":
                            continue
                        loc = f" line {d['line']}" if d["line"] else ""
                        fatal.append(f"error {d['code']}{loc}: {d['message']}")
                    bv_score = best_valid.score
                    assert bv_score is not None  # _best_valid_step filters on it
                    feedback = (
                        f"NOTE: your newest attempt (iteration {i}) did not "
                        f"compile and was DISCARDED. The DSL shown above is "
                        f"your best valid iteration ({best_valid.iteration}, "
                        f"scored {bv_score.total:g}); the feedback "
                        f"below describes THAT source. Improve it — and do "
                        f"not repeat the discarded attempt's mistakes.\n"
                        + render_feedback(best_valid.result, best_valid.score)
                        + "\n\nFatal diagnostics from the discarded attempt "
                        "(these describe the discarded source, NOT the DSL "
                        "shown above):\n"
                        + "\n".join(fatal)
                    )

        if not history:
            # Cancelled before any round was recorded: still return a well-formed
            # result (the refinement seed, or an empty compile) rather than raise.
            src = seed_source or ""
            return DesignResult(src, compile_source(src, name=None), history)
        best = _best_step(history)
        return DesignResult(best.source, best.result, history)


def _fold_critique(result: CompileResult, crit: CritiqueSpec | None) -> None:
    """Append the architect's suggestions to ``result`` as INFO diagnostics.

    This makes design-quality feedback first-class: it shows up in
    ``result.report()`` (carets and all) alongside code-check errors, so a
    single diagnostic stream carries both "is it valid" and "is it good".
    """
    if crit is None or crit.satisfied:
        return
    # Cap what rides into the next prompt: the critic is asked for at most five
    # suggestions, but a chatty model (DeepSeek returns 7-12 a round) ignores
    # that, and a dozen INFO lines scatter the revision instead of focusing it.
    for s in crit.suggestions[:5]:
        result.diagnostics.append(
            Issue(Severity.INFO, "DESIGN", s, hint="Architect's review (design quality).")
        )


def _fold_program_nudge(result: CompileResult) -> None:
    """Append an INFO when the source declares no ``program`` statement.

    Deterministic (regex on the source, no model call) and folded the same way
    :func:`_fold_critique` folds the critique — the nudge rides the diagnostic
    stream into the next revision prompt, so the loop self-corrects until the
    brief's intent is declared where the compiler can check it.
    """
    if _PROGRAM_RE.search(result.source):
        return
    result.diagnostics.append(
        Issue(
            Severity.INFO,
            "NO_PROGRAM",
            "No `program` statement: declare the brief's intent so the compiler "
            "checks the plan delivers it.",
            hint=(
                "Derive it from the brief, e.g. `program 3 bed 2 bath area 1800` "
                "(grammar: `program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]`)."
            ),
        )
    )


def _solver_candidate_sources(spec) -> list[str]:
    """Emit DSL for each solver candidate derived from ``spec``.

    ``spec`` is a program the deterministic engines can solve — a
    :class:`barndsl.layout2.LayoutBrief2` (run through the space-filling
    topologies), a :class:`barndsl.layout.LayoutBrief` (the v1 greedy engine), or
    a textual brief (parsed as a v2 brief). Enumerates engines × topologies the
    way the CLI does, capped at the three v2 topologies (bands · slice · dual) so
    runtime stays bounded — every one is near-free (no API call). Returns one DSL
    string per topology that produced a plan; any failure is skipped, so seeding
    always degrades gracefully to "no seed".
    """
    from .emit import emit_dsl
    from .layout import LayoutBrief, solve_layout
    from .layout2 import LayoutBrief2, parse_brief2, solve_layout2

    if isinstance(spec, str):
        try:
            spec = parse_brief2(spec)
        except Exception:
            return []

    results = []
    if isinstance(spec, LayoutBrief2):
        for engine in ("bands", "slice", "dual"):
            try:
                out = solve_layout2(spec, engine=engine)
            except Exception:
                continue
            if out is not None and out.plan is not None:
                results.append(out)
    elif isinstance(spec, LayoutBrief):
        try:
            out = solve_layout(spec)
        except Exception:
            out = None
        if out is not None and out.plan is not None:
            results.append(out)
    else:
        return []

    sources: list[str] = []
    for out in results:
        try:
            src = emit_dsl(out.plan)
        except Exception:
            continue
        # The emitter writes no `program` statement, which would dock the seed
        # the missing-program nudge and depress the floor below what its
        # geometry earns. Declare the intent the plan itself delivers — the
        # counts come from the same metrics() PROGRAM_MISMATCH checks against,
        # so the derived line is guaranteed consistent.
        if not any(
            line.strip().startswith("program ")
            for line in src.splitlines()
        ):
            m = out.plan.metrics()
            beds, baths = int(m["bedroom_count"]), int(m["bathroom_count"])
            if beds:
                stmt = f"program {beds} bed"
                if baths:
                    stmt += f" {baths} bath"
                src = stmt + "\n" + src
        sources.append(src)
    return sources


def _solver_seed_step(spec) -> DesignStep | None:
    """The solver's best compiling candidate, as iteration 0 — or ``None``.

    Each candidate is compiled, folded through the same ``program`` nudge the
    loop applies (so its score is directly comparable to the LLM's iterations),
    and scored with :func:`design_score`; the highest-scoring one that actually
    compiles (a plan, no errors) wins. Returns ``None`` when the solver yields
    nothing that compiles, so the loop then proceeds exactly as it does today.
    """
    best: DesignStep | None = None
    for src in _solver_candidate_sources(spec):
        try:
            result = compile_source(src, name=None)
        except Exception:
            continue
        if result.plan is None or result.errors:
            continue
        _fold_program_nudge(result)
        score = design_score(result)
        if best is None or best.score is None or score.total > best.score.total:
            best = DesignStep(0, result.source, result, None, score)
    return best


def design(
    brief: str,
    model: str | None = None,
    max_iterations: int | None = None,
    on_step=None,
    target_score: float | None = _UNSET,
    seed_with_solver=None,
    *,
    seed_source: str | None = None,
    cancel: Callable[[], bool] | None = None,
    on_phase: Callable[[str, int], None] | None = None,
    on_activity: Callable[[str, int, str, str], None] | None = None,
) -> DesignResult:
    """Convenience: run :class:`BarndoAgent` end-to-end on ``brief``.

    ``max_iterations``/``target_score`` left unset fall to the environment-backed
    defaults, exactly as :meth:`BarndoAgent.design` documents.
    """
    return BarndoAgent(model=model).design(
        brief,
        max_iterations=max_iterations,
        on_step=on_step,
        target_score=target_score,
        seed_with_solver=seed_with_solver,
        seed_source=seed_source,
        cancel=cancel,
        on_phase=on_phase,
        on_activity=on_activity,
    )
