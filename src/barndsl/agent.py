"""Claude-powered agentic workflow: generate → validate → critique → refine.

The pure engine (:mod:`barndsl.elements`, :mod:`barndsl.validation`,
:mod:`barndsl.render`) has no LLM dependency. This module adds an agent that
turns a natural-language brief into a validated plan by looping:

    1. **generate** a structured plan from the brief (and any prior feedback);
    2. **validate** it against the building-code checks;
    3. **critique** it with the model for design quality;
    4. **refine** — feed the errors and critique back and regenerate, until the
       plan is code-valid and the critic is satisfied (or the iteration cap is hit).

Requires the ``anthropic`` package and an ``ANTHROPIC_API_KEY``. Install with
``pip install 'barndsl[agent]'``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .elements import Barndominium, Direction, RoomType
from .validation import ValidationReport, validate

DEFAULT_MODEL = "claude-opus-4-8"


# --- Structured output schema ----------------------------------------------


class RoomSpec(BaseModel):
    id: str = Field(description="Unique snake_case identifier, e.g. 'master_bedroom'.")
    type: RoomType
    x: float = Field(description="West edge, feet from the SW origin.")
    y: float = Field(description="South edge, feet from the SW origin.")
    width: float = Field(description="East-west extent in feet.")
    length: float = Field(description="North-south extent in feet.")


class InteriorDoorSpec(BaseModel):
    room_a: str
    room_b: str
    width: float = 2.67


class ExteriorDoorSpec(BaseModel):
    room: str
    wall: Direction
    width: float = 3.0
    offset: float = 1.0
    egress: bool = True


class WindowSpec(BaseModel):
    room: str
    wall: Direction
    width: float = 4.0
    offset: float = 2.0


class PorchSpec(BaseModel):
    id: str
    x: float
    y: float
    width: float
    length: float
    covered: bool = True


class PlanSpec(BaseModel):
    """The structured floor plan the model emits."""

    name: str
    envelope_width: float = Field(description="Overall footprint width (E-W), feet.")
    envelope_length: float = Field(description="Overall footprint length (N-S), feet.")
    ceiling_height: float = 9.0
    rooms: list[RoomSpec] = Field(default_factory=list)
    interior_doors: list[InteriorDoorSpec] = Field(default_factory=list)
    exterior_doors: list[ExteriorDoorSpec] = Field(default_factory=list)
    windows: list[WindowSpec] = Field(default_factory=list)
    porches: list[PorchSpec] = Field(default_factory=list)
    notes: str = ""


class CritiqueSpec(BaseModel):
    """The model's design-quality review of a generated plan."""

    satisfied: bool = Field(
        description="True only if the plan is a genuinely good, buildable design "
        "needing no further changes."
    )
    assessment: str = Field(description="One-paragraph overall judgement.")
    suggestions: list[str] = Field(
        default_factory=list,
        description="Specific, actionable changes. Empty if satisfied.",
    )


def build_plan(spec: PlanSpec) -> Barndominium:
    """Materialise a :class:`Barndominium` from a :class:`PlanSpec`."""
    plan = Barndominium(
        name=spec.name,
        envelope_width=spec.envelope_width,
        envelope_length=spec.envelope_length,
        ceiling_height=spec.ceiling_height,
        notes=spec.notes,
    )
    for r in spec.rooms:
        plan.add_room(r.id, r.type, x=r.x, y=r.y, width=r.width, length=r.length)
    for d in spec.interior_doors:
        plan.connect(d.room_a, d.room_b, width=d.width)
    for d in spec.exterior_doors:
        plan.entrance(d.room, d.wall, width=d.width, offset=d.offset, egress=d.egress)
    for w in spec.windows:
        plan.add_window(w.room, w.wall, width=w.width, offset=w.offset)
    for p in spec.porches:
        plan.add_porch(p.id, x=p.x, y=p.y, width=p.width, length=p.length, covered=p.covered)
    return plan


# --- Prompts ----------------------------------------------------------------

_GEOMETRY_RULES = """\
COORDINATE SYSTEM (read carefully):
- All measurements are in FEET.
- Origin (0,0) is the bottom-left (south-west) corner of the building envelope.
- x increases east (right); y increases north (up).
- A room at x,y with width W and length L occupies [x, x+W] east-west and
  [y, y+L] south-north. Its walls: south=y, north=y+L, west=x, east=x+L.
- Rooms MUST stay inside the envelope: 0 <= x, x+W <= envelope_width and
  0 <= y, y+L <= envelope_length.
- Rooms MUST NOT overlap. Pack them to tile the footprint with little waste.
- Two rooms can only have a connecting interior door if they share a wall
  segment (collinear edges with overlapping extent).
- Put exterior doors and windows only on walls that lie on the envelope edge.

DESIGN RULES (barndominium, IRC-informed):
- Bedrooms: >= 70 sq ft, smallest dimension >= 7 ft, and each needs an egress
  window (or exterior door) on an exterior wall.
- Provide at least one full bathroom; locate baths near bedrooms.
- Every interior room must be reachable from an entrance via interior doors
  (use a hallway/open plan to connect spaces — don't strand rooms).
- Habitable rooms need windows totalling >= 8% of their floor area.
- Hallways >= 3 ft wide; at least one exterior egress door >= 32 in (2.67 ft).
- Barndominiums are typically a single large rectangle; open-concept
  living/kitchen/dining is idiomatic. A shop/garage bay is common.
"""

