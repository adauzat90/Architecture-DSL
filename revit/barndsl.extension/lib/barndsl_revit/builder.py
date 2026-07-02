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

#: Code-minimum flight width (ft), used only as a fallback run width.
MIN_STAIR_WIDTH = 3.0

#: Marker value stamped on every element a build creates, so a re-build can find
#: and replace exactly what a previous barndsl build made (and nothing the user
#: drew). Stored in **Extensible Storage** (a private schema) rather than the
#: user-facing Comments field, so it doesn't clobber a designer's annotations and
#: a user can't accidentally match it. A legacy Comments marker is still *read*
#: (so an old build is still recognised), but new builds no longer *write* it.
MANAGED_MARK = "barndsl-managed"

#: A fixed GUID for the private Extensible-Storage schema that carries the marker.
_MANAGED_SCHEMA_GUID = "b47d9a1e-6f3c-4c2a-9d18-2a1f0c7e5b64"
_MANAGED_FIELD = "marker"


def _es():
    """The ExtensibleStorage namespace + the System bits, or ``None`` if the
    running engine can't provide them (then the Comments fallback is used)."""
    try:
        from Autodesk.Revit.DB import ExtensibleStorage as ES
        from System import Guid, String

        return ES, Guid, String
    except Exception:
        return None


def _managed_schema(create=False):
    """Look up (or, when ``create``, build) the private managed-element schema."""
    bits = _es()
    if bits is None:
        return None
    ES, Guid, String = bits
    try:
        guid = Guid(_MANAGED_SCHEMA_GUID)
        schema = ES.Schema.Lookup(guid)
        if schema is not None or not create:
            return schema
        b = ES.SchemaBuilder(guid)
        b.SetSchemaName("BarndslManaged")
        b.SetReadAccessLevel(ES.AccessLevel.Public)
        b.SetWriteAccessLevel(ES.AccessLevel.Public)
        b.AddSimpleField(_MANAGED_FIELD, String)
        return b.Finish()
    except Exception:
        return None


def _es_mark(elem):
    """Stamp the managed marker into Extensible Storage. Returns True on success."""
    bits = _es()
    if bits is None:
        return False
    _ES, _Guid, String = bits
    schema = _managed_schema(create=True)
    if schema is None:
        return False
    try:
        from Autodesk.Revit.DB import ExtensibleStorage as ES

        ent = ES.Entity(schema)
        ent.Set[String](_MANAGED_FIELD, MANAGED_MARK)
        elem.SetEntity(ent)
        return True
    except Exception:
        return False


def _es_is_managed(elem):
    bits = _es()
    if bits is None:
        return False
    _ES, _Guid, String = bits
    schema = _managed_schema(create=False)
    if schema is None:
        return False
    try:
        ent = elem.GetEntity(schema)
        if ent is None or not ent.IsValid():
            return False
        return ent.Get[String](_MANAGED_FIELD) == MANAGED_MARK
    except Exception:
        return False

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


def _comments_param(elem):
    try:
        p = elem.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    except Exception:
        p = None
    if p is None:
        try:
            p = elem.LookupParameter("Comments")
        except Exception:
            p = None
    return p


def _is_managed(elem):
    """Is this element one a previous barndsl build created? Reads the Extensible
    Storage marker, then falls back to the legacy Comments marker so an older
    build is still recognised and cleaned."""
    if _es_is_managed(elem):
        return True
    p = _comments_param(elem)
    try:
        return p is not None and p.AsString() == MANAGED_MARK
    except Exception:
        return False


def _mark(elem):
    """Stamp an element as barndsl-managed. Prefers Extensible Storage (private,
    out of the way); only if that engine is unavailable does it fall back to the
    Comments field."""
    if _es_mark(elem):
        return elem
    p = _comments_param(elem)
    if p is not None and not p.IsReadOnly:
        try:
            p.Set(MANAGED_MARK)
        except Exception:
            pass
    return elem


def _made(report, kind, source, elem, message=""):
    """Mark a freshly-created element and record it as created."""
    _mark(elem)
    return report.created(kind, source, revit_id=_rid(elem), message=message)


def _purge_managed(doc, report):
    """Delete elements a previous barndsl build created, so a re-build replaces
    rather than duplicates. Identifies them by the managed mark in Comments;
    never touches anything the user drew. Runs inside the build transaction."""
    try:
        elems = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()
    except Exception:
        return 0
    removed = 0
    for e in list(elems):
        if _is_managed(e):
            try:
                doc.Delete(e.Id)
                removed += 1
            except Exception:
                pass
    if removed:
        report.note("replaced %d element(s) from a previous barndsl build" % removed)
    return removed


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


def _set_id_param(elem, bips, value):
    """Set the first writable ElementId parameter found by built-in id."""
    for bip in bips:
        try:
            p = elem.get_Parameter(bip)
        except Exception:
            p = None
        if p is not None and not p.IsReadOnly:
            try:
                p.Set(value)
                return True
            except Exception:
                pass
    return False


def _level_at(levels, elevation, tol=1e-3):
    """A level element whose elevation matches ``elevation`` (or None)."""
    for lv in levels.values():
        try:
            if abs(lv.Elevation - float(elevation)) <= tol:
                return lv
        except Exception:
            pass
    return None


