"""A transparent, assembly-based construction cost estimate for a compiled plan.

`barndsl cost FILE` turns the takeoff the compiler already computes into a
budget: every line is ``quantity × unit cost`` with the quantity's **source
named**, grouped into the assemblies a builder recognises (foundation, shell,
partitions, openings, plumbing/fixtures, systems, finishes). It is the missing
half of `metrics()` — the takeoff says *how much*, this says *roughly what it
costs* — so an agent (or an architect) can optimise value, not just square feet.

The estimate is **rough on purpose**: the unit costs in :data:`DEFAULT_UNIT_COSTS`
are order-of-magnitude 2026 US national averages for budgeting, not a bid. Every
quantity is reused from :meth:`Barndominium.metrics`, the room/opening lists, and
the fixtures/geometry helpers — nothing recomputes geometry from scratch — so the
same plan in always gives the same numbers out (pure, deterministic, JSON-safe).

    from barndsl import compile_file, estimate_cost, cost_text
    print(cost_text(estimate_cost(compile_file("plan.barn"))))
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .constants import DEFAULT_ROOF_PITCH
from .elements import Barndominium
from .fixtures import fixtures_for
from .geometry import shared_edge

#: Shown in both the text and JSON output — this is a budget aid, not a bid.
DISCLAIMER = (
    "Rough 2026 US national averages for budgeting only — NOT a bid. Real costs "
    "vary widely by region, finish level, site conditions, and market. Confirm "
    "with local quotes before committing."
)

#: The +/- band applied to the expected total to give a low/high range (%).
BAND_PCT = 15.0

#: Default unit costs (USD), rough 2026 US national averages for budgeting only.
#: Override any subset via ``estimate_cost(..., overrides={...})`` or the CLI's
#: ``--costs FILE.json``; scale the whole sheet with ``--multiplier`` (region).
DEFAULT_UNIT_COSTS: dict[str, float] = {
    # -- shell / structure (area or length) --
    "slab_sqft": 9.0,  # monolithic slab-on-grade, per footprint sqft
    "exterior_wall_sqft": 18.0,  # framing+sheathing+insulation+siding, gross wall area
    "interior_wall_lf": 58.0,  # framed+drywalled partition, per linear foot
    "roof_sqft": 10.0,  # structure+decking+covering, per sloped sqft
    # -- openings (each, installed) --
    "window_casement": 780.0,
    "window_slider": 640.0,
    "window_double_hung": 700.0,
    "window_fixed": 520.0,
    "door_interior": 360.0,
    "door_interior_double": 620.0,  # double / french interior pair
    "door_exterior": 1500.0,  # single entry/exterior leaf
    "door_exterior_double": 2800.0,  # double / french pair
    "garage_door": 1600.0,  # overhead sectional
    # -- plumbing fixtures & kitchen appliances (each, supply+waste+fixture) --
    "fixture_toilet": 520.0,
    "fixture_lavatory": 460.0,
    "fixture_tub": 1250.0,
    "fixture_shower": 1450.0,
    "fixture_sink": 950.0,  # kitchen sink + rough-in
    "fixture_range": 1300.0,  # appliance allowance
    "fixture_refrigerator": 1700.0,  # appliance allowance
    "fixture_washer": 700.0,  # washer hookup (supply/drain box) + appliance allowance
    "fixture_dryer": 650.0,  # dryer 240 V/gas + vent run + appliance allowance
    "countertop_lf": 75.0,  # fabricated + installed countertop, per linear foot
    # -- per-conditioned-sqft allowances --
    "electrical_sqft": 9.0,
    "hvac_sqft": 8.0,
    "finish_sqft": 32.0,  # flooring, trim, paint, cabinets, per conditioned sqft
}

#: Window ``kind`` (see :data:`barndsl.elements.WINDOW_KINDS`) → unit-cost key.
_WINDOW_KEY = {
    "casement": "window_casement",
    "slider": "window_slider",
    "double-hung": "window_double_hung",
    "fixed": "window_fixed",
}

#: Fixture kind (see :data:`barndsl.fixtures.FIXTURES`) → unit-cost key, in the
#: order lines are emitted (deterministic).
_FIXTURE_ORDER = (
    "toilet", "lavatory", "tub", "shower", "sink", "range", "refrigerator",
    "washer", "dryer",
)

#: Line the estimate ends with — the assemblies it does NOT price. Kept honest
#: against what actually has a line above (see :func:`estimate_cost`): plumbing
#: fixtures and an HVAC allowance ARE itemised, so they are qualified here.
EXCLUSIONS = (
    "Excludes: site work, well/septic, permits, mechanical (HVAC) unless "
    "itemized, GC overhead & profit."
)


def _as_plan(plan_or_result: Any) -> Barndominium:
    """Accept a compiled ``Barndominium`` or a ``CompileResult`` and return the
    plan; raise ``ValueError`` when there is no plan to estimate."""
    if isinstance(plan_or_result, Barndominium):
        return plan_or_result
    plan = getattr(plan_or_result, "plan", None)
    if plan is None:
        raise ValueError("cannot estimate cost: the source did not compile to a plan")
    return plan


def _interior_wall_lf(plan: Barndominium) -> float:
    """Linear feet of interior partition: the length of every wall shared by two
    rooms (each party wall counted once), via the compiler's own ``shared_edge``."""
    total = 0.0
    rooms = plan.rooms
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            a, b = rooms[i], rooms[j]
            if a.level != b.level:
                continue
            edge = shared_edge(a, b)
            if edge is not None:
                total += edge.length
    return total


