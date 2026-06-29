# -*- coding: utf-8 -*-
"""Create Revit elements from a ``barndsl.revit/1`` exchange.

Talks to the **Revit API**, so it only imports cleanly *inside* Revit (under
pyRevit). Import it lazily from a pushbutton script — never from the Revit-free
:mod:`barndsl_revit.exchange` / :mod:`barndsl_revit.report`.

**Primary target: Revit 2025** (.NET 8 / pyRevit 5, CPython 3.12 engine). Uses
APIs current in 2025 (``Floor.Create``; ``ElementId.Value`` over the deprecated
``IntegerValue``; the component Stairs API) and avoids removed members.

Built for debugging a real run:

* Every element's outcome (created / skipped / failed, with the Revit id and a
  reason) is recorded in a :class:`barndsl_revit.report.BuildReport`, which
  renders to markdown for the pyRevit panel and JSON for a build-log file.
* :func:`build` honours a :class:`~barndsl_revit.report.BuildOptions` — toggle
  passes, sizing, and map any pass to a **named** wall/floor/family type from the
  project template instead of auto-picking.
* **Dry run** does a real build and then rolls it back, so a preview surfaces the
  exact per-element API errors without committing anything.
* :func:`diagnose` reports the environment and the types/families available in
  the active document — what the *Diagnostics* button shows.

Coordinates and units pass straight through — feet (Revit's internal unit), ``x``
east / ``y`` north matching world XY.
"""

import sys

from pyrevit import DB, revit

from . import exchange as _exchange
from . import naming as _naming
from . import report as _report

try:
    from pyrevit import script as _script

    _logger = _script.get_logger()
except Exception:  # pragma: no cover - only hit outside a pyRevit command

    class _NullLogger(object):
        def _noop(self, *a, **k):
            pass

        debug = info = warning = error = _noop

    _logger = _NullLogger()


# --- low-level helpers -------------------------------------------------------


def _xyz(point, z):
    return DB.XYZ(float(point[0]), float(point[1]), float(z))


def _collect(doc, of_class):
    return DB.FilteredElementCollector(doc).OfClass(of_class).ToElements()


def _symbols(doc, bic):
    return (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.FamilySymbol)
        .OfCategory(bic)
        .ToElements()
    )


def _id_val(element_id):
    """Stable integer for an ElementId across Revit versions.

    Revit 2024+/2025 expose 64-bit ``ElementId.Value``; ``IntegerValue`` is
    deprecated. Prefer ``Value`` and fall back for older builds.
    """
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def _rid(elem):
    try:
        return _id_val(elem.Id)
    except Exception:
        return None


def _name(elem):
    try:
        return elem.Name
    except Exception:
        try:
            return DB.Element.Name.GetValue(elem)
        except Exception:
            return ""


def _wall_function(wt):
    try:
        return str(wt.Function)
    except Exception:
        return "?"


def _basic_wall_types(doc):
    return [wt for wt in _collect(doc, DB.WallType) if wt.Kind == DB.WallKind.Basic]


def _pick_wall_types(doc):
    """Auto-pick ``(exterior, interior)`` basic wall types by Function."""
    exterior = interior = fallback = None
    for wt in _basic_wall_types(doc):
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
    return (exterior or fallback), (interior or fallback)


def _floor_types(doc):
    out = []
    for ft in _collect(doc, DB.FloorType):
        try:
            if ft.IsFoundationSlab:
                continue
        except Exception:
            pass
        out.append(ft)
    return out


def _wall_type_named(doc, name):
    if not name:
        return None
    for wt in _basic_wall_types(doc):
        if _name(wt) == name:
            return wt
    return None


def _floor_type_named(doc, name):
    if not name:
        return None
    for ft in _collect(doc, DB.FloorType):
        if _name(ft) == name:
            return ft
    return None


def _symbol_named(doc, bic, family_name):
    if not family_name:
        return None
    for s in _symbols(doc, bic):
        try:
            if s.Family.Name == family_name:
                return s
        except Exception:
            pass
    return None


def _activate(symbol, doc):
    if symbol is not None and not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()
    return symbol


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


# --- resource resolution (named override → auto-pick) ------------------------


