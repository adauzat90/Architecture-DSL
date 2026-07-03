"""Building-code constants shared across modules — the single source of truth.

These thresholds were previously redefined independently in ``validation.py``
(the rule checker), ``revit.py`` (the stair planner), and the pyRevit extension.
Three copies of the same IRC number drift silently: a fix in one place leaves the
others disagreeing. Centralising them here means a code threshold is stated once.

Values are in feet unless noted. The ``revit`` and ``validation`` modules import
these names, so existing access paths (``barndsl.revit.MAX_RISER_HEIGHT``,
``barndsl.validation.NATURAL_LIGHT_RATIO``, …) keep working unchanged.
"""

from __future__ import annotations

#: Geometry tolerance (feet). Coordinates within this distance are treated as
#: coincident — it absorbs floating-point round-off when comparing edges, offsets
#: and overlaps. Named here so the validator's many bounds checks share one
#: value instead of sprinkling bare ``1e-6`` literals.
EPSILON = 1e-6

# --- stair geometry (IRC R311.7) --------------------------------------------
#: Max riser height: 7-3/4 in. A flight needs enough run to climb one storey.
MAX_RISER_HEIGHT = 7.75 / 12.0
#: Min tread depth: 10 in.
MIN_TREAD_DEPTH = 10.0 / 12.0
#: Min flight width: 36 in. Two side-by-side flights (a switchback) need ~2×.
MIN_STAIR_WIDTH = 3.0
#: A comfortable flight width when the footprint allows more than the minimum.
NICE_STAIR_WIDTH = 3.5
#: Minimum stair headroom, measured vertically from the tread nosing line to any
#: construction above: 6 ft 8 in (IRC R311.7.2). Drives the stairwell-opening
#: feasibility check — a run needs a floor opening long enough to develop this.
STAIR_HEADROOM = 6.0 + 8.0 / 12.0

# --- guards & fall protection (IRC R312) ------------------------------------
#: A walking surface (loft edge, landing, balcony) more than this above the floor
#: below needs a guard: 30 in.
GUARD_DROP_TRIGGER = 30.0 / 12.0
#: Minimum guard height at an open edge: 36 in.
GUARD_HEIGHT = 36.0 / 12.0

# --- daylight (IRC R303) -----------------------------------------------------
#: Glazing must total at least 8% of a habitable room's floor area.
NATURAL_LIGHT_RATIO = 0.08

# --- solar orientation (northern hemisphere) ---------------------------------
# The compass sector a wall's outward face falls into drives the solar-glazing
# nudges. Arcs are chosen so the *hot afternoon quadrant* (SW→W) is one sector,
# not split across "south" and "west" by a naive nearest-cardinal rule. Azimuth
# is degrees clockwise from true north; a wall's azimuth is the plan orientation
# plus the wall's plan bearing (N=0, E=90, S=180, W=270), mod 360.
#: [start, end) half-open arcs, in degrees. "north" is the wrap-around remainder.
SOLAR_SOUTH_ARC = (135.0, 225.0)  # controlled winter gain, easy to shade — good
SOLAR_WEST_ARC = (225.0, 300.0)  # low afternoon sun (SW→W), hard to shade — overheats
SOLAR_EAST_ARC = (60.0, 135.0)  # morning sun — benign
# everything else (300°–360°, 0°–60°) is "north": little direct sun, heat loss.
#: A single room with more than this much west-sector glazing (sq ft) overheats
#: in the afternoon and is hard to shade — the SOLAR_WEST_GAIN threshold. ~a big
#: 5x5 window; a small west window in a bath never trips it.
SOLAR_WEST_MAX_GLAZING = 24.0
#: An exterior south-sector wall run longer than this (ft) with little glazing is
#: a passive-solar face left unused — the SOLAR_SOUTH_UNUSED floor.
SOLAR_SOUTH_MIN_WALL = 16.0
#: If a qualifying south wall carries less south-sector glazing than this (sq ft),
#: the passive-solar opportunity is being wasted — SOLAR_SOUTH_UNUSED. Conservative:
#: fires only when the sunny face is almost blank.
SOLAR_SOUTH_MIN_GLAZING = 12.0
#: South glazing above this (sq ft) that isn't shaded overheats in summer without
#: an eave — the SOLAR_SOUTH_NO_OVERHANG floor. Above SOLAR_SOUTH_MIN_GLAZING so a
#: plan is never told both to add south glass *and* that it has too much unshaded.
SOLAR_SOUTH_SHADE_GLAZING = 24.0
#: A roof overhang at least this deep (ft) reads as real summer shade for south
#: glass — the high winter sun still reaches under it. ~18 in.
MIN_SHADE_OVERHANG = 1.5