def _fixture_counts(plan: Barndominium) -> Counter:
    """Plumbing fixtures / appliances implied by the rooms (kitchens & baths carry
    them; other rooms carry none), reusing ``fixtures.fixtures_for``."""
    counts: Counter = Counter()
    for room in plan.rooms:
        for kind in fixtures_for(room.type):
            counts[kind] += 1
    return counts


def _line(
    group: str, item: str, quantity: float, unit: str, key: str, source: str,
    unit_costs: dict[str, float], multiplier: float,
) -> dict[str, Any]:
    """Build one assembly line: ``cost = quantity × unit_cost``.

    ``unit_cost`` is the *effective* rate — base rate × regional multiplier —
    and both quantity and unit cost are rounded to what the table shows
    *before* the product, so the printed line reads exactly (shown qty ×
    shown unit = cost) at any multiplier.
    """
    qty = round(quantity, 2)
    unit_cost = round(float(unit_costs[key]) * multiplier, 2)
    cost = round(qty * unit_cost, 2)
    return {
        "group": group,
        "item": item,
        "quantity": qty,
        "unit": unit,
        "cost_key": key,
        "unit_cost": unit_cost,
        "cost": cost,
        "source": source,
    }


def estimate_cost(
    plan_or_result: Any,
    overrides: dict[str, float] | None = None,
    multiplier: float = 1.0,
) -> dict[str, Any]:
    """A transparent assembly cost estimate as a JSON-safe dict.

    ``plan_or_result`` is a compiled :class:`~barndsl.elements.Barndominium` or a
    :class:`~barndsl.compiler.CompileResult`. ``overrides`` replace any subset of
    :data:`DEFAULT_UNIT_COSTS`; ``multiplier`` is a regional factor scaling every
    unit cost. Returns ``{plan, currency, multiplier, band_pct, disclaimer,
    assemblies, subtotals, total{low,expected,high}}`` — each assembly line names
    the quantity's source, and ``total`` carries a +/-:data:`BAND_PCT` range.
    """
    plan = _as_plan(plan_or_result)
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")
    unit_costs = dict(DEFAULT_UNIT_COSTS)
    if overrides:
        unknown = sorted(set(overrides) - set(unit_costs))
        if unknown:
            raise ValueError(
                "unknown unit-cost key(s): %s — valid keys are the "
                "DEFAULT_UNIT_COSTS entries" % ", ".join(unknown)
            )
        unit_costs.update({k: float(v) for k, v in overrides.items()})

    m = plan.metrics()
    lines: list[dict[str, Any]] = []

    def add(group, item, qty, unit, key, source):
        if qty > 0:
            lines.append(_line(group, item, qty, unit, key, source, unit_costs, multiplier))

    # -- Foundation --
    add("Foundation", "Slab-on-grade", m["footprint_sqft"], "sqft", "slab_sqft",
        "metrics: footprint_sqft")
    # Every porch (covered or open) is a platform on its own slab, at the same
    # slab rate as the house floor.
    add("Foundation", "Porch slab", m.get("porch_sqft", 0.0), "sqft", "slab_sqft",
        "metrics: porch_sqft (porch platforms)")

    # -- Shell --
    add("Shell", "Exterior walls", m["exterior_wall_area_sqft"], "sqft",
        "exterior_wall_sqft", "metrics: exterior_wall_area_sqft")
    # A gable roof adds a triangle of wall at each gable end (the two walls the
    # ridge runs *between*): base = envelope width, rise = half-span × pitch, so
    # one triangle is width²·pitch/4 and the pair is width²·pitch/2. Sheathed and
    # sided like the rest of the shell, so priced at the exterior-wall rate.
    if plan.roof_style == "gable":
        pitch = plan.roof_pitch if plan.roof_pitch is not None else DEFAULT_ROOF_PITCH
        gable_area = plan.envelope_width * plan.envelope_width * pitch / 2.0
        add("Shell", "Gable-end walls", gable_area, "sqft", "exterior_wall_sqft",
            "gable ends: envelope width & roof pitch")
    add("Shell", "Roof", m["roof_area_sqft"], "sqft", "roof_sqft",
        "metrics: roof_area_sqft (sloped)")
    # Covered porches carry their own roof (already sloped by the pitch), at the
    # same roof rate as the main roof.
    add("Shell", "Porch roof", m.get("covered_porch_roof_sqft", 0.0), "sqft",
        "roof_sqft", "metrics: covered_porch_roof_sqft (sloped)")

    # -- Partitions --
    add("Partitions", "Interior partition walls", _interior_wall_lf(plan), "lf",
        "interior_wall_lf", "geometry: shared walls between rooms")

    # -- Openings --
    wk = Counter(getattr(w, "kind", "casement") for w in plan.windows)
    for kind in ("casement", "slider", "double-hung", "fixed"):
        n = wk.get(kind, 0)
        add("Openings", f"Windows ({kind})", n, "each", _WINDOW_KEY[kind],
            f"plan.windows kind={kind}")
    xd = plan.exterior_doors
    n_single = sum(1 for d in xd if getattr(d, "kind", "entry") in ("entry",))
    n_double = sum(1 for d in xd if getattr(d, "kind", "entry") in ("double", "french"))
    n_garage = sum(1 for d in xd if getattr(d, "kind", "entry") == "overhead")
    add("Openings", "Exterior doors", n_single, "each", "door_exterior",
        "plan.exterior_doors (single)")
    add("Openings", "Exterior doors (double/french)", n_double, "each",
        "door_exterior_double", "plan.exterior_doors (double/french)")
    add("Openings", "Overhead (garage) doors", n_garage, "each", "garage_door",
        "plan.exterior_doors (overhead)")
    ints = [d for d in plan.interior_doors if getattr(d, "kind", "swing") != "cased"]
    n_int_double = sum(
        1 for d in ints if getattr(d, "kind", "swing") in ("double", "french")
    )
    add("Openings", "Interior doors", len(ints) - n_int_double, "each",
        "door_interior", "plan.interior_doors (excl. cased openings)")
    add("Openings", "Interior doors (double/french)", n_int_double, "each",
        "door_interior_double", "plan.interior_doors (double/french)")

    # -- Plumbing & fixtures --
    fc = _fixture_counts(plan)
    for kind in _FIXTURE_ORDER:
        add("Plumbing & fixtures", kind.title(), fc.get(kind, 0), "each",
            f"fixture_{kind}", f"fixtures for wet/kitchen rooms ({kind})")
    add("Plumbing & fixtures", "Countertops", m["counter_linear_ft"], "lf",
        "countertop_lf", "metrics: counter_linear_ft (placed counter runs)")

    # -- Systems (per conditioned interior sqft) --
    cond = m["interior_sqft"]
    add("Systems", "Electrical allowance", cond, "sqft", "electrical_sqft",
        "metrics: interior_sqft")
    add("Systems", "HVAC allowance", cond, "sqft", "hvac_sqft",
        "metrics: interior_sqft")

    # -- Finishes (per conditioned interior sqft) --
    add("Finishes", "Interior finish allowance", cond, "sqft", "finish_sqft",
        "metrics: interior_sqft")

    subtotals: dict[str, float] = {}
    for ln in lines:
        subtotals[ln["group"]] = round(subtotals.get(ln["group"], 0.0) + ln["cost"], 2)
    expected = round(sum(ln["cost"] for ln in lines), 2)
    band = BAND_PCT / 100.0
    return {
        "plan": plan.name,
        "currency": "USD",
        "multiplier": multiplier,
        "band_pct": BAND_PCT,
        "disclaimer": DISCLAIMER,
        "exclusions": EXCLUSIONS,
        "assemblies": lines,
        "subtotals": subtotals,
        "total": {
            "low": round(expected * (1.0 - band), 2),
            "expected": expected,
            "high": round(expected * (1.0 + band), 2),
        },
    }