def _raise_column(doc, inst, base_level, col, levels, report):
    """Give a structural column a top at the plate elevation.

    Prefer an existing level at the plate (``col["top"]``); otherwise pin the top
    to the base level and offset it up by the storey height. Best-effort — a
    family without these parameters keeps its default height (noted once)."""
    top_elev = float(col.get("top", 0.0))
    base_elev = float(col.get("base", base_level.Elevation))
    if top_elev <= base_elev + 1e-6:
        return
    top_level = _level_at(levels, top_elev)
    set_top = False
    if top_level is not None:
        set_top = _set_id_param(
            inst, [DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM], top_level.Id
        )
        if set_top:
            _set_double_param(
                inst, [DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM], [], 0.0
            )
    if not set_top:
        # No level at the plate (a single-storey post rising to the eave): keep the
        # base level as the top level and offset the top up to the plate.
        _set_id_param(inst, [DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM], base_level.Id)
        set_top = _set_double_param(
            inst,
            [DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM],
            ["Top Offset"],
            top_elev - base_elev,
        )
    if not set_top:
        report.note("column height: family has no settable top level/offset; default kept")


def _level_above(levels, base_level, tol=1e-3):
    """The nearest level strictly above ``base_level`` (or None)."""
    best = None
    for lv in levels.values():
        try:
            if lv.Elevation > base_level.Elevation + tol and (
                best is None or lv.Elevation < best.Elevation
            ):
                best = lv
        except Exception:
            pass
    return best


def _constrain_wall_top(wall, base_level, height, levels, report):
    """Constrain a wall's top to the level above so it stays parametric.

    A wall built with an explicit height won't follow a level edit. When a storey
    exists above, pin the wall's Top Constraint to that level and carry the
    difference as a top **offset** — the level above sits a floor-assembly depth
    above the wall's plate, so the offset (usually a small negative) keeps the
    built top exactly at the plate while remaining level-driven. With no level
    above (top storey), the explicit height stands. Best-effort."""
    top_level = _level_above(levels, base_level)
    if top_level is None:
        return False
    if _set_id_param(wall, [DB.BuiltInParameter.WALL_HEIGHT_TYPE], top_level.Id):
        offset = (base_level.Elevation + float(height)) - top_level.Elevation
        _set_double_param(wall, [DB.BuiltInParameter.WALL_TOP_OFFSET], [], offset)
        return True
    return False


#: Location-line options the config may name → the Revit ``WallLocationLine`` value.
#: The default keeps barndsl's centreline placement (rooms tile on centrelines);
#: "finish_face_exterior" lands the outside finish on the footprint line so the
#: building's overall dimension is exact (at the cost of shifting interior faces).
def _wall_location_line_value(name):
    table = {
        "centerline": 0,          # WallCenterline
        "core_centerline": 1,     # CoreCenterline
        "finish_face_exterior": 2,  # FinishFaceExterior
        "finish_face_interior": 3,  # FinishFaceInterior
        "core_exterior": 4,       # CoreExterior
        "core_interior": 5,       # CoreInterior
    }
    return table.get((name or "").strip().lower())


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
        self.roof_type = None
        self.ceiling_type = None
        self.plumbing = None
        self.appliance = None
        self.footing = None


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
    res.plumbing = named_or(
        options.plumbing_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_PlumbingFixtures, n),
        (_symbols(doc, DB.BuiltInCategory.OST_PlumbingFixtures) or [None])[0],
        "plumbing-fixture family",
    )
    res.appliance = named_or(
        options.appliance_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_SpecialityEquipment, n),
        (_symbols(doc, DB.BuiltInCategory.OST_SpecialityEquipment) or [None])[0],
        "appliance family",
    )
    res.footing = named_or(
        options.foundation_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_StructuralFoundation, n),
        (_symbols(doc, DB.BuiltInCategory.OST_StructuralFoundation) or [None])[0],
        "structural-foundation family",
    )

    report.resources["exterior_wall"] = _name(res.ext_wall) if res.ext_wall else "(none)"
    report.resources["interior_wall"] = _name(res.int_wall) if res.int_wall else "(none)"
    report.resources["door_family"] = res.door.Family.Name if res.door else "(none loaded)"
    report.resources["window_family"] = res.window.Family.Name if res.window else "(none loaded)"
    report.resources["floor_type"] = _name(res.floor) if res.floor else "(none)"

    try:
        res.roof_type = (_collect(doc, DB.RoofType) or [None])[0]
    except Exception:
        res.roof_type = None
    report.resources["roof_type"] = _name(res.roof_type) if res.roof_type else "(none)"
    try:
        res.ceiling_type = (_collect(doc, DB.CeilingType) or [None])[0]
    except Exception:
        res.ceiling_type = None
    report.resources["ceiling_type"] = _name(res.ceiling_type) if res.ceiling_type else "(none)"
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


def _gable_wall(doc, w, wtype, level):
    """A gable-end wall built from a vertical pentagon profile: two base corners,
    two plate corners, and the apex at the ridge. Uses the profile overload of
    ``Wall.Create`` (an ``IList<Curve>`` loop) so the wall top follows the roof
    instead of stopping flat at the plate."""
    from System.Collections.Generic import List

    z = level.Elevation
    plate = float(w["height"])
    apex_h = float(w.get("apex_height", plate))
    (sx, sy), (ex, ey) = w["start"], w["end"]
    ax, ay = w["apex"]
    pts = [
        DB.XYZ(float(sx), float(sy), z),
        DB.XYZ(float(ex), float(ey), z),
        DB.XYZ(float(ex), float(ey), z + plate),
        DB.XYZ(float(ax), float(ay), z + apex_h),
        DB.XYZ(float(sx), float(sy), z + plate),
    ]
    profile = List[DB.Curve]()
    n = len(pts)
    for i in range(n):
        profile.Add(DB.Line.CreateBound(pts[i], pts[(i + 1) % n]))
    return DB.Wall.Create(doc, profile, wtype.Id, level.Id, False)


