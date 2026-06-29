# -*- coding: utf-8 -*-
"""Create Revit elements from a ``barndsl.revit/1`` exchange.

This module talks to the **Revit API**, so it only imports cleanly *inside*
Revit (under pyRevit). Import it lazily from a pushbutton script — never from the
Revit-free :mod:`barndsl_revit.exchange`.

What it builds, in one transaction:

* **Levels** — reuse an existing level at the same elevation, else create one.
* **Walls** — a wall per exchange segment, on its level, at its height, using an
  *exterior* or *interior* wall type chosen from the project (by ``WallFunction``).
* **Doors / windows** — a hosted family instance on the matched wall, using the
  first loaded door/window family symbol; window sill heights are applied.
* **Rooms** — placed at each seed point once the walls enclose it, then named.
* **Structure** (best effort) — structural columns at posts and framing along
  beams, *if* suitable structural family symbols are loaded; skipped with a note
  otherwise.

Everything is defensive: a single element that fails to create is recorded as a
warning and the build continues, so one missing family can't abort the whole run.

Coordinates and units pass straight through — the exchange is in feet, which is
Revit's internal unit, with ``x`` east / ``y`` north matching Revit's world XY.
"""

from pyrevit import DB, revit

from . import exchange as _exchange

_TOL = 1e-6


class BuildSummary(object):
    """Counts of what got created, plus any per-element warnings."""

    def __init__(self):
        self.levels = 0
        self.walls = 0
        self.doors = 0
        self.windows = 0
        self.rooms = 0
        self.columns = 0
        self.framing = 0
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)

    def as_line(self):
        return (
            "%d level(s), %d wall(s), %d door(s), %d window(s), %d room(s), "
            "%d column(s), %d framing"
            % (
                self.levels,
                self.walls,
                self.doors,
                self.windows,
                self.rooms,
                self.columns,
                self.framing,
            )
        )


# --- helpers -----------------------------------------------------------------


def _xyz(point, z):
    return DB.XYZ(float(point[0]), float(point[1]), float(z))


def _collect(doc, of_class):
    return DB.FilteredElementCollector(doc).OfClass(of_class).ToElements()


def _pick_wall_types(doc):
    """Return ``(exterior_type, interior_type)`` — the best wall types available.

    Chooses by ``WallFunction`` where set, falling back to any basic wall type so
    the build still runs in a stripped template.
    """
    exterior = interior = fallback = None
    for wt in _collect(doc, DB.WallType):
        # Only basic (line-based) walls; skip curtain/stacked types.
        if wt.Kind != DB.WallKind.Basic:
            continue
        if fallback is None:
            fallback = wt
        try:
            fn = wt.Function
        except Exception:
            fn = None
        if fn == DB.WallFunction.Exterior and exterior is None:
            exterior = wt
        elif fn == DB.WallFunction.Interior and interior is None:
            interior = wt
    exterior = exterior or fallback
    interior = interior or fallback
    return exterior, interior


def _ensure_levels(doc, data, summary):
    """Map each exchange level index to a Revit :class:`Level` (reuse or create)."""
    existing = list(_collect(doc, DB.Level))
    out = {}
    for lvl in data["levels"]:
        elev = float(lvl["elevation"])
        match = None
        for e in existing:
            if abs(e.Elevation - elev) <= 1e-3:
                match = e
                break
        if match is None:
            match = DB.Level.Create(doc, elev)
            existing.append(match)
            summary.levels += 1
            # Name it from the exchange when that name is free.
            try:
                match.Name = lvl["name"]
            except Exception:
                pass
        out[lvl["index"]] = match
    return out


def _activate(symbol, doc):
    if symbol is not None and not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()
    return symbol


def _first_symbol(doc, bic):
    """First loaded :class:`FamilySymbol` of a built-in category, or ``None``."""
    col = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.FamilySymbol)
        .OfCategory(bic)
        .ToElements()
    )
    return col[0] if col else None


# --- element passes ----------------------------------------------------------


def _build_walls(doc, data, levels, ext_type, int_type, summary):
    """Create walls; return ``{exchange_wall_id: Wall}`` for opening hosting."""
    made = {}
    for w in data["walls"]:
        level = levels.get(w["level"])
        if level is None:
            summary.warn("wall %s: no level %r" % (w["id"], w["level"]))
            continue
        z = level.Elevation
        try:
            curve = DB.Line.CreateBound(_xyz(w["start"], z), _xyz(w["end"], z))
        except Exception:
            summary.warn("wall %s: degenerate segment, skipped" % w["id"])
            continue
        wtype = ext_type if w.get("exterior") else int_type
        try:
            wall = DB.Wall.Create(
                doc,
                curve,
                wtype.Id,
                level.Id,
                float(w["height"]),
                0.0,
                False,
                False,
            )
            made[w["id"]] = wall
            summary.walls += 1
        except Exception as exc:
            summary.warn("wall %s: %s" % (w["id"], exc))
    return made