_GENERATE_SYSTEM = (
    "You are an expert residential designer specialising in barndominiums "
    "(metal-frame post-and-beam homes). You produce complete, buildable, "
    "code-conscious floor plans as structured data.\n\n" + _GEOMETRY_RULES
)

_CRITIQUE_SYSTEM = (
    "You are a senior architect reviewing a barndominium floor plan for design "
    "quality and livability. Be constructive but exacting. Consider flow and "
    "adjacencies (e.g. kitchen near dining, baths near beds, mudroom near "
    "entry), privacy, natural light, wasted space, and whether the layout is "
    "actually pleasant to live in — not just code-compliant.\n\n" + _GEOMETRY_RULES
)


# --- Result types -----------------------------------------------------------


@dataclass
class DesignStep:
    iteration: int
    spec: PlanSpec
    plan: Barndominium
    report: ValidationReport
    critique: CritiqueSpec | None = None


@dataclass
class DesignResult:
    plan: Barndominium
    spec: PlanSpec
    report: ValidationReport
    history: list[DesignStep] = field(default_factory=list)

    @property
    def iterations(self) -> int:
        return len(self.history)


# --- Agent ------------------------------------------------------------------


class BarndoAgent:
    """Drives the generate → validate → critique → refine loop."""

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

    def generate(self, brief: str, feedback: str | None = None, prior: PlanSpec | None = None) -> PlanSpec:
        prompt = f"Design brief:\n{brief}\n"
        if prior is not None:
            prompt += (
                "\nYour previous attempt (JSON):\n"
                + prior.model_dump_json(indent=2)
                + "\n"
            )
        if feedback:
            prompt += (
                "\nThe previous attempt had the following problems. Fix ALL of "
                "them in this revision:\n" + feedback + "\n"
            )
        prompt += "\nProduce the full revised plan."

        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=_GENERATE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=PlanSpec,
            thinking={"type": "adaptive"},
        )
        spec = resp.parsed_output
        if spec is None:  # pragma: no cover - refusal / parse failure
            raise RuntimeError("Model did not return a parseable plan.")
        return spec

    def critique(self, plan: Barndominium, report: ValidationReport, spec: PlanSpec) -> CritiqueSpec:
        prompt = (
            "Review this barndominium plan.\n\n"
            f"Plan (JSON):\n{spec.model_dump_json(indent=2)}\n\n"
            f"Automated code-check report:\n{report}\n\n"
            "Assess the design and list concrete improvements. Set satisfied=true "
            "only if it is code-valid AND a genuinely good layout."
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
            return CritiqueSpec(satisfied=report.is_valid, assessment="(no critique returned)")
        return crit

    # -- full loop ---------------------------------------------------------

    def design(
        self,
        brief: str,
        max_iterations: int = 3,
        critique: bool = True,
        on_step=None,
    ) -> DesignResult:
        """Run the full workflow and return the best plan produced."""
        history: list[DesignStep] = []
        spec: PlanSpec | None = None
        feedback: str | None = None

        for i in range(1, max_iterations + 1):
            spec = self.generate(brief, feedback=feedback, prior=spec)
            plan = build_plan(spec)
            report = validate(plan)
            crit = self.critique(plan, report, spec) if critique else None

            step = DesignStep(i, spec, plan, report, crit)
            history.append(step)
            if on_step:
                on_step(step)

            done = report.is_valid and (crit is None or crit.satisfied)
            if done or i == max_iterations:
                break
            feedback = _format_feedback(report, crit)

        last = history[-1]
        return DesignResult(last.plan, last.spec, last.report, history)


def _format_feedback(report: ValidationReport, crit: CritiqueSpec | None) -> str:
    lines: list[str] = []
    if report.errors:
        lines.append("CODE ERRORS (must fix):")
        lines += [f"  - {i.code}{f' [{i.room}]' if i.room else ''}: {i.message}" for i in report.errors]
    if report.warnings:
        lines.append("WARNINGS (address where possible):")
        lines += [f"  - {i.code}{f' [{i.room}]' if i.room else ''}: {i.message}" for i in report.warnings]
    if crit and crit.suggestions:
        lines.append("DESIGN CRITIQUE:")
        lines += [f"  - {s}" for s in crit.suggestions]
    return "\n".join(lines) if lines else "No specific issues; improve overall quality."


def design(brief: str, model: str = DEFAULT_MODEL, max_iterations: int = 3, on_step=None) -> DesignResult:
    """Convenience: run :class:`BarndoAgent` end-to-end on ``brief``."""
    return BarndoAgent(model=model).design(brief, max_iterations=max_iterations, on_step=on_step)