def _is_gable(w):
    return (
        w.get("profile") == "gable"
        and w.get("apex") is not None
        and float(w.get("apex_height", 0.0)) > float(w.get("height", 0.0)) + 1e-6
    )


def _build_walls(doc, data, levels, res, options, report):
    made = {}
    loc_line = _wall_location_line_value(getattr(options, "location_line", None))
    constrained = 0
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
        wall = None
        gable = _is_gable(w)
        if gable:
            try:
                wall = _gable_wall(doc, w, wtype, level)
                _made(report, "wall", w["id"], wall, message="gable-end profile")
            except Exception as exc:
                report.note(
                    "gable wall %s: profile build failed (%s); flat fallback"
                    % (w["id"], exc)
                )
                wall = None
        if wall is None:
            try:
                wall = DB.Wall.Create(
                    doc, curve, wtype.Id, level.Id, float(w["height"]), 0.0, False, False
                )
                _made(report, "wall", w["id"], wall)
            except Exception as exc:
                _logger.warning("wall %s: %s", w["id"], exc)
                report.failed("wall", w["id"], str(exc))
                continue
            # A flat wall built to an explicit height isn't parametric; pin its top
            # to the level above when there is one so it follows level edits. (A
            # gable wall's top is its ridge profile, so leave it be.)
            if not gable and _constrain_wall_top(wall, level, w["height"], levels, report):
                constrained += 1
        # Optional: land the exterior finish face on the footprint line so the
        # building's overall dimension is exact. Off by default (centreline).
        if loc_line is not None and (loc_line == 0 or w.get("exterior")):
            _set_id_param(wall, [DB.BuiltInParameter.WALL_KEY_REF_PARAM], loc_line)
        made[w["id"]] = wall
    if constrained:
        report.note("constrained %d wall top(s) to the level above" % constrained)
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
        _made(report, kind, o["id"], inst)


#: Default finish schedule by room type — a starting point a residential room
#: schedule expects (the designer refines it). Wet rooms get tile, living areas
#: wood, service rooms sealed concrete; everything falls back to the generic set.
_ROOM_FINISHES = {
    "bathroom": ("Tile", "Tile", "Paint - Ceiling", "Tile"),
    "half_bath": ("Tile", "Tile", "Paint - Ceiling", "Paint"),
    "kitchen": ("Tile", "Wood Base", "Paint - Ceiling", "Paint"),
    "laundry": ("Tile", "Tile", "Paint - Ceiling", "Paint"),
    "utility": ("Sealed Concrete", "Rubber Base", "Exposed", "Paint"),
    "mudroom": ("Tile", "Tile", "Paint - Ceiling", "Paint"),
    "garage": ("Sealed Concrete", "None", "Exposed", "None"),
    "shop": ("Sealed Concrete", "None", "Exposed", "None"),
    "living": ("Wood", "Wood Base", "Paint - Ceiling", "Paint"),
    "dining": ("Wood", "Wood Base", "Paint - Ceiling", "Paint"),
    "bedroom": ("Carpet", "Wood Base", "Paint - Ceiling", "Paint"),
    "office": ("Carpet", "Wood Base", "Paint - Ceiling", "Paint"),
    "loft": ("Carpet", "Wood Base", "Paint - Ceiling", "Paint"),
}
_ROOM_FINISH_DEFAULT = ("Finish", "Base", "Paint - Ceiling", "Paint")


def _set_string_param(elem, bip, value):
    try:
        p = elem.get_Parameter(bip)
    except Exception:
        p = None
    if p is not None and not p.IsReadOnly:
        try:
            p.Set(str(value))
            return True
        except Exception:
            pass
    return False


def _build_rooms(doc, data, levels, report):
    # Number rooms sequentially per level: 101.., 201.. — the residential
    # convention (floor number × 100 + running count).
    counters = {}
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
        _set_string_param(room, DB.BuiltInParameter.ROOM_NAME, r.get("name", r["id"]))
        lvl = r.get("level", 0)
        counters[lvl] = counters.get(lvl, 0) + 1
        _set_string_param(
            room, DB.BuiltInParameter.ROOM_NUMBER, "%d%02d" % (lvl + 1, counters[lvl])
        )
        floor, base, ceil, wall = _ROOM_FINISHES.get(r.get("type", ""), _ROOM_FINISH_DEFAULT)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_FLOOR, floor)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_BASE, base)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_CEILING, ceil)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_WALL, wall)
        _made(report, "room", r["id"], room)


