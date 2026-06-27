"""Claude-powered agentic workflow built around the compiler.

The agent *writes architecture in the DSL*. Each round it emits DSL source, the
compiler returns diagnostics (errors + fix hints), and those diagnostics are fed
straight back as the next prompt — a compile-fix loop, exactly like a developer
iterating against compiler output:

    1. **write**    DSL source from the brief (+ prior source + diagnostics);
    2. **compile**  → plan + diagnostics (line, code, hint);
    3. **critique** the design for quality (optional, model-driven);
    4. **revise**   feed diagnostics + critique back, rewrite the DSL, repeat —
       until it compiles clean and the critic is satisfied (or the cap is hit).

Requires ``anthropic`` and ``ANTHROPIC_API_KEY``. Install ``pip install 'barndsl[agent]'``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .compiler import DSL_REFERENCE, CompileResult, compile_source

DEFAULT_MODEL = "claude-opus-4-8"

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
    + "\nAlways reply with ONLY the DSL source (optionally inside a ```barn code "
    "block). Do not add prose before or after."
)

_CRITIQUE_SYSTEM = (
    "You are a senior architect reviewing a barndominium plan (given as barndsl "
    "source plus the compiler's report) for design quality and livability. Judge "
    "flow and adjacencies (kitchen by dining, baths by beds, mudroom by entry), "
    "privacy, light, wasted space, and whether it is pleasant to live in — not "
    "just code-compliant. Be constructive but exacting.\n\n" + DSL_REFERENCE
)

_FENCE_RE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n(.*?)```", re.DOTALL)


def _extract_source(text: str) -> str:
    """Pull the DSL out of a model reply (strip a code fence if present)."""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


class CritiqueSpec(BaseModel):
    """The model's design-quality review."""

    satisfied: bool = Field(
        description="True only if the plan is genuinely good and needs no changes."
    )
    assessment: str = Field(description="One-paragraph overall judgement.")
    suggestions: list[str] = Field(
        default_factory=list, description="Specific, actionable changes. Empty if satisfied."
    )


@dataclass
class DesignStep:
    iteration: int
    source: str
    result: CompileResult
    critique: CritiqueSpec | None = None


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


class BarndoAgent:
    """Drives the write → compile → critique → revise loop."""

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
                "\nThe compiler reported the following. Fix EVERY error and "
                "address warnings where reasonable:\n\n" + diagnostics + "\n"
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

    def critique(self, result: CompileResult) -> CritiqueSpec:
        prompt = (
            "Review this barndominium plan.\n\n"
            f"DSL source:\n```barn\n{result.source}```\n\n"
            f"Compiler report:\n{result.report()}\n\n"
            "Assess the design and list concrete improvements. Set satisfied=true "
            "only if it compiles clean AND is a genuinely good layout."
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
            return CritiqueSpec(satisfied=result.ok, assessment="(no critique returned)")
        return crit

    # -- full loop ---------------------------------------------------------

    def design(
        self,
        brief: str,
        max_iterations: int = 3,
        critique: bool = True,
        on_step=None,
    ) -> DesignResult:
        history: list[DesignStep] = []
        source: str | None = None
        feedback: str | None = None

        for i in range(1, max_iterations + 1):
            source = self.write_source(brief, prior=source, diagnostics=feedback)
            result = compile_source(source, name=None)
            crit = self.critique(result) if (critique and result.plan is not None) else None

            step = DesignStep(i, source, result, crit)
            history.append(step)
            if on_step:
                on_step(step)

            done = result.ok and (crit is None or crit.satisfied)
            if done or i == max_iterations:
                break
            feedback = _format_feedback(result, crit)

        last = history[-1]
        return DesignResult(last.source, last.result, history)


def _format_feedback(result: CompileResult, crit: CritiqueSpec | None) -> str:
    parts = [result.report()]
    if crit and crit.suggestions:
        parts.append("\nDESIGN CRITIQUE (improve where you can):")
        parts += [f"  - {s}" for s in crit.suggestions]
    return "\n".join(parts)


def design(
    brief: str, model: str = DEFAULT_MODEL, max_iterations: int = 3, on_step=None
) -> DesignResult:
    """Convenience: run :class:`BarndoAgent` end-to-end on ``brief``."""
    return BarndoAgent(model=model).design(brief, max_iterations=max_iterations, on_step=on_step)
