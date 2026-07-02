"""A deterministic 0–100 design score for a compiled plan.

The compiler tells an author *what* is wrong; this module says *how good* the
plan is as a single number an agent can hill-climb on — compare two candidate
plans, keep the better one, detect a regression. The score is a **contract**:
same plan in, same score out (pure arithmetic, no randomness, no LLM), and the
formula below is the whole definition.

Formula::

    total = clamp(100 - sum(components), 0, 100)

where each component is a penalty (points deducted):

    errors       100 flat if the compile has any error (or produced no plan) —
                 an unbuildable plan scores 0, full stop.
    warnings     8 per warning, capped at 40.
    infos        2 per design-quality info nudge, capped at 20.
    space        up to 10 — ground-floor room area / footprint, full marks at
                 the 85% coverage AREA_UNUSED expects, scaling to 10 at 0%.
    circulation  up to 6 — hallway share of interior area; free up to 15%,
                 full penalty at 35% (a plan that is mostly corridor).
    proportion   up to 8 — mean habitable-room elongation past 1.6:1
                 (full penalty at a mean 3.1:1 — long thin rooms don't furnish).
    daylight     up to 8 — mean habitable-room glazing shortfall below the
                 IRC R303 8%-of-floor minimum (full penalty at zero glazing).

Diagnostics dominate (their caps sum to 60 before the error gate); the four
continuous terms (32 max) refine, so two clean plans still rank — the one with
less waste, squarer rooms, and more light wins. The continuous terms overlap
some diagnostics (AREA_UNUSED, HALL_TIGHT, ROOM_PROPORTION, NATURAL_LIGHT) on
purpose: the info gives the step, the margin gives the *gradient* an agent can
descend even before (or after) the threshold trips.

    from barndsl import compile_file
    from barndsl.score import design_score
    print(design_score(compile_file("plan.barn")).total)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .elements import HABITABLE_TYPES, Barndominium, RoomType

# --- weights (the contract; change these and the score changes meaning) ------

WARNING_PENALTY = 8.0  #: points per warning …
WARNING_CAP = 40.0  #: … up to this many
INFO_PENALTY = 2.0  #: points per design-quality info …
INFO_CAP = 20.0  #: … up to this many

SPACE_WEIGHT = 10.0  #: max penalty for unassigned footprint
SPACE_FULL_MARKS = 0.85  #: coverage at/above this loses nothing (AREA_UNUSED's bar)
CIRCULATION_WEIGHT = 6.0  #: max penalty for hallway-heavy plans
CIRCULATION_FREE = 0.15  #: hallway share of interior area with no penalty
CIRCULATION_WORST = 0.35  #: share at/above which the full penalty applies
PROPORTION_WEIGHT = 8.0  #: max penalty for elongated habitable rooms
GOOD_ASPECT = 1.6  #: elongation past this ratio starts to cost (ROOM_PROPORTION's bar)
WORST_ASPECT_EXCESS = 1.5  #: mean excess (i.e. 3.1:1) at which the full penalty applies
DAYLIGHT_WEIGHT = 8.0  #: max penalty for under-glazed habitable rooms
DAYLIGHT_RATIO = 0.08  #: IRC R303 glazing floor: 8% of habitable floor area


@dataclass
class ScoreReport:
    """The scored view of one compile: a total and its explainable parts."""

    total: float
    components: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Stable JSON-able form (mirrors ``CompileResult.to_dict`` style)."""
        return {
            "total": self.total,
            "components": dict(self.components),
            "counts": dict(self.counts),
        }


# --- continuous components (each returns points deducted, 0..weight) ---------


def _space_penalty(plan: Barndominium) -> float:
    """Unassigned footprint. Coverage is a ground-floor concept (lofts sit above)."""
    footprint = plan.footprint_area
    if footprint <= 0:
        return 0.0
    used = sum(r.area for r in plan.rooms if r.level == 0)
    frac = min(1.0, used / footprint)
    shortfall = max(0.0, SPACE_FULL_MARKS - frac) / SPACE_FULL_MARKS
    return SPACE_WEIGHT * min(1.0, shortfall)


def _circulation_penalty(plan: Barndominium) -> float:
    """Hallway share of interior area — corridors are overhead past a point."""
    interior = plan.interior_area
    if interior <= 0:
        return 0.0
    halls = sum(r.area for r in plan.rooms if r.type is RoomType.HALLWAY)
    frac = halls / interior
    band = CIRCULATION_WORST - CIRCULATION_FREE
    return CIRCULATION_WEIGHT * min(1.0, max(0.0, (frac - CIRCULATION_FREE) / band))


def _proportion_penalty(plan: Barndominium) -> float:
    """Mean habitable-room elongation past GOOD_ASPECT (halls/closets exempt)."""
    excesses: list[float] = []
    for r in plan.rooms:
        if r.type not in HABITABLE_TYPES:
            continue
        side = min(r.width, r.length)
        if side <= 0:
            excesses.append(WORST_ASPECT_EXCESS)  # degenerate: as bad as it gets
            continue
        excesses.append(max(0.0, max(r.width, r.length) / side - GOOD_ASPECT))
    if not excesses:
        return 0.0
    mean = sum(excesses) / len(excesses)
    return PROPORTION_WEIGHT * min(1.0, mean / WORST_ASPECT_EXCESS)


def _daylight_penalty(plan: Barndominium) -> float:
    """Mean glazing shortfall below the 8% floor across habitable rooms.

    At/above 8% a room costs nothing (the floor is the target, not a ceiling);
    below it the deficit is proportional, so 5% glazing scores better than 2% —
    a gradient where the pass/fail NATURAL_LIGHT check is a step.
    """
    deficits: list[float] = []
    for r in plan.rooms:
        if r.type not in HABITABLE_TYPES or r.area <= 0:
            continue
        required = DAYLIGHT_RATIO * r.area
        glazed = sum(w.glazed_area for w in plan.windows_for(r.id))
        deficits.append(max(0.0, (required - glazed) / required))
    if not deficits:
        return 0.0
    return DAYLIGHT_WEIGHT * (sum(deficits) / len(deficits))


# --- the score ----------------------------------------------------------------


def design_score(result) -> ScoreReport:
    """Score a :class:`~barndsl.compiler.CompileResult` on the 0–100 contract.

    Deterministic and pure: reads only ``result.diagnostics`` and
    ``result.plan``. An errored (or plan-less) compile scores 0; the continuous
    components are still reported when a plan exists, so an agent fixing errors
    can already see what else needs work.
    """
    counts = {
        "error": len(result.errors),
        "warning": len(result.warnings),
        "info": len(result.infos),
    }
    components: dict[str, float] = {
        "errors": 100.0 if (result.plan is None or result.errors) else 0.0,
        "warnings": min(WARNING_CAP, WARNING_PENALTY * counts["warning"]),
        "infos": min(INFO_CAP, INFO_PENALTY * counts["info"]),
    }
    plan = result.plan
    components["space"] = _space_penalty(plan) if plan is not None else 0.0
    components["circulation"] = _circulation_penalty(plan) if plan is not None else 0.0
    components["proportion"] = _proportion_penalty(plan) if plan is not None else 0.0
    components["daylight"] = _daylight_penalty(plan) if plan is not None else 0.0

    components = {k: round(v, 2) for k, v in components.items()}
    total = max(0.0, min(100.0, 100.0 - sum(components.values())))
    return ScoreReport(total=round(total, 1), components=components, counts=counts)