class _Resources(object):
    def __init__(self):
        self.ext_wall = None
        self.int_wall = None
        self.door = None
        self.window = None
        self.floor = None
        self.column = None
        self.beam = None


def _resolve_resources(doc, options, report):
    """Pick the wall/floor/family types, honouring named overrides.

    A missing named override falls back to an auto-pick and adds a note. The
    chosen names are recorded in ``report.resources`` for the report header.
    """
    res = _Resources()
    auto_ext, auto_int = _pick_wall_types(doc)

    def named_or(name, finder, fallback, label):
        if name:
            found = finder(name)
            if found is not None:
                return found
            report.note("%s '%s' not found in project; using an auto-pick" % (label, name))
        return fallback

    res.ext_wall = named_or(
        options.exterior_wall_type, lambda n: _wall_type_named(doc, n), auto_ext, "exterior wall type"
    )
    res.int_wall = named_or(
        options.interior_wall_type, lambda n: _wall_type_named(doc, n), auto_int, "interior wall type"
    )
    res.door = named_or(
        options.door_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_Doors, n),
        (_symbols(doc, DB.BuiltInCategory.OST_Doors) or [None])[0],
        "door family",
    )
    res.window = named_or(
        options.window_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_Windows, n),
        (_symbols(doc, DB.BuiltInCategory.OST_Windows) or [None])[0],
        "window family",
    )
    res.floor = named_or(
        options.floor_type,
        lambda n: _floor_type_named(doc, n),
        (_floor_types(doc) or [None])[0],
        "floor type",
    )
    res.column = named_or(
        options.column_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_StructuralColumns, n),
        (_symbols(doc, DB.BuiltInCategory.OST_StructuralColumns) or [None])[0],
        "structural-column family",
    )
    res.beam = named_or(
        options.beam_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_StructuralFraming, n),
        (_symbols(doc, DB.BuiltInCategory.OST_StructuralFraming) or [None])[0],
        "structural-framing family",
    )

    report.resources["exterior_wall"] = _name(res.ext_wall) if res.ext_wall else "(none)"
    report.resources["interior_wall"] = _name(res.int_wall) if res.int_wall else "(none)"
    report.resources["door_family"] = res.door.Family.Name if res.door else "(none loaded)"
    report.resources["window_family"] = res.window.Family.Name if res.window else "(none loaded)"
    report.resources["floor_type"] = _name(res.floor) if res.floor else "(none)"
    return res


# --- element passes ----------------------------------------------------------


def _ensure_levels(doc, data, report):
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
            try:
                match.Name = lvl["name"]
            except Exception:
                pass
            report.created("level", lvl["name"], revit_id=_rid(match))
        out[lvl["index"]] = match
    return out


def _build_walls(doc, data, levels, res, report):
    made = {}
    for w in data["walls"]:
        level = levels.get(w["level"])
        if level is None:
            report.skipped("wall", w["id"], "no level %r" % w["level"])
            continue
        z = level.Elevation
        try:
            curve = DB.Line.CreateBound(_xyz(w["start"], z), _xyz(w["end"], z))
        except Exception:
            report.skipped("wall", w["id"], "degenerate segment")
            continue
        wtype = res.ext_wall if w.get("exterior") else res.int_wall
        try:
            wall = DB.Wall.Create(
                doc, curve, wtype.Id, level.Id, float(w["height"]), 0.0, False, False
            )
            made[w["id"]] = wall
            report.created("wall", w["id"], revit_id=_rid(wall))
        except Exception as exc:
            _logger.warning("wall %s: %s", w["id"], exc)
            report.failed("wall", w["id"], str(exc))
    return made


def _sized_symbol(doc, base, width, height, cache, report):
    key = (_id_val(base.Id), round(float(width), 4), round(float(height), 4))
    if key in cache:
        return cache[key]
    target_name = "barndsl %.2fx%.2f" % (float(width), float(height))
    fam = base.Family
    sym = None
    try:
        for sid in fam.GetFamilySymbolIds():
            s = doc.GetElement(sid)
            if s is not None and _name(s) == target_name:
                sym = s
                break
    except Exception:
        sym = None
    if sym is None:
        try:
            sym = base.Duplicate(target_name)
        except Exception as exc:
            report.note("could not size family '%s': %s" % (target_name, exc))
            cache[key] = _activate(base, doc)
            return cache[key]
        set_w = _set_double_param(sym, [DB.BuiltInParameter.FAMILY_WIDTH_PARAM], ["Width"], width)
        set_h = _set_double_param(sym, [DB.BuiltInParameter.FAMILY_HEIGHT_PARAM], ["Height"], height)
        doc.Regenerate()
        if not (set_w or set_h):
            report.note(
                "family '%s' has no settable Width/Height; using its default size" % fam.Name
            )
    cache[key] = _activate(sym, doc)
    return cache[key]


