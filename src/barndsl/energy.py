"""Thermal-envelope reference data — IECC climate zones and prescriptive R-values.

The compiler can't run an energy model, but it *can* tell an author what the
prescriptive envelope for their climate zone is and flag the two things a
barndominium most often gets wrong: an over-glazed wall (heating/cooling load)
and a steel frame insulated in the cavity only (a severe thermal bridge). This
module is the pure reference table + helpers; :func:`barndsl.validation` turns it
into the ``ENERGY_ENVELOPE`` / ``WINDOW_HEAVY`` nudges, gated on a declared
``climate``.

Values are representative **2021 IECC / IRC Chapter 11** residential prescriptive
minimums (Table R402.1.3-ish), rounded and A/B/C-subzone-collapsed — guidance, not
the code of record. Confirm with the adopted energy code and, ideally, a rater.
"""

from __future__ import annotations

#: The IECC climate zones this reference covers (1 warmest … 8 coldest).
CLIMATE_ZONES = tuple(range(1, 9))

#: Glazing area above this fraction of the gross exterior wall area is "window
#: heavy" — a high window-to-wall ratio that drives the heating/cooling load. The
#: daylight *floor* (NAT_LIGHT, 8%) has always been checked; this is the ceiling.
WWR_CEILING = 0.28

#: Per-zone prescriptive envelope targets. ``wall`` is a string so it can carry the
#: cavity-plus-continuous-insulation ("+Nci") form the code uses for colder zones.
ENVELOPE_TARGETS: dict[int, dict] = {
    1: {"ceiling": "R-30", "wall": "R-13", "floor": "R-13", "slab": None, "window_u": 0.50},
    2: {"ceiling": "R-49", "wall": "R-13", "floor": "R-13", "slab": None, "window_u": 0.40},
    3: {"ceiling": "R-49", "wall": "R-20 (or R-13+5 c.i.)", "floor": "R-19", "slab": None, "window_u": 0.30},
    4: {"ceiling": "R-60", "wall": "R-30 (or R-20+5 c.i.)", "floor": "R-19", "slab": "R-10 to 2 ft", "window_u": 0.30},
    5: {"ceiling": "R-60", "wall": "R-30 (or R-20+5 c.i.)", "floor": "R-30", "slab": "R-10 to 2 ft", "window_u": 0.30},
    6: {"ceiling": "R-60", "wall": "R-30 (or R-20+5 c.i.)", "floor": "R-30", "slab": "R-10 to 4 ft", "window_u": 0.30},
    7: {"ceiling": "R-60", "wall": "R-30 (or R-20+5 c.i.)", "floor": "R-38", "slab": "R-10 to 4 ft", "window_u": 0.30},
    8: {"ceiling": "R-60", "wall": "R-30 (or R-20+5 c.i.)", "floor": "R-38", "slab": "R-10 to 4 ft", "window_u": 0.30},
}


def envelope_targets(zone: int) -> dict:
    """The prescriptive envelope targets for an IECC ``zone`` (1–8)."""
    return ENVELOPE_TARGETS[int(zone)]


def describe_targets(zone: int) -> str:
    """A one-line summary of a zone's prescriptive envelope, for a diagnostic."""
    t = envelope_targets(zone)
    parts = [
        f"ceiling {t['ceiling']}",
        f"walls {t['wall']}",
        f"floor {t['floor']}",
    ]
    if t["slab"]:
        parts.append(f"slab edge {t['slab']}")
    parts.append(f"windows U-{t['window_u']:g}")
    return ", ".join(parts)
