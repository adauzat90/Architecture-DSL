"""Claude-powered agentic workflow built around the compiler.

The agent *writes architecture in the DSL*. Each round it emits DSL source, the
compiler returns diagnostics (errors + fix hints), and those diagnostics are fed
straight back as the next prompt — a compile-fix loop, exactly like a developer
iterating against compiler output:

    1. **write**    DSL source from the brief (+ prior source + diagnostics);
    2. **compile**  → plan + diagnostics (line, code, hint) + a deterministic
       0-100 design score (see :mod:`barndsl.score`);
    3. **critique** the design for quality (optional, model-driven, anchored to
       the score evidence);
    4. **revise**   feed the structured diagnostics + score + critique back,
       rewrite the DSL, repeat — until it compiles clean, the critic is
       satisfied AND the score clears ``target_score`` (or the cap is hit).

The loop hill-climbs on the score: every step is scored, and ``design()``
returns the **best-scoring** iteration, not the last — a regression on the
final round is never silently returned.

Requires ``anthropic`` and ``ANTHROPIC_API_KEY``. Install ``pip install 'barndsl[agent]'``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .compiler import DSL_REFERENCE, CompileResult, compile_source
from .score import ScoreReport, design_score
from .validation import Issue, Severity

DEFAULT_MODEL = "claude-opus-4-8"

#: Below this score the design is not "done" even if the critic is satisfied.
DEFAULT_TARGET_SCORE = 90.0

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

_FENCE_RE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n(.*?)```", re.DOTALL)

_PROGRAM_RE = re.compile(r"^\s*program\b", re.MULTILINE)


def _extract_source(text: str) -> str:
    """Pull the DSL out of a model reply (strip a code fence if present)."""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


def render_feedback(result: CompileResult, score: ScoreReport | None = None) -> str:
    """Render a compact, deterministic feedback block for the revision prompt.

    Structured fields from :meth:`CompileResult.to_dict` — one line per
    diagnostic (``severity CODE (room) line N: message | hint: ...``) headed by
    the score total and its non-zero per-component deductions. Token-lean by
    design: no source snippets, no caret art — the model gets fields to act on,
    not human formatting to scrape.
    """
    if score is None:
        score = design_score(result)
    deductions = ", ".join(f"{k} -{v:g}" for k, v in score.components.items() if v)
    lines = [
        f"Design score: {score.total:g}/100"
        + (f" — deductions: {deductions}" if deductions else " — no deductions")
    ]
    for d in result.to_dict()["diagnostics"]:
        where = f" ({d['room']})" if d["room"] else ""
        loc = f" line {d['line']}" if d["line"] else ""
        line = f"{d['severity']} {d['code']}{where}{loc}: {d['message']}"
        if d["hint"]:
            line += f" | hint: {d['hint']}"
        lines.append(line)
    return "\n".join(lines)


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
        self, brief: str, prior: str | None = None, diagnostics: str | None = None
    ) -> str:
        prompt = f"Design brief:\n{brief}\n"
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
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=_CRITIQUE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
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
    ) -> DesignResult:
        history: list[DesignStep] = []
        source: str | None = None
        feedback: str | None = None

        for i in range(1, max_iterations + 1):
            source = self.write_source(brief, prior=source, diagnostics=feedback)
            result = compile_source(source, name=None)
            # The program nudge is folded before scoring: it is deterministic
            # (a pure function of the source), so the score stays a contract —
            # and a missing `program` line now costs points the loop can win back.
            _fold_program_nudge(result)
            score = design_score(result)
            crit = self.critique(result, score) if (critique and result.plan is not None) else None
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
            feedback = render_feedback(result, score)

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


def design(
    brief: str,
    model: str = DEFAULT_MODEL,
    max_iterations: int = 3,
    on_step=None,
    target_score: float | None = DEFAULT_TARGET_SCORE,
) -> DesignResult:
    """Convenience: run :class:`BarndoAgent` end-to-end on ``brief``."""
    return BarndoAgent(model=model).design(
        brief, max_iterations=max_iterations, on_step=on_step, target_score=target_score
    )