def _build_openings(doc, data, levels, walls, res, options, report):
    door_base = _activate(res.door, doc)
    win_base = _activate(res.window, doc)
    st = DB.Structure.StructuralType.NonStructural
    cache = {}

    for o in data["openings"]:
        kind = "window" if o["category"] == "window" else "door"
        host = walls.get(o.get("host_wall"))
        if host is None:
            report.skipped(kind, o["id"], "no host wall")
            continue
        level = levels.get(o["level"])
        base = win_base if kind == "window" else door_base
        if base is None:
            report.skipped(kind, o["id"], "no %s family loaded" % kind)
            continue
        if options.size_families:
            sym = _sized_symbol(doc, base, o.get("width", 0.0), o.get("height", 0.0), cache, report)
        else:
            sym = base
        point = _xyz(o["location"], level.Elevation if level else 0.0)
        try:
            inst = doc.Create.NewFamilyInstance(point, sym, host, level, st)
        except Exception as exc:
            _logger.warning("opening %s: %s", o["id"], exc)
            report.failed(kind, o["id"], str(exc))
            continue
        if kind == "window":
            try:
                p = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
                if p is not None and not p.IsReadOnly:
                    p.Set(float(o.get("sill", 0.0)))
            except Exception:
                pass
        report.created(kind, o["id"], revit_id=_rid(inst))


def _build_rooms(doc, data, levels, report):
    for r in data["rooms"]:
        level = levels.get(r["level"])
        if level is None:
            report.skipped("room", r["id"], "no level %r" % r["level"])
            continue
        uv = DB.UV(float(r["point"][0]), float(r["point"][1]))
        try:
            room = doc.Create.NewRoom(level, uv)
        except Exception as exc:
            report.failed("room", r["id"], "could not place (walls may not enclose it): %s" % exc)
            continue
        if room is None:
            report.skipped("room", r["id"], "point not in an enclosed region")
            continue
        try:
            p = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
            if p is not None and not p.IsReadOnly:
                p.Set(r.get("name", r["id"]))
        except Exception:
            pass
        report.created("room", r["id"], revit_id=_rid(room))


def _build_structure(doc, data, levels, res, report):
    structure = data.get("structure", {})
    columns = structure.get("columns", [])
    framing = structure.get("framing", [])
    if not columns and not framing:
        return
    col_sym = _activate(res.column, doc)
    beam_sym = _activate(res.beam, doc)

    if columns and col_sym is None:
        report.note("structural columns skipped: no structural-column family loaded")
    for i, c in enumerate(columns):
        if col_sym is None:
            break
        level = levels.get(c["level"])
        if level is None:
            continue
        src = "post %d" % i
        try:
            inst = doc.Create.NewFamilyInstance(
                _xyz(c["point"], level.Elevation), col_sym, level, DB.Structure.StructuralType.Column
            )
            report.created("column", src, revit_id=_rid(inst))
        except Exception as exc:
            report.failed("column", src, str(exc))

    if framing and beam_sym is None:
        report.note("structural framing skipped: no structural-framing family loaded")
    for i, f in enumerate(framing):
        if beam_sym is None:
            break
        level = levels.get(f["level"])
        z = level.Elevation if level else 0.0
        src = "%s %d" % (f.get("role", "beam"), i)
        try:
            curve = DB.Line.CreateBound(_xyz(f["start"], z), _xyz(f["end"], z))
            inst = doc.Create.NewFamilyInstance(
                curve, beam_sym, level, DB.Structure.StructuralType.Beam
            )
            report.created("framing", src, revit_id=_rid(inst))
        except Exception as exc:
            report.failed("framing", src, str(exc))