def _build_ceilings(doc, data, levels, res, report):
    """A flat ceiling per room, hosted at the room's ceiling height above its
    level — so the model has a reflected-ceiling plane and somewhere to host
    lighting. Uses each room's rectangle; skipped (with a note) when the project
    has no ceiling type. A per-room ceiling height (vaulted rooms) is honoured."""
    rooms = data.get("rooms", [])
    if not rooms:
        return
    if res.ceiling_type is None:
        report.note("ceilings skipped: no ceiling type in project")
        return
    plan_h = float(data.get("plan", {}).get("ceiling_height", 8.0))
    from System.Collections.Generic import List

    for r in rooms:
        level = levels.get(r["level"])
        if level is None:
            continue
        # A vaulted/cathedral room has no flat ceiling plane — skip it.
        if r.get("vaulted"):
            report.skipped("ceiling", r["id"], "vaulted — open to the roof")
            continue
        height = float(r.get("ceiling_height", plan_h))
        try:
            loop = _rect_loop(
                float(r["x"]), float(r["y"]), float(r["width"]), float(r["length"]),
                level.Elevation,
            )
            loops = List[DB.CurveLoop]()
            loops.Add(loop)
            ceil = DB.Ceiling.Create(doc, loops, res.ceiling_type.Id, level.Id)
            _set_double_param(
                ceil, [DB.BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM], [], height
            )
            _made(report, "ceiling", r["id"], ceil)
        except Exception as exc:
            report.failed("ceiling", r["id"], str(exc))


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
            # Give the post a real height: rise from the floor to the plate (the
            # beam it carries) rather than the family's default stub. Prefer an
            # existing level at the plate elevation; otherwise offset the top above
            # this level.
            _raise_column(doc, inst, level, c, levels, report)
            _made(report, "column", src, inst)
        except Exception as exc:
            report.failed("column", src, str(exc))

    if framing and beam_sym is None:
        report.note("structural framing skipped: no structural-framing family loaded")
    for i, f in enumerate(framing):
        if beam_sym is None:
            break
        level = levels.get(f["level"])
        # The bent/ridge sits at the plate (top of the posts), carried on the
        # exchange as ``z`` — not down at the floor level. Fall back to the level
        # elevation only for an old exchange without ``z``.
        z = float(f.get("z", level.Elevation if level else 0.0))
        src = "%s %d" % (f.get("role", "beam"), i)
        try:
            curve = DB.Line.CreateBound(_xyz(f["start"], z), _xyz(f["end"], z))
            inst = doc.Create.NewFamilyInstance(
                curve, beam_sym, level, DB.Structure.StructuralType.Beam
            )
            _made(report, "framing", src, inst)
        except Exception as exc:
            report.failed("framing", src, str(exc))


#: Fixture kinds hosted from a plumbing family vs. an appliance (specialty) family.
_WET_FIXTURES = ("toilet", "lavatory", "tub", "shower", "sink")


def _build_fixtures(doc, data, levels, res, report):
    """Place a family instance at each fixture/appliance seed — a plumbing family
    for wet fixtures, a specialty-equipment family for appliances. These are
    seeds: the family stands in at the right spot for the designer to swap/adjust.
    Skips a fixture (with a note) when its family isn't loaded."""
    fixtures = data.get("fixtures", [])
    if not fixtures:
        return
    plumb = _activate(res.plumbing, doc)
    appl = _activate(res.appliance, doc)
    if plumb is None:
        report.note("plumbing fixtures skipped: no plumbing-fixture family loaded")
    if appl is None:
        report.note("appliances skipped: no specialty-equipment family loaded")
    st = DB.Structure.StructuralType.NonStructural
    for fx in fixtures:
        wet = fx.get("kind") in _WET_FIXTURES
        sym = plumb if wet else appl
        if sym is None:
            report.skipped("fixture", fx.get("id"), "no %s family" % ("plumbing" if wet else "appliance"))
            continue
        level = levels.get(fx.get("level", 0))
        z = level.Elevation if level else 0.0
        try:
            inst = doc.Create.NewFamilyInstance(_xyz(fx["point"], z), sym, level, st)
            _made(report, "fixture", fx.get("id"), inst, message=fx.get("kind", ""))
        except Exception as exc:
            _logger.warning("fixture %s: %s", fx.get("id"), exc)
            report.failed("fixture", fx.get("id"), str(exc))


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
            _made(report, "porch", p.get("id"), floor)
        except Exception as exc:
            report.failed("porch", p.get("id"), str(exc))


def _stair_runs_for(s):
    """The planned flights for a stair area, falling back to a single straight
    run derived from the footprint if the exchange carries no plan."""
    plan = (s.get("meta", {}) or {}).get("plan")
    if plan and plan.get("runs"):
        return plan["runs"], plan.get("layout", "straight"), bool(plan.get("fits", True))
    x, y, w, l = float(s["x"]), float(s["y"]), float(s["width"]), float(s["length"])
    if l >= w:
        cx = x + w / 2.0
        run = {"start": [cx, y], "end": [cx, y + l], "width": w}
    else:
        cy = y + l / 2.0
        run = {"start": [x, cy], "end": [x + w, cy], "width": l}
    return [run], "straight", True


def _build_slabs(doc, data, levels, res, report):
    slabs = data.get("slabs", [])
    if not slabs:
        return
    if res.floor is None:
        report.note("floor slabs skipped: no floor type in project")
        for s in slabs:
            report.skipped("slab", "level %s" % s.get("level"), "no floor type")
        return
    from System.Collections.Generic import List

    for s in slabs:
        src = "level %s" % s.get("level")
        level = levels.get(s["level"])
        if level is None:
            report.skipped("slab", src, "no level")
            continue
        try:
            loop = _rect_loop(
                float(s["x"]), float(s["y"]), float(s["width"]), float(s["length"]), level.Elevation
            )
            loops = List[DB.CurveLoop]()
            loops.Add(loop)
            floor = DB.Floor.Create(doc, loops, res.floor.Id, level.Id)
            _made(report, "slab", src, floor)
        except Exception as exc:
            report.failed("slab", src, str(exc))


