"""A transparent, deterministic material/cost estimate from the plan's takeoff.

The compiler already computes a full quantity takeoff (:meth:`Barndominium.metrics`
plus the room/opening model); this turns it into a **rough order-of-magnitude**
budget number — unit cost × quantity, per line item, with a user-overridable rate
table. No API key, same plan in / same number out. Every rate is stated in
:data:`DEFAULT_RATES` and can be replaced with a JSON sidecar, so the estimate is
auditable, not a black box.

**Not a bid.** These are representative U.S. rates for a finished barndominium;
they exclude site work, utilities to the property, permits/fees, design, well/
septic, and regional labor swings, and every real project varies. Override the
rates for your market.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .elements import Barndominium, RoomType

#: Default unit rates (US dollars). Replace any subset via ``estimate_cost(plan,
#: rates=...)`` or a ``--rates`` JSON sidecar. ``$/sqft`` items multiply an area;
#: ``$/ft`` a length; ``$ each`` a count.
DEFAULT_RATES: dict[str, float] = {
    "foundation": 7.0,       # $/sqft of footprint — monolithic slab-on-grade
    "frame_beam": 22.0,      # $/linear ft of primary beam/bent
    "frame_post": 150.0,     # $ per structural post
    "roof": 9.0,             # $/sqft of roof area — standing-seam metal, installed
    "shell": 16.0,           # $/sqft of exterior wall — metal siding + framing + insulation
    "interior": 38.0,        # $/sqft conditioned — MEP rough-in, drywall, floors, trim, paint
    "window": 650.0,         # $ each
    "ext_door": 1400.0,      # $ each — exterior people-door
    "overhead_door": 1600.0, # $ each — sectional/overhead garage door
    "int_door": 350.0,       # $ each — interior leaf door
    "kitchen": 14000.0,      # $ each — cabinets, counters, appliance allowance
    "bathroom": 7500.0,      # $ each — full bath fixtures/finishes
    "half_bath": 4000.0,     # $ each — half bath
    "porch": 16.0,           # $/sqft of porch — slab + light framing
}


@dataclass
class CostLine:
    """One estimate line: a quantity at a unit rate."""

    item: str
    qty: float
    unit: str
    rate: float

    @property
    def cost(self) -> float:
        return self.qty * self.rate


@dataclass
class CostReport:
    """A costed takeoff: the line items and their subtotal."""

    lines: list[CostLine] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return sum(line.cost for line in self.lines)

    def to_dict(self) -> dict:
        return {
            "lines": [
                {
                    "item": line.item,
                    "qty": round(line.qty, 2),
                    "unit": line.unit,
                    "rate": line.rate,
                    "cost": round(line.cost, 2),
                }
                for line in self.lines
            ],
            "subtotal": round(self.subtotal, 2),
        }

    def table(self) -> str:
        """A plain-text aligned table with the subtotal and a disclaimer."""
        header = f"{'Item':<22}{'Qty':>12}  {'Rate':>10}  {'Cost':>12}"
        rows = [header, "-" * len(header)]
        for line in self.lines:
            qty = f"{line.qty:,.0f} {line.unit}"
            rate = f"${line.rate:,.0f}"
            rows.append(
                f"{line.item:<22}{qty:>12}  {rate:>10}  {'$' + format(line.cost, ',.0f'):>12}"
            )
        rows.append("-" * len(header))
        rows.append(f"{'Estimated subtotal':<22}{'':>12}  {'':>10}  "
                    f"{'$' + format(self.subtotal, ',.0f'):>12}")
        rows.append("")
        rows.append("Rough order-of-magnitude, not a bid. Excludes site work, "
                    "utilities, permits,")
        rows.append("design, well/septic and regional variation. Override rates "
                    "with --rates.")
        return "\n".join(rows)


def estimate_cost(
    plan: Barndominium, rates: dict[str, float] | None = None
) -> CostReport:
    """Estimate ``plan``'s cost from its takeoff. ``rates`` overrides any subset of
    :data:`DEFAULT_RATES`; missing keys fall back to the default."""
    r = {**DEFAULT_RATES, **(rates or {})}
    m = plan.metrics()

    kitchens = sum(1 for room in plan.rooms if room.type is RoomType.KITCHEN)
    full_baths = sum(1 for room in plan.rooms if room.type is RoomType.BATHROOM)
    half_baths = sum(1 for room in plan.rooms if room.type is RoomType.HALF_BATH)
    overhead = sum(1 for d in plan.exterior_doors if d.overhead)
    ext_people = sum(1 for d in plan.exterior_doors if not d.overhead)
    # Cased openings have no leaf, so they aren't a door cost.
    leaf_doors = sum(1 for d in plan.interior_doors if getattr(d, "kind", "swing") != "cased")
    porch_area = sum(p.area for p in plan.porches)

    # (item, unit, quantity, rate-key) — a line is emitted only if qty > 0.
    candidates = [
        ("Foundation (slab)", "sqft", m["footprint_sqft"], "foundation"),
        ("Structural frame", "ft", m["beam_linear_ft"], "frame_beam"),
        ("Structural posts", "ea", m["post_count"], "frame_post"),
        ("Roof (metal)", "sqft", m["roof_area_sqft"] + m["covered_porch_roof_sqft"], "roof"),
        ("Exterior shell", "sqft", m["exterior_wall_area_sqft"], "shell"),
        ("Interior finish", "sqft", m["interior_sqft"], "interior"),
        ("Windows", "ea", float(len(plan.windows)), "window"),
        ("Exterior doors", "ea", float(ext_people), "ext_door"),
        ("Overhead doors", "ea", float(overhead), "overhead_door"),
        ("Interior doors", "ea", float(leaf_doors), "int_door"),
        ("Kitchen", "ea", float(kitchens), "kitchen"),
        ("Full baths", "ea", float(full_baths), "bathroom"),
        ("Half baths", "ea", float(half_baths), "half_bath"),
        ("Porch", "sqft", porch_area, "porch"),
    ]
    lines = [
        CostLine(item, qty, unit, r[key])
        for (item, unit, qty, key) in candidates
        if qty > 0
    ]
    return CostReport(lines)
