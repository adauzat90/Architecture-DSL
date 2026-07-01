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

# --- daylight (IRC R303) -----------------------------------------------------
#: Glazing must total at least 8% of a habitable room's floor area.
NATURAL_LIGHT_RATIO = 0.08

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
