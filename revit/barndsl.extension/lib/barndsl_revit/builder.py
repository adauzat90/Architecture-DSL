# -*- coding: utf-8 -*-
"""Create Revit elements from a ``barndsl.revit/1`` exchange.

Talks to the **Revit API**, so it only imports cleanly *inside* Revit (under
pyRevit). Import it lazily from a pushbutton script — never from the Revit-free
:mod:`barndsl_revit.exchange`.

**Primary target: Revit 2025** (.NET 8 / pyRevit 5, CPython 3.12 engine). The
code sticks to APIs current in 2025: ``Floor.Create`` (the old ``NewFloor`` is
gone), ``ElementId.Value`` in favour of the deprecated ``IntegerValue``, and the
Stairs-by-component API. It avoids removed members, so it should also run on
recent prior versions, but 2025 is what it's written against.

What it builds, in one transaction (plus stairs in their own edit scope after):

* **Levels** — reuse an existing level at the same elevation, else create one.
* **Walls** — a wall per exchange segment, on its level, at its height, using an
  *exterior* or *interior* wall type chosen from the project.
* **Doors / windows** — a hosted family instance on the matched wall. When
  ``size_families`` is on, the base family symbol is duplicated and its
  Width/Height type parameters are set to the exchange's values (cached and
  reused), so openings come out the right size instead of the family default.
* **Rooms** — placed at each seed point once the walls enclose it, then named.
* **Structure** — structural columns at posts and framing along beams, when those
  families are loaded.
* **Porches** — a floor slab from each porch outline.
* **Stairs** — a best-effort straight run between the two levels (experimental;
  falls back to a note if the Stairs API rejects the geometry).

Everything is defensive: a single element that fails is recorded as a warning and
the build continues. Coordinates and units pass straight through — the exchange
is in feet (Revit's internal unit), ``x`` east / ``y`` north matching world XY.
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
        self.porches = 0
        self.stairs = 0
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)

    def as_line(self):
        return (
            "%d level(s), %d wall(s), %d door(s), %d window(s), %d room(s), "
            "%d column(s), %d framing, %d porch(es), %d stair(s)"
            % (
                self.levels,
                self.walls,
                self.doors,
                self.windows,
                self.rooms,
                self.columns,
                self.framing,
                self.porches,
                self.stairs,
            )
        )


# --- helpers -----------------------------------------------------------------


def _xyz(point, z):
    return DB.XYZ(float(point[0]), float(point[1]), float(z))


def _collect(doc, of_class):
    return DB.FilteredElementCollector(doc).OfClass(of_class).ToElements()


def _id_val(element_id):
    """Stable integer for an ElementId across Revit versions.

    Revit 2024+/2025 expose 64-bit ``ElementId.Value``; ``IntegerValue`` is
    deprecated. Prefer ``Value`` and fall back for older builds.
    """
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def _pick_wall_types(doc):
    """Return ``(exterior_type, interior_type)`` — the best wall types available."""
    exterior = interior = fallback = None
    for wt in _collect(doc, DB.WallType):
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


def _first_floor_type(doc):
    fallback = None
    for ft in _collect(doc, DB.FloorType):
        try:
            if ft.IsFoundationSlab:
                continue
        except Exception:
            pass
        return ft
    return fallback


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


def _set_double_param(elem, bips, names, value):
    """Set the first writable double parameter found by built-in id or name."""
    for bip in bips:
        try:
            p = elem.get_Parameter(bip)
        except Exception:
            p = None
        if p is not None and not p.IsReadOnly and p.StorageType == DB.StorageType.Double:
            p.Set(float(value))
            return True
    for nm in names:
        p = elem.LookupParameter(nm)
        if p is not None and not p.IsReadOnly and p.StorageType == DB.StorageType.Double:
            p.Set(float(value))
            return True
    return False


def _sized_symbol(doc, base, width, height, cache, summary):
    """Return a family symbol of ``base``'s family sized ``width`` x ``height``.

    Duplicates the base type once per distinct size (named ``barndsl WxH``) and
    sets its Width/Height type parameters, caching the result. Reuses an existing
    same-named type on re-runs. Falls back to the base symbol if the family has
    no settable Width/Height (e.g. a fixed-size family).
    """
    key = (_id_val(base.Id), round(float(width), 4), round(float(height), 4))
    if key in cache:
        return cache[key]

    target_name = "barndsl %.2fx%.2f" % (float(width), float(height))
    fam = base.Family
    sym = None
    try:
        for sid in fam.GetFamilySymbolIds():
            s = doc.GetElement(sid)
            if s is not None and s.Name == target_name:
                sym = s
                break
    except Exception:
        sym = None

    if sym is None:
        try:
            sym = base.Duplicate(target_name)
        except Exception as exc:
            summary.warn("could not size family '%s': %s" % (target_name, exc))
            cache[key] = _activate(base, doc)
            return cache[key]
        set_w = _set_double_param(
            sym, [DB.BuiltInParameter.FAMILY_WIDTH_PARAM], ["Width"], width
        )
        set_h = _set_double_param(
            sym, [DB.BuiltInParameter.FAMILY_HEIGHT_PARAM], ["Height"], height
        )
        doc.Regenerate()
        if not (set_w or set_h):
            summary.warn(
                "family '%s' has no settable Width/Height; using its default size"
                % base.Family.Name
            )

    cache[key] = _activate(sym, doc)
    return cache[key]


def _rect_loop(x, y, w, l, z):
    pts = [
        DB.XYZ(x, y, z),
        DB.XYZ(x + w, y, z),
        DB.XYZ(x + w, y + l, z),
        DB.XYZ(x, y + l, z),
    ]
    loop = DB.CurveLoop()
    for i in range(4):
        loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % 4]))
    return loop


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
                doc, curve, wtype.Id, level.Id, float(w["height"]), 0.0, False, False
            )
            made[w["id"]] = wall
            summary.walls += 1
        except Exception as exc:
            summary.warn("wall %s: %s" % (w["id"], exc))
    return made


def _build_openings(doc, data, levels, walls, summary, size_families):
    door_base = _activate(_first_symbol(doc, DB.BuiltInCategory.OST_Doors), doc)
    win_base = _activate(_first_symbol(doc, DB.BuiltInCategory.OST_Windows), doc)
    st = DB.Structure.StructuralType.NonStructural
    size_cache = {}

    for o in data["openings"]:
        host = walls.get(o.get("host_wall"))
        if host is None:
            summary.warn("opening %s: no host wall, skipped" % o["id"])
            continue
        level = levels.get(o["level"])
        is_window = o["category"] == "window"
        base = win_base if is_window else door_base
        if base is None:
            summary.warn(
                "opening %s: no %s family loaded, skipped"
                % (o["id"], "window" if is_window else "door")
            )
            continue

        if size_families:
            sym = _sized_symbol(
                doc, base, o.get("width", 0.0), o.get("height", 0.0), size_cache, summary
            )
        else:
            sym = base

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

    if data["openings"] and not size_families:
        summary.warn(
            "openings used their family's default size (sizing was disabled)"
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


def _build_porches(doc, data, levels, summary):
    porches = [a for a in data.get("areas", []) if a.get("kind") == "porch"]
    if not porches:
        return
    ftype = _first_floor_type(doc)
    if ftype is None:
        summary.warn("porches skipped: no floor type in project")
        return
    level0 = levels.get(0)
    if level0 is None:
        summary.warn("porches skipped: no ground level")
        return
    from System.Collections.Generic import List

    for p in porches:
        try:
            loop = _rect_loop(
                float(p["x"]), float(p["y"]), float(p["width"]), float(p["length"]),
                level0.Elevation,
            )
            loops = List[DB.CurveLoop]()
            loops.Add(loop)
            DB.Floor.Create(doc, loops, ftype.Id, level0.Id)
            summary.porches += 1
        except Exception as exc:
            summary.warn("porch %s: %s" % (p.get("id"), exc))


def _build_stairs(doc, data, levels, summary):
    """Best-effort straight-run stairs, each in its own edit scope.

    Run *after* the main transaction has committed (the levels must exist and
    StairsEditScope manages its own transactions). Experimental: if the Stairs
    API rejects the derived geometry, it's logged and the stair is left for the
    user to model — the rest of the build is unaffected.
    """
    stairs = [a for a in data.get("areas", []) if a.get("kind") == "stair"]
    if not stairs:
        return

    from Autodesk.Revit.DB.Architecture import (
        StairsEditScope,
        StairsRun,
        StairsRunJustification,
    )

    class _SwallowFailures(DB.IFailuresPreprocessor):
        def PreprocessFailures(self, accessor):
            return DB.FailureProcessingResult.Continue

    for s in stairs:
        meta = s.get("meta", {})
        base = levels.get(meta.get("from_level", s.get("level", 0)))
        top = levels.get(meta.get("to_level"))
        if base is None or top is None:
            summary.warn("stair %s: missing base/top level" % s.get("id"))
            continue

        x, y, w, l = float(s["x"]), float(s["y"]), float(s["width"]), float(s["length"])
        if l >= w:  # run north-south, full length, centred east-west
            cx = x + w / 2.0
            p1, p2, run_w = DB.XYZ(cx, y, base.Elevation), DB.XYZ(cx, y + l, base.Elevation), w
        else:  # run east-west
            cy = y + l / 2.0
            p1, p2, run_w = DB.XYZ(x, cy, base.Elevation), DB.XYZ(x + w, cy, base.Elevation), l

        scope = StairsEditScope(doc, "barndsl stair")
        try:
            stairs_id = scope.Start(base.Id, top.Id)
            t = DB.Transaction(doc, "barndsl stair run")
            t.Start()
            try:
                run = StairsRun.CreateStraightRun(
                    doc, stairs_id, DB.Line.CreateBound(p1, p2), StairsRunJustification.Center
                )
                try:
                    run.ActualRunWidth = run_w
                except Exception:
                    pass
                t.Commit()
            except Exception:
                t.RollBack()
                raise
            scope.Commit(_SwallowFailures())
            summary.stairs += 1
        except Exception as exc:
            try:
                if scope.IsActive:
                    scope.Cancel()
            except Exception:
                pass
            summary.warn(
                "stair %s: could not build (left as a reference, model manually): %s"
                % (s.get("id"), exc)
            )


# --- entry point -------------------------------------------------------------


def build(doc, data, structure=True, size_families=True, porches=True, stairs=True):
    """Build a Revit model from an exchange ``data`` dict in the active ``doc``.

    ``data`` may be a raw dict (run through :func:`barndsl_revit.exchange.load`
    here) or one already loaded. Returns a :class:`BuildSummary`. Walls,
    openings, rooms, structure and porches go in one transaction (a single undo
    step); stairs are built afterward in their own edit scopes.
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
        _build_openings(doc, data, levels, walls, summary, size_families)
        _build_rooms(doc, data, levels, summary)
        if structure:
            _build_structure(doc, data, levels, summary)
        if porches:
            _build_porches(doc, data, levels, summary)
        t.Commit()
    except Exception:
        t.RollBack()
        raise

    # Stairs manage their own transactions, so they run after the main commit
    # (`levels` stays bound — function scope, and the commit above succeeded).
    if stairs:
        try:
            _build_stairs(doc, data, levels, summary)
        except Exception as exc:
            summary.warn("stairs pass failed: %s" % exc)

    return summary
