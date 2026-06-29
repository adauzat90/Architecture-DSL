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