def _build_porches(doc, data, levels, res, report):
    porches = [a for a in data.get("areas", []) if a.get("kind") == "porch"]
    if not porches:
        return
    if res.floor is None:
        report.note("porches skipped: no floor type in project")
        for p in porches:
            report.skipped("porch", p.get("id"), "no floor type")
        return
    level0 = levels.get(0)
    if level0 is None:
        report.note("porches skipped: no ground level")
        return
    from System.Collections.Generic import List

    for p in porches:
        try:
            loop = _rect_loop(
                float(p["x"]), float(p["y"]), float(p["width"]), float(p["length"]), level0.Elevation
            )
            loops = List[DB.CurveLoop]()
            loops.Add(loop)
            floor = DB.Floor.Create(doc, loops, res.floor.Id, level0.Id)
            report.created("porch", p.get("id"), revit_id=_rid(floor))
        except Exception as exc:
            report.failed("porch", p.get("id"), str(exc))


def _build_stairs(doc, data, levels, report, dry_run):
    """Best-effort straight-run stairs, each in its own edit scope.

    Runs after the main transaction (StairsEditScope manages its own
    transactions). For a dry run the scope is cancelled instead of committed, so
    the preview still exercises the API without persisting anything.
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
            report.skipped("stair", s.get("id"), "missing base/top level")
            continue

        x, y, w, l = float(s["x"]), float(s["y"]), float(s["width"]), float(s["length"])
        if l >= w:
            cx = x + w / 2.0
            p1, p2, run_w = DB.XYZ(cx, y, base.Elevation), DB.XYZ(cx, y + l, base.Elevation), w
        else:
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
            if dry_run:
                scope.Cancel()
            else:
                scope.Commit(_SwallowFailures())
            report.created("stair", s.get("id"), revit_id=_id_val(stairs_id))
        except Exception as exc:
            try:
                if scope.IsActive:
                    scope.Cancel()
            except Exception:
                pass
            _logger.warning("stair %s: %s", s.get("id"), exc)
            report.failed("stair", s.get("id"), "left as a reference, model manually: %s" % exc)


# --- diagnostics -------------------------------------------------------------


def diagnose(doc):
    """Return a dict describing the environment and the active document's
    resources — what the *Diagnostics* button surfaces, and the first thing to
    check when a build doesn't produce what you expect."""
    info = {}
    try:
        app = doc.Application
        info["revit"] = "%s (%s) build %s" % (
            app.VersionNumber, app.VersionName, app.VersionBuild
        )
    except Exception as exc:
        info["revit"] = "? (%s)" % exc
    info["document"] = getattr(doc, "Title", "?")
    info["python"] = sys.version.split()[0]
    try:
        import pyrevit

        info["pyrevit"] = getattr(pyrevit, "__version__", "?")
    except Exception:
        info["pyrevit"] = "?"
    try:
        import barndsl

        info["barndsl"] = "importable (v%s)" % getattr(barndsl, "__version__", "?")
    except Exception as exc:
        info["barndsl"] = "NOT importable — use the JSON workflow (%s)" % exc

    info["wall_types"] = [
        "%s [%s]" % (_name(wt), _wall_function(wt)) for wt in _basic_wall_types(doc)
    ]
    info["floor_types"] = [_name(ft) for ft in _floor_types(doc)]
    info["door_families"] = sorted(
        set(s.Family.Name for s in _symbols(doc, DB.BuiltInCategory.OST_Doors))
    )
    info["window_families"] = sorted(
        set(s.Family.Name for s in _symbols(doc, DB.BuiltInCategory.OST_Windows))
    )
    info["structural_column_families"] = sorted(
        set(s.Family.Name for s in _symbols(doc, DB.BuiltInCategory.OST_StructuralColumns))
    )
    info["structural_framing_families"] = sorted(
        set(s.Family.Name for s in _symbols(doc, DB.BuiltInCategory.OST_StructuralFraming))
    )
    levels = sorted(_collect(doc, DB.Level), key=lambda e: e.Elevation)
    info["levels"] = ["%s @ %.2f ft" % (_name(e), e.Elevation) for e in levels]

    # Readiness flags a tester can act on at a glance.
    info["ready"] = {
        "walls": bool(_basic_wall_types(doc)),
        "doors": bool(info["door_families"]),
        "windows": bool(info["window_families"]),
        "floors": bool(info["floor_types"]),
        "structural_columns": bool(info["structural_column_families"]),
        "structural_framing": bool(info["structural_framing_families"]),
    }
    return info