def _build_openings(doc, data, levels, walls, summary):
    door_sym = _activate(_first_symbol(doc, DB.BuiltInCategory.OST_Doors), doc)
    win_sym = _activate(_first_symbol(doc, DB.BuiltInCategory.OST_Windows), doc)
    st = DB.Structure.StructuralType.NonStructural

    for o in data["openings"]:
        host = walls.get(o.get("host_wall"))
        if host is None:
            summary.warn("opening %s: no host wall, skipped" % o["id"])
            continue
        level = levels.get(o["level"])
        is_window = o["category"] == "window"
        sym = win_sym if is_window else door_sym
        if sym is None:
            summary.warn(
                "opening %s: no %s family loaded, skipped"
                % (o["id"], "window" if is_window else "door")
            )
            continue
        point = _xyz(o["location"], level.Elevation if level else 0.0)
        try:
            inst = doc.Create.NewFamilyInstance(point, sym, host, level, st)
        except Exception as exc:
            summary.warn("opening %s: %s" % (o["id"], exc))
            continue
        if is_window:
            try:
                p = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
                if p is not None and not p.IsReadOnly:
                    p.Set(float(o.get("sill", 0.0)))
            except Exception:
                pass
            summary.windows += 1
        else:
            summary.doors += 1
    # The exchange carries each opening's width, but family widths are a type
    # property; the instances use their family's default size. Note it once.
    if data["openings"]:
        summary.warn(
            "openings use their family's default width/height; "
            "swap to a sized family type in Revit if needed"
        )


def _build_rooms(doc, data, levels, summary):
    for r in data["rooms"]:
        level = levels.get(r["level"])
        if level is None:
            summary.warn("room %s: no level %r" % (r["id"], r["level"]))
            continue
        uv = DB.UV(float(r["point"][0]), float(r["point"][1]))
        try:
            room = doc.Create.NewRoom(level, uv)
        except Exception as exc:
            summary.warn(
                "room %s: could not place (walls may not enclose it): %s"
                % (r["id"], exc)
            )
            continue
        if room is None:
            summary.warn("room %s: point not in an enclosed region" % r["id"])
            continue
        try:
            p = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
            if p is not None and not p.IsReadOnly:
                p.Set(r.get("name", r["id"]))
        except Exception:
            pass
        summary.rooms += 1


def _build_structure(doc, data, levels, summary):
    structure = data.get("structure", {})
    columns = structure.get("columns", [])
    framing = structure.get("framing", [])
    if not columns and not framing:
        return

    col_sym = _activate(_first_symbol(doc, DB.BuiltInCategory.OST_StructuralColumns), doc)
    beam_sym = _activate(_first_symbol(doc, DB.BuiltInCategory.OST_StructuralFraming), doc)

    if columns and col_sym is None:
        summary.warn("structural columns skipped: no structural-column family loaded")
    for c in columns:
        if col_sym is None:
            break
        level = levels.get(c["level"])
        if level is None:
            continue
        try:
            doc.Create.NewFamilyInstance(
                _xyz(c["point"], level.Elevation),
                col_sym,
                level,
                DB.Structure.StructuralType.Column,
            )
            summary.columns += 1
        except Exception as exc:
            summary.warn("column at %s: %s" % (c["point"], exc))

    if framing and beam_sym is None:
        summary.warn("structural framing skipped: no structural-framing family loaded")
    for f in framing:
        if beam_sym is None:
            break
        level = levels.get(f["level"])
        z = level.Elevation if level else 0.0
        try:
            curve = DB.Line.CreateBound(_xyz(f["start"], z), _xyz(f["end"], z))
            doc.Create.NewFamilyInstance(
                curve, beam_sym, level, DB.Structure.StructuralType.Beam
            )
            summary.framing += 1
        except Exception as exc:
            summary.warn("framing %s->%s: %s" % (f["start"], f["end"], exc))


# --- entry point -------------------------------------------------------------


def build(doc, data, structure=True):
    """Build a Revit model from an exchange ``data`` dict in the active ``doc``.

    ``data`` may be a raw dict (it is run through :func:`barndsl_revit.exchange.load`
    here) or one already loaded. Returns a :class:`BuildSummary`. Everything
    happens inside a single transaction so the build is atomic — roll back in
    Revit's undo with one step.
    """
    data = _exchange.load(data)
    summary = BuildSummary()
    for problem in _exchange.validate(data):
        summary.warn(problem)

    ext_type, int_type = _pick_wall_types(doc)
    if ext_type is None or int_type is None:
        summary.warn("no basic wall type found in this project; nothing built")
        return summary

    t = DB.Transaction(doc, "Build barndsl plan")
    t.Start()
    try:
        levels = _ensure_levels(doc, data, summary)
        walls = _build_walls(doc, data, levels, ext_type, int_type, summary)
        _build_openings(doc, data, levels, walls, summary)
        _build_rooms(doc, data, levels, summary)
        if structure:
            _build_structure(doc, data, levels, summary)
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return summary