def _money(v: float) -> str:
    return f"${v:,.0f}"


def cost_text(est: dict[str, Any]) -> str:
    """Render an :func:`estimate_cost` dict as a human-readable table."""
    lines = [
        f"Cost estimate — {est['plan']}  ({est['currency']})",
    ]
    if est["multiplier"] != 1.0:
        lines.append(f"Regional multiplier: x{est['multiplier']:g}")
    lines.append("")
    header = f"  {'Assembly':<34}{'Qty':>12}  {'Unit cost':>11}  {'Cost':>12}"
    last_group: str | None = None
    for ln in est["assemblies"]:
        if ln["group"] != last_group:
            last_group = ln["group"]
            lines.append(f"{ln['group']}")
            lines.append(header)
        qty = f"{ln['quantity']:g} {ln['unit']}"
        lines.append(
            f"  {ln['item']:<34}{qty:>12}  {_money(ln['unit_cost']):>11}  "
            f"{_money(ln['cost']):>12}"
        )
        lines.append(f"      ↳ {ln['source']}")
    lines.append("")
    lines.append("Subtotals:")
    for group, subtotal in est["subtotals"].items():
        lines.append(f"  {group:<34}{_money(subtotal):>39}")
    t = est["total"]
    lines.append("")
    lines.append(
        f"Estimated total: {_money(t['expected'])}   "
        f"(range {_money(t['low'])} – {_money(t['high'])}, "
        f"+/-{est['band_pct']:g}%)"
    )
    lines.append("")
    lines.append(est.get("exclusions", EXCLUSIONS))
    lines.append("")
    lines.append(f"NOTE: {est['disclaimer']}")
    return "\n".join(lines)