def _build_foundation(doc, data, levels, res, report):
    """Build the slab-on-grade foundation: a **pad footing** under each post, plus
    a note carrying the thickened-edge (turndown) run for manual detailing.

    Pad footings place a structural-foundation family at each post point on the
    ground level; skipped with a note if no foundation family is loaded. The
    turndown/grade beam has no one-call API, so its geometry is reported for the
    detailer (like the roof gable). The slab itself is the ground-level floor slab.
    """
    foundation = data.get("foundation")
    if not foundation:
        return
    level0 = levels.get(0)
    if level0 is None:
        report.skipped("foundation", "footings", "no ground level")
        return

    footings = foundation.get("footings", [])
    if footings:
        sym = _activate(res.footing, doc)
        if sym is None:
            report.note("pad footings skipped: no structural-foundation family loaded")
            for i, f in enumerate(footings):
                report.skipped("footing", "post %d" % i, "no foundation family")
        else:
            for i, f in enumerate(footings):
                try:
                    inst = doc.Create.NewFamilyInstance(
                        _xyz(f["point"], level0.Elevation), sym, level0,
                        DB.Structure.StructuralType.Footing,
                    )
                    _made(report, "footing", "post %d" % i, inst)
                except Exception as exc:
                    report.failed("footing", "post %d" % i, str(exc))

    edge = foundation.get("edge") or {}
    segs = edge.get("segments") or []
    if segs:
        report.note(
            "thickened slab edge (turndown): %d perimeter run(s), %.0f in wide x "
            "%.0f in deep — detail as a grade beam"
            % (len(segs), edge.get("width", 0) * 12, edge.get("depth", 0) * 12)
        )
    yd3 = foundation.get("concrete_yd3")
    if yd3:
        report.note("foundation concrete (rough): %.1f cu yd" % yd3)


def _build_grids(doc, data, report):
    grids = data.get("grids", [])
    if not grids:
        return
    for g in grids:
        label = str(g.get("label", "?"))
        try:
            line = DB.Line.CreateBound(
                DB.XYZ(float(g["start"][0]), float(g["start"][1]), 0.0),
                DB.XYZ(float(g["end"][0]), float(g["end"][1]), 0.0),
            )
            grid = DB.Grid.Create(doc, line)
            try:
                grid.Name = label
            except Exception:
                pass
            _made(report, "grid", label, grid)
        except Exception as exc:
            report.failed("grid", label, str(exc))


def _iter_mapping(mapping):
    """The model curves ``NewFootPrintRoof`` returns, in footprint-edge order.

    Revit hands back a ``ModelCurveArray`` (``Size`` / ``get_Item``); fall back to
    plain iteration for anything already list-like."""
    try:
        return [mapping.get_Item(i) for i in range(mapping.Size)]
    except Exception:
        try:
            return list(mapping)
        except Exception:
            return []


def _slope_eaves(roof_el, mapping, roof, report):
    """Make the eave edges slope-defining at the roof pitch, leaving the gable
    ends vertical. ``outline_slopes`` flags which footprint edges are eaves."""
    slopes = roof.get("outline_slopes") or []
    angle = float(roof.get("slope_angle", 0.0))
    if angle <= 0.0 or not slopes:
        return 0
    curves = _iter_mapping(mapping)
    made = 0
    for i, mc in enumerate(curves):
        if i < len(slopes) and slopes[i]:
            try:
                roof_el.set_DefinesSlope(mc, True)
                roof_el.set_SlopeAngle(mc, angle)
                made += 1
            except Exception as exc:
                report.note("roof slope not applied to edge %d: %s" % (i, exc))
    return made


def _build_roof(doc, data, levels, res, report):
    """A gable footprint roof over the building (experimental).

    Builds the footprint roof and makes its **eave edges slope-defining** at the
    plan's pitch (the gable ends stay vertical), so the roof comes out as a gable
    rather than flat. Falls back to a flat roof if the slope can't be applied.
    """
    roof = data.get("roof")
    if not roof:
        return
    if res.roof_type is None:
        report.note("roof skipped: no roof type in project")
        report.skipped("roof", "roof", "no roof type")
        return
    level = levels.get(roof.get("top_level", 0))
    if level is None:
        report.skipped("roof", "roof", "no top level")
        return
    try:
        arr = DB.CurveArray()
        for seg in roof.get("outline", []):
            (x1, y1), (x2, y2) = seg
            arr.Append(
                DB.Line.CreateBound(
                    DB.XYZ(float(x1), float(y1), level.Elevation),
                    DB.XYZ(float(x2), float(y2), level.Elevation),
                )
            )
        result = doc.Create.NewFootPrintRoof(arr, level, res.roof_type)
        roof_el = result[0] if isinstance(result, tuple) else result
        mapping = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        sloped = _slope_eaves(roof_el, mapping, roof, report) if mapping is not None else 0
        msg = "gable roof; %d eave edge(s) sloped" % sloped if sloped else (
            "flat footprint roof; gable pitch is a manual refinement"
        )
        _made(report, "roof", "roof", roof_el, message=msg)
    except Exception as exc:
        _logger.warning("roof: %s", exc)
        report.failed("roof", "roof", "experimental: %s" % exc)


