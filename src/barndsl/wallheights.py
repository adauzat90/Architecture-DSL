"""Shared vertical extents for lowering wall runs in the 3D exporters.

Both :mod:`barndsl.gltf` and :mod:`barndsl.ifc` lower the *same* Revit-shaped
exchange from :func:`barndsl.revit.to_revit_model`. The exchange carries every
wall run at the clear ceiling height of its own level (``RevitWall.height``) — the
storey plate — because that is the *centreline* fact the pyRevit consumer needs
(it then constrains the wall's top to the level above inside Revit). Extruded
naively into a solid, though, that plate-high box leaves two openings on a
multi-level plan:

* a horizontal **gap band** the depth of the inter-floor assembly, between the top
  of a lower level's walls (its ceiling plane) and the base of the level above
  (ceiling + floor assembly), open all around the perimeter; and
* over a lower level **not covered by an upper floor**, a wall-to-roof **void** —
  the wall stops at its ceiling while the roof bears roughly a storey higher, on
  the top plate.

This module is the one place that corrects those extents, so the two exporters
stay in lockstep. It reads only the exchange (pure, stdlib only) and never
mutates it, so the Revit exchange — and its pyRevit consumers and tests — are
untouched. :func:`wall_top_intervals` gives each wall run as a list of
running-axis intervals, each with the elevation its top should reach:

* a run on the **top** level is unchanged — it already reaches its plate, and the
  roof bears there;
* a lower run **covered** by an upper-level room footprint rises to the **base of
  the level above** (ceiling + floor assembly), closing the gap band — the upper
  level's own wall continues upward from there;
* a lower **exterior** run **not covered** by any upper floor rises to the **top
  plate** (the elevation the roof bears on), closing the wall-to-roof void;
* a lower **interior** run not covered by any upper floor keeps its ceiling height
  — nothing bears on it, so leaving it avoids interior walls poking into open air,
  and keeps single-level plans byte-identical (a single-storey plan has only
  top-level runs, so every interval is the unchanged plate-high box).

Coverage is per **segment**: a single run is split where an upper-level room
rectangle starts or stops covering it (a south wall half under a loft, half not),
so each part extrudes to its own height, and hosted openings are cut correctly
whichever segment they fall in.

:func:`roof_plate` and :func:`gable_line` expose the roof facts the exporters need
to close the gable ends of any exterior run that now reaches the plate.
"""

from __future__ import annotations

from .geometry import TOL
from .revit import RevitModel, RevitWall


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort and union overlapping/adjacent ``(lo, hi)`` intervals."""
    out: list[tuple[float, float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + TOL:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _covered_intervals(w: RevitWall, model: RevitModel) -> list[tuple[float, float]]:
    """Running-axis intervals of ``w`` covered by an upper-level room footprint.

    A lower run is *covered* wherever an upper-level room rectangle lies on its
    line (its constant coordinate falls within the room's extent across the wall)
    and overlaps its run — i.e. an upper floor bears on it there.
    """
    lo, hi = w.span
    c = w.const_coord
    vertical = w.orientation == "v"
    out: list[tuple[float, float]] = []
    for r in model.rooms:
        if r.level <= w.level:
            continue
        if vertical:
            if r.x - TOL <= c <= r.x + r.width + TOL:
                a, b = max(lo, r.y), min(hi, r.y + r.length)
                if b - a > TOL:
                    out.append((a, b))
        elif r.y - TOL <= c <= r.y + r.length + TOL:
            a, b = max(lo, r.x), min(hi, r.x + r.width)
            if b - a > TOL:
                out.append((a, b))
    return _merge(out)


def roof_plate(model: RevitModel) -> float | None:
    """The elevation the roof bears on (the top level's plate), or ``None``.

    Matches the plate the exporters lay the roof at:
    ``top-level elevation + ceiling height``.
    """
    roof = model.roof
    if not roof:
        return None
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    top = roof.get("top_level", max(elev) if elev else 0)
    return elev.get(top, 0.0) + model.ceiling_height


def wall_top_intervals(w: RevitWall, model: RevitModel) -> list[tuple[float, float, float]]:
    """``w`` corrected top elevation as ordered ``(lo, hi, top)`` running intervals.

    The intervals tile the wall's span ``[lo, hi]`` in order with no gaps; each
    carries the ``z`` its top should reach (see the module docstring for the rule).
    """
    lo, hi = w.span
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    base = elev.get(w.level, 0.0)
    ceiling_top = base + w.height
    indices = sorted(elev)
    max_level = indices[-1] if indices else w.level
    if w.level >= max_level:
        return [(lo, hi, ceiling_top)]

    # The level directly above this one: covered runs rise to meet its base.
    above = next((i for i in indices if i > w.level), max_level)
    next_base = elev.get(above, ceiling_top)
    plate = roof_plate(model)
    if plate is None:
        plate = elev.get(max_level, base) + model.ceiling_height
    covered = _covered_intervals(w, model)

    # Break the run at every coverage edge, then classify each piece by its midpoint.
    cuts = {lo, hi}
    for a, b in covered:
        if lo < a < hi:
            cuts.add(a)
        if lo < b < hi:
            cuts.add(b)
    ordered = sorted(cuts)

    out: list[tuple[float, float, float]] = []
    for a, b in zip(ordered, ordered[1:]):
        mid = (a + b) / 2.0
        if any(ca - TOL <= mid <= cb + TOL for ca, cb in covered):
            top = next_base
        elif w.exterior:
            top = float(plate)
        else:
            top = ceiling_top
        if out and abs(out[-1][2] - top) <= TOL and abs(out[-1][1] - a) <= TOL:
            out[-1] = (out[-1][0], b, out[-1][2])  # merge equal-top neighbours
        else:
            out.append((a, b, top))
    return out


def gable_line(model: RevitModel) -> dict | None:
    """Ridge facts for a plain single-block **gable** roof, else ``None``.

    Returns ``{"axis", "ends", "mid", "half", "rise"}`` describing the gable
    triangle so an exporter can close the ends of any exterior wall that reaches
    the plate: a wall is a gable end when its orientation is ``axis`` and its
    constant coordinate is one of ``ends``; the roof underside over its run is
    ``plate + rise * (1 - |s - mid| / half)`` at running position ``s``.

    ``None`` for a shed (no gable ends), a multi-section roof (L/T/U or monitor —
    out of scope here, as for the exchange's own gable marking), or no roof.
    """
    roof = model.roof
    if not roof or roof.get("sections") or roof.get("style") != "gable":
        return None
    xs: list[float] = []
    ys: list[float] = []
    for seg in roof["outline"]:
        for px, py in seg:
            xs.append(px)
            ys.append(py)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    rise = float(roof["rise"])
    if roof["gable_axis"] == "x":  # ridge runs east-west; gable ends are "v" walls
        return {"axis": "v", "ends": (minx, maxx), "mid": (miny + maxy) / 2.0,
                "half": (maxy - miny) / 2.0 or 1.0, "rise": rise}
    # ridge runs north-south; gable ends are "h" walls
    return {"axis": "h", "ends": (miny, maxy), "mid": (minx + maxx) / 2.0,
            "half": (maxx - minx) / 2.0 or 1.0, "rise": rise}


def is_gable_end(w: RevitWall, gl: dict) -> bool:
    """Whether wall ``w`` sits on a gable end described by :func:`gable_line`."""
    return (
        w.orientation == gl["axis"]
        and any(abs(w.const_coord - e) <= 1e-6 for e in gl["ends"])
    )
