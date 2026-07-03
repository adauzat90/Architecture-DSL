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
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .compiler import DSL_REFERENCE, CompileResult, compile_source
from .introspect import plan_summary, summary_text
from .score import ScoreReport, design_score
from .validation import Issue, Severity

DEFAULT_MODEL = "claude-opus-4-8"

#: Below this score the design is not "done" even if the critic is satisfied.
DEFAULT_TARGET_SCORE = 90.0

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

_GENERATE_SYSTEM = (
    "You are an expert residential designer specialising in barndominiums. You "
    "describe floor plans by writing source code in the barndsl architecture "
    "language, then refining it against the compiler's diagnostics until it is "
    "valid and well-designed.\n\n"
    + DSL_REFERENCE
    + "\n"
    + _DESIGN_RULES
    + "\nYou MUST declare the brief's program as a `program` statement derived "
    "from the brief — grammar: `program <n> bed [<m> bath] [<k> <type> ...] "
    "[area <sqft>]` (e.g. `program 3 bed 2 bath area 1800`) — so the compiler "
    "checks the plan delivers what was asked, not what you remembered.\n"
    "\nAlways reply with ONLY the DSL source (optionally inside a ```barn code "
    "block). Do not add prose before or after."
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
    """Pull the DSL out of a model reply (strip a code fence if present)."""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


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

    def __init__(self, model: str = DEFAULT_MODEL, client=None):
        self.model = model
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
            self._client = anthropic.Anthropic()
        return self._client

    # -- single steps ------------------------------------------------------

    def write_source(
        self,
        brief: str,
        prior: str | None = None,
        diagnostics: str | None = None,
        seed: str | None = None,
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
        prompt += "\nReturn the complete, revised DSL source."

        with self.client.messages.stream(
            model=self.model,
            max_tokens=8000,
            system=_GENERATE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
        ) as stream:
            msg = stream.get_final_message()
        text = "".join(b.text for b in msg.content if b.type == "text")
        return _extract_source(text)

    def critique(self, result: CompileResult, score: ScoreReport | None = None) -> CritiqueSpec:
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
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=CritiqueSpec,
        )
        crit = resp.parsed_output
        if crit is None:  # pragma: no cover
            return CritiqueSpec(
                satisfied=result.ok, assessment="(no critique returned)", rationale=""
            )
        return crit

    # -- full loop ---------------------------------------------------------

    def design(
        self,
        brief: str,
        max_iterations: int = 3,
        critique: bool = True,
        on_step=None,
        target_score: float | None = DEFAULT_TARGET_SCORE,
        seed_with_solver=None,
        *,
        seed_source: str | None = None,
        cancel: Callable[[], bool] | None = None,
        on_phase: Callable[[str, int], None] | None = None,
    ) -> DesignResult:
        """Run the write → compile → score → critique → revise loop.

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
        Both default ``None`` (no behaviour change, no calls).
        """
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

        for i in range(1, max_iterations + 1):
            if cancel is not None and cancel():
                break
            if on_phase is not None:
                on_phase("writing", i)
            source = self.write_source(
                brief, prior=source, diagnostics=feedback, seed=solver_seed
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
                crit = self.critique(result, score)
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
    for s in crit.suggestions:
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
    model: str = DEFAULT_MODEL,
    max_iterations: int = 3,
    on_step=None,
    target_score: float | None = DEFAULT_TARGET_SCORE,
    seed_with_solver=None,
    *,
    seed_source: str | None = None,
    cancel: Callable[[], bool] | None = None,
    on_phase: Callable[[str, int], None] | None = None,
) -> DesignResult:
    """Convenience: run :class:`BarndoAgent` end-to-end on ``brief``."""
    return BarndoAgent(model=model).design(
        brief,
        max_iterations=max_iterations,
        on_step=on_step,
        target_score=target_score,
        seed_with_solver=seed_with_solver,
        seed_source=seed_source,
        cancel=cancel,
        on_phase=on_phase,
    )
