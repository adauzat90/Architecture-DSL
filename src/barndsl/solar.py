"""Solar orientation helpers — the bridge from the plan frame to the compass.

barndsl plans are drawn in a **plan-relative** frame (``+y`` is plan-north). A
plan's ``orientation`` is the true-north azimuth (degrees clockwise from true
north) that plan-north points, so a wall's outward face has a true compass
bearing of ``orientation + <the wall's plan bearing>`` (N=0, E=90, S=180, W=270).
Classifying that bearing into a sun-exposure *sector* is what lets the validator
reason about glazing — flag overheating west glass, a room lit only from the cold
north, and so on.

Pure and deterministic; northern-hemisphere (south is the good solar face). The
sector arcs live in :mod:`barndsl.constants`.
"""

from __future__ import annotations

from .constants import SOLAR_EAST_ARC, SOLAR_SOUTH_ARC, SOLAR_WEST_ARC
from .elements import Direction

#: A wall's outward bearing in the plan frame (degrees clockwise from plan-north).
_WALL_BEARING: dict[Direction, float] = {
    Direction.NORTH: 0.0,
    Direction.EAST: 90.0,
    Direction.SOUTH: 180.0,
    Direction.WEST: 270.0,
}

#: 16-point compass, for human/LLM-facing bearings in diagnostics.
_COMPASS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def true_azimuth(wall: Direction, orientation: float) -> float:
    """Compass azimuth (deg CW from true north) that ``wall``'s outward face points.

    ``orientation`` is the azimuth plan-north (``+y``) points; each wall adds its
    plan bearing.
    """
    return (orientation + _WALL_BEARING[wall]) % 360.0


def _in_arc(azimuth: float, arc: tuple[float, float]) -> bool:
    """True if ``azimuth`` is in the half-open ``[lo, hi)`` arc (no wrap here)."""
    lo, hi = arc
    return lo <= azimuth < hi


def solar_sector(azimuth: float) -> str:
    """Classify a compass ``azimuth`` → ``'south' | 'west' | 'east' | 'north'``.

    ``'north'`` is the wrap-around remainder (roughly 300°–60°), so every bearing
    lands in exactly one sector.
    """
    az = azimuth % 360.0
    if _in_arc(az, SOLAR_SOUTH_ARC):
        return "south"
    if _in_arc(az, SOLAR_WEST_ARC):
        return "west"
    if _in_arc(az, SOLAR_EAST_ARC):
        return "east"
    return "north"


def wall_sector(wall: Direction, orientation: float) -> str:
    """The sun-exposure sector of ``wall`` given the plan ``orientation``."""
    return solar_sector(true_azimuth(wall, orientation))


def compass_label(azimuth: float) -> str:
    """A 16-point compass label (e.g. ``'WNW'``) for a bearing, for messages."""
    idx = int((azimuth % 360.0 + 11.25) // 22.5) % 16
    return _COMPASS_16[idx]