# --- reading a Revit model back into an exchange (experimental) --------------


def _double(elem, bip):
    try:
        p = elem.get_Parameter(bip)
        if p is not None and p.StorageType == DB.StorageType.Double:
            return p.AsDouble()
    except Exception:
        pass
    return None


def read_model(doc, report=None):
    """Read the active model's rooms and door/window instances into a
    ``barndsl.revit/1`` exchange dict — the input to ``exchange_to_plan``.

    **Experimental.** Rooms come from placed Revit Rooms (bounding box → the
    rectangle the DSL needs; name → a best-guess room type); openings from door/
    window instances, connected to rooms via their FromRoom/ToRoom. Coordinates
    are normalised so the south-west corner sits at the origin (the DSL
    convention). Walls and structure aren't read back — the reconstruction is
    room- and opening-driven. Records what it read into ``report``.
    """
    if report is None:
        report = _report.BuildReport()

    levels = sorted(_collect(doc, DB.Level), key=lambda e: e.Elevation)
    level_index = {}
    for i, lv in enumerate(levels):
        level_index[_id_val(lv.Id)] = i
    ceiling = 9.0
    if len(levels) >= 2:
        gap = levels[1].Elevation - levels[0].Elevation
        if gap > 0:
            ceiling = gap

    used_ids = set()
    rooms = []
    room_id_by_eid = {}
    room_level_by_id = {}
    minx = miny = None

    rm_collector = (
        DB.FilteredElementCollector(doc)
        .OfCategory(DB.BuiltInCategory.OST_Rooms)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    for rm in rm_collector:
        try:
            if rm.Area <= 0 or rm.Location is None:
                report.skipped("room", _name(rm) or "?", "unplaced")
                continue
            bb = rm.get_BoundingBox(None)
            if bb is None:
                continue
            x, y = bb.Min.X, bb.Min.Y
            w, l = bb.Max.X - x, bb.Max.Y - y
        except Exception as exc:
            report.failed("room", "?", str(exc))
            continue
        nm = None
        try:
            p = rm.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
            nm = p.AsString() if p is not None else None
        except Exception:
            nm = None
        nm = nm or "Room"
        rid = _naming.slug_id(nm, used_ids)
        lvl = level_index.get(_id_val(rm.LevelId), 0)
        room_id_by_eid[_id_val(rm.Id)] = rid
        room_level_by_id[rid] = lvl
        minx = x if minx is None else min(minx, x)
        miny = y if miny is None else min(miny, y)
        rooms.append(
            {"_eid": _id_val(rm.Id), "id": rid, "name": nm, "type": _naming.guess_room_type(nm),
             "level": lvl, "x": x, "y": y, "width": w, "length": l, "area": rm.Area}
        )
        report.created("room", rid)

    if not rooms:
        report.note("no placed rooms found to read")
    ox = minx or 0.0
    oy = miny or 0.0
    for r in rooms:
        r["x"] -= ox
        r["y"] -= oy
        r.pop("_eid", None)
        r["point"] = [r["x"] + r["width"] / 2.0, r["y"] + r["length"] / 2.0]

    openings = []

    def _read_openings(bic, is_window):
        insts = (
            DB.FilteredElementCollector(doc)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
            .ToElements()
        )
        for inst in insts:
            kindlabel = "window" if is_window else "door"
            try:
                loc = inst.Location.Point
            except Exception:
                report.skipped(kindlabel, "?", "no location point")
                continue
            sym = getattr(inst, "Symbol", None)
            width = _double(sym, DB.BuiltInParameter.FAMILY_WIDTH_PARAM) if sym else None
            height = _double(sym, DB.BuiltInParameter.FAMILY_HEIGHT_PARAM) if sym else None
            width = width or 3.0
            height = height or 6.667
            try:
                from_room = inst.FromRoom
                to_room = inst.ToRoom
            except Exception:
                from_room = to_room = None
            connected = []
            for rm in (from_room, to_room):
                if rm is not None:
                    rid = room_id_by_eid.get(_id_val(rm.Id))
                    if rid:
                        connected.append(rid)
            label = _name(inst) or kindlabel
            point = [loc.X - ox, loc.Y - oy]
            if is_window:
                if not connected:
                    report.skipped("window", label, "not bounded by a known room")
                    continue
                sill = _double(inst, DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM) or 0.0
                openings.append(
                    {"id": "win%d" % len(openings), "category": "window", "kind": "window",
                     "level": room_level_by_id.get(connected[0], 0), "location": point,
                     "width": width, "height": height, "sill": sill, "exterior": True,
                     "egress": False, "rooms": connected[:1], "host_wall": None}
                )
                report.created("window", label)
            else:
                interior = len(connected) >= 2
                openings.append(
                    {"id": "door%d" % len(openings),
                     "category": "door", "kind": "swing" if interior else "exterior",
                     "level": room_level_by_id.get(connected[0], 0) if connected else 0,
                     "location": point, "width": width, "height": height, "sill": 0.0,
                     "exterior": not interior, "egress": not interior,
                     "rooms": connected[:2] if interior else connected[:1], "host_wall": None}
                )
                if not connected:
                    report.skipped("door", label, "not bounded by a known room")
                    openings.pop()
                else:
                    report.created("door", label)

    _read_openings(DB.BuiltInCategory.OST_Doors, False)
    _read_openings(DB.BuiltInCategory.OST_Windows, True)

    xs = [r["x"] + r["width"] for r in rooms] or [0.0]
    ys = [r["y"] + r["length"] for r in rooms] or [0.0]
    exchange = {
        "schema": _exchange.SCHEMA,
        "units": "feet",
        "plan": {
            "name": getattr(doc, "Title", "Revit Model"),
            "ceiling_height": ceiling,
            "envelope_width": max(xs),
            "envelope_length": max(ys),
            "wings": [],
        },
        "levels": [
            {"index": i, "name": _name(lv), "elevation": lv.Elevation, "height": ceiling}
            for i, lv in enumerate(levels)
        ]
        or [{"index": 0, "name": "Level 1", "elevation": 0.0, "height": ceiling}],
        "walls": [],
        "openings": openings,
        "rooms": rooms,
        "structure": {"columns": [], "framing": []},
        "areas": [],
    }
    return exchange, report


# --- entry point -------------------------------------------------------------


def build(doc, data, options=None):
    """Build a Revit model from an exchange ``data`` dict in the active ``doc``.

    ``data`` may be a raw dict (validated here) or one already loaded. ``options``
    is a :class:`~barndsl_revit.report.BuildOptions` (defaults if omitted).
    Returns a :class:`~barndsl_revit.report.BuildReport`.

    Walls, openings, rooms, structure and porches run in one transaction;
    **for a dry run that transaction is rolled back** (so the preview is real but
    nothing persists). Stairs run afterward in their own edit scopes.
    """
    if options is None:
        options = _report.BuildOptions()
    data = _exchange.load(data)
    report = _report.BuildReport(dry_run=options.dry_run)
    report.problems = _exchange.validate(data)

    res = _resolve_resources(doc, options, report)
    if res.ext_wall is None or res.int_wall is None:
        report.note("no basic wall type found in this project; nothing built")
        return report

    label = "Preview barndsl plan" if options.dry_run else "Build barndsl plan"
    t = DB.Transaction(doc, label)
    t.Start()
    try:
        levels = _ensure_levels(doc, data, report)
        walls = _build_walls(doc, data, levels, res, report)
        _build_openings(doc, data, levels, walls, res, options, report)
        _build_rooms(doc, data, levels, report)
        if options.structure:
            _build_structure(doc, data, levels, res, report)
        if options.porches:
            _build_porches(doc, data, levels, res, report)
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise

    if options.dry_run:
        t.RollBack()
    else:
        t.Commit()

    if options.stairs:
        try:
            _build_stairs(doc, data, levels, report, options.dry_run)
        except Exception as exc:
            report.note("stairs pass failed: %s" % exc)

    _logger.info("barndsl %s", report.summary_line())
    return report