# --- habitability / circulation minimums (IRC R304/R305/R311) ----------------
# These were previously defined inline in ``validation.py``; they live here now
# so the jurisdiction-profile layer (``profiles.py``) can build its DEFAULT
# thresholds from one source without importing the validator (which would be a
# cycle). ``validation.py`` still imports them by the same names, so the check
# code reads unchanged.
#: Minimum ceiling height for habitable space: 7 ft (IRC R305.1).
MIN_CEILING = 7.0
#: Minimum floor area of a habitable room (a bedroom): 70 sq ft (IRC R304.1).
MIN_BEDROOM_AREA = 70.0
#: Minimum horizontal dimension of a habitable room: 7 ft (IRC R304.2).
MIN_BEDROOM_DIMENSION = 7.0
#: Minimum hallway width: 36 in — a hard code minimum (IRC R311.6).
MIN_HALLWAY_WIDTH = 3.0
#: A hall under this passes code but feels tight (comfort target, not code).
COMFORT_HALLWAY_WIDTH = 4.0

# --- emergency escape openings (IRC R310) ------------------------------------
#: Net clear escape opening on an upper floor: 5.7 sq ft.
MIN_EGRESS_AREA = 5.7
#: Net clear escape opening at grade (level 0): 5.0 sq ft (IRC R310.2.1 exc).
MIN_EGRESS_AREA_GRADE = 5.0
#: Minimum clear opening width / height: 20 in / 24 in.
MIN_EGRESS_OPENING_WIDTH = 20 / 12
MIN_EGRESS_OPENING_HEIGHT = 24 / 12
#: Maximum sill height above the finished floor: 44 in.
MAX_EGRESS_SILL = 44 / 12

# --- vertical assembly -------------------------------------------------------
#: Depth (feet) of the inter-floor assembly between two stacked levels — the
#: joists/trusses + subfloor + ceiling finish that a *ceiling height* alone
#: ignores. Floor-to-floor = ceiling height + this, so an upper level sits on
#: top of the level below's structure rather than directly on its ceiling plane.
#: A modest 12 in default for a residential engineered-joist floor.
FLOOR_ASSEMBLY_DEPTH = 12.0 / 12.0

#: Default roof pitch (rise:run) for a barndominium gable — a modest 4:12.
DEFAULT_ROOF_PITCH = 4.0 / 12.0

# --- wall thicknesses (nominal) ----------------------------------------------
#: Nominal built thickness (feet) of an exterior shell wall (~2x6 + sheathing +
#: cladding) and an interior partition (~2x4 + gypsum both sides). barndsl rooms
#: tile on wall *centrelines*, so a room's built clear interior is its nominal
#: rectangle minus half of each bounding wall — an exterior edge costs more than
#: an interior one. Used to pick a Revit wall type *and* to derive clear
#: dimensions (the dimension IRC minimums are measured to, and the one Revit's
#: room schedule reports), so both agree on one number.
EXTERIOR_WALL_THICKNESS = 6.5 / 12.0
INTERIOR_WALL_THICKNESS = 4.5 / 12.0
#: Nominal thickness (feet) of a declared **plumbing (wet) wall** — a 2x6
#: partition deep enough to carry a 3 in waste stack (`wall a - b plumbing`).
#: 6.5 in follows the same convention as the interior 2x4 partition above
#: (stud depth + half-inch gypsum each face: 5.5 + 2 x 0.5). Rooms flanking a
#: declared plumbing wall lose half of this (instead of half an ordinary
#: partition) from their clear dimensions, and the Revit exchange hints the
#: thicker wall type on the matching segments.
PLUMBING_WALL_THICKNESS = 6.5 / 12.0

# --- foundation (monolithic slab-on-grade) -----------------------------------
#: The barndominium foundation is typically a monolithic slab-on-grade: a 4 in
#: slab with a thickened perimeter edge (turndown / grade beam) carried below the
#: frost line, plus a pad footing under each post of a post-frame building. These
#: are conservative defaults (feet); the real depths follow the frost line and
#: the soil report — an engineer's call, so the check is a takeoff, not a design.
SLAB_THICKNESS = 4.0 / 12.0
#: Width and depth (below the slab underside) of the thickened perimeter edge.
TURNDOWN_WIDTH = 12.0 / 12.0
TURNDOWN_DEPTH = 12.0 / 12.0
#: Plan size and depth of a square pad footing under a post.
FOOTING_SIZE = 2.0
FOOTING_DEPTH = 12.0 / 12.0