def _build_stairs(doc, data, levels, report, dry_run):
    """Build stairs from the planned flights, each in its own edit scope.

    Uses the multi-flight plan the core computed (straight / switchback), creating
    one ``StairsRun`` per flight and an automatic landing between consecutive
    flights. Runs after the main transaction (StairsEditScope manages its own
    transactions); a dry run cancels the scope instead of committing, so the
    preview still exercises the API without persisting anything.
    """
    stairs = [a for a in data.get("areas", []) if a.get("kind") == "stair"]
    if not stairs:
        return

    from Autodesk.Revit.DB.Architecture import (
        StairsEditScope,
        StairsLanding,
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

        runs, layout, fits = _stair_runs_for(s)
        z = base.Elevation
        scope = StairsEditScope(doc, "barndsl stair")
        try:
            stairs_id = scope.Start(base.Id, top.Id)
            t = DB.Transaction(doc, "barndsl stair run")
            t.Start()
            try:
                created_runs = []
                for spec in runs:
                    p1 = DB.XYZ(float(spec["start"][0]), float(spec["start"][1]), z)
                    p2 = DB.XYZ(float(spec["end"][0]), float(spec["end"][1]), z)
                    run = StairsRun.CreateStraightRun(
                        doc, stairs_id, DB.Line.CreateBound(p1, p2), StairsRunJustification.Center
                    )
                    try:
                        run.ActualRunWidth = float(spec.get("width", MIN_STAIR_WIDTH))
                    except Exception:
                        pass
                    created_runs.append(run)
                # Automatic landings bridge consecutive flights (e.g. a switchback).
                for a, b in zip(created_runs, created_runs[1:]):
                    try:
                        StairsLanding.CreateAutomaticLanding(doc, a.Id, b.Id)
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
            msg = "%s, %d flight(s)" % (layout, len(runs))
            if not fits:
                msg += " — run exceeds the footprint; review"
            report.created("stair", s.get("id"), revit_id=_id_val(stairs_id), message=msg)
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
        if options.replace:
            _purge_managed(doc, report)
        levels = _ensure_levels(doc, data, report)
        walls = _build_walls(doc, data, levels, res, options, report)
        _build_openings(doc, data, levels, walls, res, options, report)
        _build_rooms(doc, data, levels, report)
        if options.ceilings:
            _build_ceilings(doc, data, levels, res, report)
        if options.structure:
            _build_structure(doc, data, levels, res, report)
        if options.fixtures:
            _build_fixtures(doc, data, levels, res, report)
        if options.slabs:
            _build_slabs(doc, data, levels, res, report)
        if options.foundation:
            _build_foundation(doc, data, levels, res, report)
        if options.porches:
            _build_porches(doc, data, levels, res, report)
        if options.grids:
            _build_grids(doc, data, report)
        if options.roof:
            _build_roof(doc, data, levels, res, report)
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


# --- documentation: views, tags, schedules, sheets ---------------------------

#: Created views/sheets/schedules are named with this prefix so a re-document
#: can find and replace exactly what a previous run made.
DOC_PREFIX = "barndsl - "


def _view_family_type(doc, view_family):
    for vft in _collect(doc, DB.ViewFamilyType):
        try:
            if vft.ViewFamily == view_family:
                return vft
        except Exception:
            pass
    return None


def _managed_in_category(doc, bic):
    out = []
    try:
        elems = (
            DB.FilteredElementCollector(doc)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        return out
    for e in elems:
        if _is_managed(e):
            out.append(e)
    return out


def _purge_documents(doc, report):
    """Delete barndsl-named sheets, schedules and views (plans, elevations,
    sections) plus managed dimensions/markers from a prior run."""
    removed = 0
    for cls in (DB.ViewSheet, DB.ViewSchedule, DB.ViewPlan, DB.ViewSection):
        try:
            views = _collect(doc, cls)
        except Exception:
            views = []
        for v in list(views):
            try:
                if _name(v).startswith(DOC_PREFIX):
                    doc.Delete(v.Id)
                    removed += 1
            except Exception:
                pass
    # Managed dimensions and elevation markers aren't name-prefixed.
    for cls in (DB.Dimension, DB.ElevationMarker):
        try:
            elems = _collect(doc, cls)
        except Exception:
            elems = []
        for e in list(elems):
            if _is_managed(e):
                try:
                    doc.Delete(e.Id)
                    removed += 1
                except Exception:
                    pass
    if removed:
        report.note("replaced %d view/sheet/schedule/dimension(s) from a previous run" % removed)
    return removed


def _make_views(doc, levels, vft, report):
    """One floor-plan view per level; returns ``{level_id_value: view}``."""
    views = {}
    if vft is None:
        report.note("views skipped: no floor-plan view type in project")
        return views
    try:
        if not vft.IsActive:
            vft.Activate()
            doc.Regenerate()
    except Exception:
        pass
    for lv in levels:
        name = DOC_PREFIX + _name(lv)
        try:
            view = DB.ViewPlan.Create(doc, vft.Id, lv.Id)
            try:
                view.Name = name
            except Exception:
                pass
            views[_id_val(lv.Id)] = view
            report.created("view", _name(lv), revit_id=_rid(view))
        except Exception as exc:
            report.failed("view", _name(lv), str(exc))
    return views


def _tag_in_view(doc, view, report):
    """Place room/door/window tags for managed elements, in ``view``."""
    # Room tags (NewRoomTag needs no tag type).
    for rm in _managed_in_category(doc, DB.BuiltInCategory.OST_Rooms):
        try:
            loc = rm.Location.Point
            uv = DB.UV(loc.X, loc.Y)
            doc.Create.NewRoomTag(DB.LinkElementId(rm.Id), uv, view.Id)
            report.created("tag", "room", revit_id=_rid(rm))
        except Exception as exc:
            report.failed("tag", "room", str(exc))

    # Door / window tags.
    for bic, label in (
        (DB.BuiltInCategory.OST_Doors, "door"),
        (DB.BuiltInCategory.OST_Windows, "window"),
    ):
        for inst in _managed_in_category(doc, bic):
            try:
                loc = inst.Location.Point
                ref = DB.Reference(inst)
                DB.IndependentTag.Create(
                    doc, view.Id, ref, False, DB.TagMode.TM_ADDBY_CATEGORY,
                    DB.TagOrientation.Horizontal, loc,
                )
                report.created("tag", label, revit_id=_rid(inst))
            except Exception as exc:
                report.failed("tag", label, str(exc))


def _make_schedules(doc, report):
    specs = [
        (DB.BuiltInCategory.OST_Doors, "Doors"),
        (DB.BuiltInCategory.OST_Windows, "Windows"),
        (DB.BuiltInCategory.OST_Rooms, "Rooms"),
    ]
    made = []
    for bic, label in specs:
        try:
            sched = DB.ViewSchedule.CreateSchedule(doc, DB.ElementId(bic))
            try:
                sched.Name = DOC_PREFIX + label
            except Exception:
                pass
            made.append(sched)
            report.created("schedule", label, revit_id=_rid(sched))
        except Exception as exc:
            report.failed("schedule", label, str(exc))
    return made


def _wall_endpoints(w):
    """The two plan endpoints of a wall — from a real ``Location.Curve`` or the
    fake's stored line — as ``((x1, y1), (x2, y2))``, or None."""
    loc = getattr(w, "Location", None)
    curve = getattr(loc, "Curve", None) if loc is not None else getattr(w, "curve", None)
    if curve is None:
        return None
    try:
        a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
    except Exception:
        a, b = getattr(curve, "p1", None), getattr(curve, "p2", None)
    if a is None or b is None:
        return None
    return ((a.X, a.Y), (b.X, b.Y))


def _model_bounds(doc):
    """Plan bounds ``(minx, miny, maxx, maxy)`` of the barndsl-built walls, for
    placing elevation markers and a section. None if there are no managed walls."""
    xs, ys = [], []
    for w in _collect(doc, DB.Wall):
        if not _is_managed(w):
            continue
        ends = _wall_endpoints(w)
        if ends is None:
            continue
        (x1, y1), (x2, y2) = ends
        xs.extend([x1, x2])
        ys.extend([y1, y2])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _dimension_ground_plan(doc, view, report):
    """Overall grid-to-grid dimension strings on the ground plan.

    Dimensions reference the structural grids a frame produced (Revit needs
    referenceable geometry; grids are the clean choice). One string per grid
    direction, on a dimension line just outside the building. Skipped with a note
    when there are no grids to dimension."""
    grids = [g for g in _collect(doc, DB.Grid) if _is_managed(g)]
    if not grids:
        report.note("plan dimensions skipped: no grids to dimension (place a frame)")
        return
    bounds = _model_bounds(doc)
    if bounds is None:
        return
    minx, miny, maxx, maxy = bounds
    vert, horiz = [], []  # grids running N-S (constant x) vs E-W (constant y)
    for g in grids:
        curve = getattr(g, "Curve", None) or getattr(g, "line", None)
        if curve is None:
            continue
        try:
            a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
        except Exception:
            a, b = getattr(curve, "p1", None), getattr(curve, "p2", None)
        if a is None or b is None:
            continue
        (vert if abs(a.X - b.X) <= abs(a.Y - b.Y) else horiz).append((g, a, b))

    made = 0
    for group, along_x in ((vert, True), (horiz, False)):
        if len(group) < 2:
            continue
        refs = DB.ReferenceArray()
        for g, _a, _b in group:
            try:
                refs.Append(DB.Reference(g))
            except Exception:
                pass
        try:
            if along_x:  # vertical grids → a horizontal dimension line below the plan
                p1 = DB.XYZ(minx, miny - 5.0, 0.0)
                p2 = DB.XYZ(maxx, miny - 5.0, 0.0)
            else:  # horizontal grids → a vertical dimension line left of the plan
                p1 = DB.XYZ(minx - 5.0, miny, 0.0)
                p2 = DB.XYZ(minx - 5.0, maxy, 0.0)
            line = DB.Line.CreateBound(p1, p2)
            dim = DB.Dimension.Create(doc, view, line, refs)
            _mark(dim)
            report.created("dimension", "grid line", revit_id=_rid(dim))
            made += 1
        except Exception as exc:
            report.failed("dimension", "grid line", str(exc))
    if not made:
        report.note("plan dimensions: no dimension string could be placed")


def _make_elevations(doc, plan_view, report):
    """Four exterior elevations (North/South/East/West) from one marker centred on
    the building — a residential set's exterior elevations. Needs an elevation
    view type and a plan view to host them."""
    vft = _view_family_type(doc, DB.ViewFamily.Elevation)
    if vft is None:
        report.note("elevations skipped: no elevation view type in project")
        return
    if plan_view is None:
        report.note("elevations skipped: no plan view to host them")
        return
    bounds = _model_bounds(doc)
    if bounds is None:
        report.note("elevations skipped: no built walls to bound the building")
        return
    minx, miny, maxx, maxy = bounds
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    try:
        if not vft.IsActive:
            vft.Activate()
            doc.Regenerate()
    except Exception:
        pass
    try:
        marker = DB.ElevationMarker.CreateElevationMarker(
            doc, vft.Id, DB.XYZ(cx, cy, 0.0), 96
        )
    except Exception as exc:
        report.failed("elevation", "marker", str(exc))
        return
    _mark(marker)
    for i, name in enumerate(("North", "East", "South", "West")):
        try:
            elev = marker.CreateElevation(doc, plan_view.Id, i)
            try:
                elev.Name = DOC_PREFIX + name + " Elevation"
            except Exception:
                pass
            report.created("elevation", name, revit_id=_rid(elev))
        except Exception as exc:
            report.failed("elevation", name, str(exc))


def _make_section(doc, levels, report):
    """One transverse building section cutting across the plan — the section a
    residential set needs to show wall/roof heights. Experimental: the section's
    orientation transform needs live-Revit confirmation, so a failure is noted."""
    vft = _view_family_type(doc, DB.ViewFamily.Section)
    if vft is None:
        report.note("section skipped: no section view type in project")
        return
    bounds = _model_bounds(doc)
    if bounds is None:
        report.note("section skipped: no built walls to bound the building")
        return
    minx, miny, maxx, maxy = bounds
    top = max((lv.Elevation for lv in levels), default=0.0) + 12.0
    try:
        bbox = DB.BoundingBoxXYZ()
        # Look north across the middle of the building: the section box's local
        # X spans east-west, Y spans elevation, Z is the view depth (northward).
        cy = (miny + maxy) / 2.0
        t = DB.Transform.Identity
        t.Origin = DB.XYZ((minx + maxx) / 2.0, cy, 0.0)
        t.BasisX = DB.XYZ(1.0, 0.0, 0.0)
        t.BasisY = DB.XYZ(0.0, 0.0, 1.0)
        t.BasisZ = DB.XYZ(0.0, -1.0, 0.0)
        bbox.Transform = t
        half_w = (maxx - minx) / 2.0 + 2.0
        bbox.Min = DB.XYZ(-half_w, -2.0, -(maxy - miny) / 2.0 - 2.0)
        bbox.Max = DB.XYZ(half_w, top, (maxy - miny) / 2.0 + 2.0)
        section = DB.ViewSection.CreateSection(doc, vft.Id, bbox)
        try:
            section.Name = DOC_PREFIX + "Building Section"
        except Exception:
            pass
        report.created("section", "building", revit_id=_rid(section))
    except Exception as exc:
        report.failed("section", "building", "experimental: %s" % exc)


def _make_sheets(doc, levels, views, title_block, report, schedules=None):
    if title_block is None:
        report.note("sheets skipped: no title block loaded")
        return
    try:
        if not title_block.IsActive:
            title_block.Activate()
            doc.Regenerate()
    except Exception:
        pass
    for lv in levels:
        view = views.get(_id_val(lv.Id))
        if view is None:
            continue
        try:
            sheet = DB.ViewSheet.Create(doc, title_block.Id)
            try:
                sheet.Name = DOC_PREFIX + _name(lv) + " Plan"
            except Exception:
                pass
            DB.Viewport.Create(doc, sheet.Id, view.Id, DB.XYZ(1.0, 1.0, 0.0))
            report.created("sheet", _name(lv), revit_id=_rid(sheet))
        except Exception as exc:
            report.failed("sheet", _name(lv), str(exc))
    # A dedicated schedule sheet so the door/window/room schedules land on a
    # drawing rather than only living in the browser.
    if schedules:
        try:
            sheet = DB.ViewSheet.Create(doc, title_block.Id)
            try:
                sheet.Name = DOC_PREFIX + "Schedules"
            except Exception:
                pass
            y = 1.0
            for sched in schedules:
                try:
                    DB.ScheduleSheetInstance.Create(
                        doc, sheet.Id, sched.Id, DB.XYZ(0.5, y, 0.0)
                    )
                    y -= 0.5
                except Exception as exc:
                    report.failed("sheet", "schedule placement", str(exc))
            report.created("sheet", "Schedules", revit_id=_rid(sheet))
        except Exception as exc:
            report.failed("sheet", "Schedules", str(exc))


def document(doc, options=None):
    """Create floor-plan views, tags, schedules and a sheet per level from the
    model a build produced. Decoupled from :func:`build` (run it when you're
    ready to document) and **idempotent**: a re-document replaces the barndsl
    views/sheets/schedules a previous run made. Returns a :class:`BuildReport`.
    """
    if options is None:
        options = _report.BuildOptions()
    report = _report.BuildReport()

    vft = _view_family_type(doc, DB.ViewFamily.FloorPlan)
    title_block = (_symbols(doc, DB.BuiltInCategory.OST_TitleBlocks) or [None])[0]
    report.resources["floor_plan_view_type"] = _name(vft) if vft else "(none)"
    report.resources["title_block"] = (title_block.Family.Name if title_block else "(none)")

    levels = sorted(_collect(doc, DB.Level), key=lambda e: e.Elevation)

    t = DB.Transaction(doc, "barndsl documentation")
    t.Start()
    try:
        if options.replace:
            _purge_documents(doc, report)
        views = _make_views(doc, levels, vft, report) if options.views else {}
        if options.tags:
            for view in views.values():
                _tag_in_view(doc, view, report)
        # The ground plan hosts dimensions and the elevation marker.
        ground = views.get(_id_val(levels[0].Id)) if (views and levels) else None
        if getattr(options, "dimensions", True) and ground is not None:
            _dimension_ground_plan(doc, ground, report)
        if getattr(options, "elevations", True):
            _make_elevations(doc, ground, report)
        if getattr(options, "sections", True):
            _make_section(doc, levels, report)
        schedules = _make_schedules(doc, report) if options.schedules else []
        if options.sheets:
            _make_sheets(doc, levels, views, title_block, report, schedules)
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise

    _logger.info("barndsl document: %s", report.summary_line())
    return report
