# -*- coding: utf-8 -*-
"""Create Revit elements from a ``barndsl.revit/1`` exchange.

Talks to the **Revit API**, so it only imports cleanly *inside* Revit (under
pyRevit). Import it lazily from a pushbutton script — never from the Revit-free
:mod:`barndsl_revit.exchange` / :mod:`barndsl_revit.report`.

**Primary target: Revit 2025** (.NET 8 / pyRevit 5, CPython 3.12 engine). Uses
APIs current in 2025 (``Floor.Create``; ``ElementId.Value`` over the deprecated
``IntegerValue``; the component Stairs API) and avoids removed members.

Built for debugging a real run:

* Every element's outcome (created / kept / skipped / failed, with the Revit id
  and a reason) is recorded in a :class:`barndsl_revit.report.BuildReport`, which
  renders to markdown for the pyRevit panel and JSON for a build-log file.
* Re-builds are **diff-based** by default: every element is stamped with its
  exchange identity + record fingerprint (Extensible Storage), and a ``replace``
  re-build keeps whatever is unchanged — preserving Revit element ids so user
  annotations survive iteration. ``rebuild: "full"`` restores purge-and-recreate.
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

import math
import sys

from pyrevit import DB

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

#: The v1 schema (marker only). ES schemas are **immutable once created in a
#: document**, so the extended identity stamp lives in a new schema under a new
#: GUID; this one is still *read* so elements from a v1 build are recognised as
#: managed (they carry no identity, so a diff rebuild purges them — the old
#: behaviour). Never written by new builds.
_LEGACY_SCHEMA_GUID = "b47d9a1e-6f3c-4c2a-9d18-2a1f0c7e5b64"
#: The current schema: the marker plus the element's **exchange identity key**
#: and the **fingerprint** of the exchange record that produced it, so a diff
#: rebuild can keep unchanged elements in place (preserving their Revit ids).
#: That two-schema dance (read old, write new) needs live-Revit confirmation on
#: a document that carries v1 stamps.
_IDENTITY_SCHEMA_GUID = "3f8c5d2a-9b41-4e7f-8c06-5d2e9a7b1c43"
_MANAGED_FIELD = "marker"
_KEY_FIELD = "key"
_FINGERPRINT_FIELD = "fingerprint"


def _es():
    """The ExtensibleStorage namespace + the System bits, or ``None`` if the
    running engine can't provide them (then the Comments fallback is used)."""
    try:
        from Autodesk.Revit.DB import ExtensibleStorage as ES
        from System import Guid, String

        return ES, Guid, String
    except Exception:
        return None


def _legacy_schema():
    """The v1 marker-only schema, looked up read-only (never created/written)."""
    bits = _es()
    if bits is None:
        return None
    ES, Guid, _String = bits
    try:
        return ES.Schema.Lookup(Guid(_LEGACY_SCHEMA_GUID))
    except Exception:
        return None


def _identity_schema(create=False):
    """Look up (or, when ``create``, build) the identity-stamp schema."""
    bits = _es()
    if bits is None:
        return None
    ES, Guid, String = bits
    try:
        guid = Guid(_IDENTITY_SCHEMA_GUID)
        schema = ES.Schema.Lookup(guid)
        if schema is not None or not create:
            return schema
        b = ES.SchemaBuilder(guid)
        b.SetSchemaName("BarndslManagedV2")
        b.SetReadAccessLevel(ES.AccessLevel.Public)
        b.SetWriteAccessLevel(ES.AccessLevel.Public)
        b.AddSimpleField(_MANAGED_FIELD, String)
        b.AddSimpleField(_KEY_FIELD, String)
        b.AddSimpleField(_FINGERPRINT_FIELD, String)
        return b.Finish()
    except Exception:
        return None


def _es_mark(elem, key=None, fingerprint=None):
    """Stamp the managed marker (+ identity, when known) into Extensible
    Storage. Returns True on success."""
    bits = _es()
    if bits is None:
        return False
    _ES, _Guid, String = bits
    schema = _identity_schema(create=True)
    if schema is None:
        return False
    try:
        from Autodesk.Revit.DB import ExtensibleStorage as ES

        ent = ES.Entity(schema)
        ent.Set[String](_MANAGED_FIELD, MANAGED_MARK)
        ent.Set[String](_KEY_FIELD, key or "")
        ent.Set[String](_FINGERPRINT_FIELD, fingerprint or "")
        elem.SetEntity(ent)
        return True
    except Exception:
        return False


def _es_entity(elem, schema):
    """The element's valid entity for ``schema``, or None."""
    if schema is None:
        return None
    try:
        ent = elem.GetEntity(schema)
        if ent is None or not ent.IsValid():
            return None
        return ent
    except Exception:
        return None


def _es_is_managed(elem):
    bits = _es()
    if bits is None:
        return False
    _ES, _Guid, String = bits
    for schema in (_identity_schema(create=False), _legacy_schema()):
        ent = _es_entity(elem, schema)
        if ent is None:
            continue
        try:
            if ent.Get[String](_MANAGED_FIELD) == MANAGED_MARK:
                return True
        except Exception:
            pass
    return False


def _es_identity(elem):
    """The ``(identity_key, fingerprint)`` stamped on a managed element, or
    ``None`` when it carries no identity (a legacy/v1 build, a mark written
    without a key, or no Extensible Storage engine)."""
    bits = _es()
    if bits is None:
        return None
    _ES, _Guid, String = bits
    ent = _es_entity(elem, _identity_schema(create=False))
    if ent is None:
        return None
    try:
        if ent.Get[String](_MANAGED_FIELD) != MANAGED_MARK:
            return None
        key = ent.Get[String](_KEY_FIELD) or ""
        fp = ent.Get[String](_FINGERPRINT_FIELD) or ""
    except Exception:
        return None
    if not key:
        return None
    return (key, fp)

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


def _mark(elem, key=None, fingerprint=None):
    """Stamp an element as barndsl-managed (with its exchange identity, when
    known). Prefers Extensible Storage (private, out of the way); only if that
    engine is unavailable does it fall back to the Comments field — a Comments
    mark carries no identity, so such elements are treated as legacy (purged,
    never kept) on a diff rebuild."""
    if _es_mark(elem, key=key, fingerprint=fingerprint):
        return elem
    p = _comments_param(elem)
    if p is not None and not p.IsReadOnly:
        try:
            p.Set(MANAGED_MARK)
        except Exception:
            pass
    return elem


def _made(report, kind, source, elem, message="", key=None, fingerprint=None):
    """Mark a freshly-created element (stamping its identity) and record it."""
    _mark(elem, key=key, fingerprint=fingerprint)
    return report.created(kind, source, revit_id=_rid(elem), message=message)


class _Rebuild(object):
    """Diff-rebuild bookkeeping threaded through the build passes.

    ``fps`` maps every incoming record's identity key to its fingerprint (from
    :func:`exchange.identities`) so passes stamp exactly what the diff will
    compare next time. ``kept`` maps identity keys to the existing elements a
    diff decided to keep; a pass *takes* its element (so it is claimed once),
    reports it ``kept`` and skips creation.
    """

    def __init__(self, fps=None, kept=None, candidate=None):
        self.fps = fps or {}
        self.kept = kept or {}
        #: When set, every identity key a pass computes is namespaced under this
        #: candidate label (Design Options workflow) before it is looked up or
        #: stamped — so option-scoped builds diff independently. ``None`` leaves
        #: keys byte-identical, so a plain build stamps exactly as before.
        self.candidate = candidate or None

    def key(self, base):
        """Namespace a pass's base identity key for the current candidate (a
        no-op for a plain build)."""
        return _exchange.namespaced_key(base, self.candidate)

    def fp(self, key):
        return self.fps.get(key, "")

    def take(self, key):
        return self.kept.pop(key, None)


def _resource_context(res, options):
    """Per-kind context strings for the **resolved** resources + placement
    options that shape each kind's elements.

    Passed to :func:`exchange.identities` and folded into the fingerprints
    so that rebuilding with a different wall type, family, ``location_line`` or
    ``size_families`` setting *recreates* the affected elements — otherwise a
    diff rebuild would keep elements built with the old resources while the
    report claimed the new ones were in use. Uses the resolved names (override
    → hint → auto-pick), so only an *effective* change re-fingerprints.

    Deliberate exception: the auto-picked **ceiling type** is not folded in. It
    has no config override, and folding it would make kept ceilings get purged
    (unreproducible — the pass skips without a type) the moment a project loses
    its ceiling type, which is strictly worse than keeping them.
    """

    def nm(x):
        return _name(x) if x is not None else "(none)"

    def fam(x):
        try:
            return x.Family.Name if x is not None else "(none)"
        except Exception:
            return "(none)"

    sizing = "sized" if options.size_families else "unsized"
    opening = "opening:%s|%s|%s|%s" % (
        fam(res.door), fam(res.garage_door), fam(res.window), sizing
    )
    struct = "structure:%s|%s|%s" % (fam(res.column), fam(res.beam), sizing)
    floor = "floor:%s" % nm(res.floor)
    wall = "wall:%s|%s|%s" % (
        nm(res.ext_wall), nm(res.int_wall),
        # None and "centerline" land walls identically, so they share a context.
        (getattr(options, "location_line", None) or "centerline"),
    )
    out = {
        "wall": wall,
        "door": opening,
        "window": opening,
        "opening": opening,
        "slab": floor,
        "porch": floor,
        "roof": "roof:%s" % nm(res.roof_type),
        "column": struct,
        "framing": struct,
        "fixture": "fixture:%s|%s" % (fam(res.plumbing), fam(res.appliance)),
        "footing": "footing:%s" % fam(res.footing),
    }
    # Per-sub-kind context ("wall/plumbing", "window/fixed", "door/double"):
    # read by identities() for exactly the records carrying that kind, so a
    # changed kind mapping recreates only the matching elements. A kind that
    # resolved to the pass's standard pick is omitted — it builds identically,
    # and omitting it keeps pre-kind fingerprints byte-compatible (no one-time
    # recreate of every default window on the first rebuild).
    for k, v in sorted(res.kind_walls.items()):
        if v is not res.int_wall:
            out["wall/%s" % k] = "%s|%s=%s" % (wall, k, nm(v))
    for k, v in sorted(res.window_kinds.items()):
        if v is not res.window:
            out["window/%s" % k] = "%s|%s=%s" % (opening, k, fam(v))
    for k, v in sorted(res.door_kinds.items()):
        if v is not res.door:
            out["door/%s" % k] = "%s|%s=%s" % (opening, k, fam(v))
    return out


#: Which options flag turns each managed element kind's pass off. A disabled
#: pass builds nothing, so a diff rebuild must not *keep* those kinds either —
#: mirroring the full purge, which deletes them.
_KIND_FLAGS = {
    "ceiling": "ceilings",
    "column": "structure",
    "framing": "structure",
    "fixture": "fixtures",
    "slab": "slabs",
    "footing": "foundation",
    "porch": "porches",
    "grid": "grids",
    "roof": "roof",
}


def _in_scope(elem, candidate):
    """Does this managed element belong to the current build's *candidate scope*?

    A candidate build (Design Options workflow) owns **only** elements whose
    stamped identity key sits under its own namespace — it never touches another
    candidate's elements, a plain build's elements, or identity-less legacy
    elements. A plain build (``candidate`` is ``None``) owns every element that
    is *not* namespaced to some candidate — every plain-keyed element plus legacy
    (identity-less) managed elements, exactly the historic reach. This is what
    makes the diff/purge cross-candidate safe: building candidate B never purges
    candidate A's model.
    """
    ident = _es_identity(elem)
    if candidate:
        if ident is None:
            return False  # legacy / plain / other-candidate — leave it alone
        return ident[0].startswith(_exchange.candidate_prefix(candidate))
    # Plain build: own everything that isn't a candidate's.
    if ident is None:
        return True
    return not _exchange.is_candidate_key(ident[0])


def _purge_managed(doc, report, candidate=None):
    """Delete every element a previous barndsl build created, so a re-build
    replaces rather than duplicates (the ``rebuild: "full"`` path). Identifies
    them by the managed mark (Extensible Storage, or the legacy Comments mark);
    never touches anything the user drew, nor — under a ``candidate`` build —
    any element outside that candidate's namespace. Runs inside the build
    transaction."""
    try:
        elems = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()
    except Exception:
        return 0
    removed = 0
    for e in list(elems):
        if _is_managed(e) and _in_scope(e, candidate):
            try:
                doc.Delete(e.Id)
                removed += 1
            except Exception:
                pass
    if removed:
        report.note("replaced %d element(s) from a previous barndsl build" % removed)
    return removed


def _diff_managed(doc, idents, options, report, candidate=None):
    """The diff arm of a replace re-build: keep managed elements whose stamped
    identity+fingerprint match an incoming record, delete the rest.

    Returns ``{identity_key: element}`` for the keeps — the passes then report
    those as ``kept`` instead of recreating them, so their Revit element ids
    (and any user dimensions/tags attached to them) survive the iteration.

    Deleted: elements whose record changed (recreated by their pass), whose
    identity is stale (no incoming record — removed from the plan), whose kind's
    pass is now disabled, and legacy managed elements with no identity stamp (a
    v1 or Comments-marked build — old behaviour). Deleting a wall makes Revit
    cascade-delete its hosted doors/windows/cuts; the fingerprint scheme folds
    each host wall's fingerprint into its openings', so any opening whose host
    is deleted was never a keep — a later ``Delete`` on the already-gone element
    is swallowed. Runs inside the build transaction.

    Under a ``candidate`` build the whole diff is scoped to that candidate's
    namespace (:func:`_in_scope`): elements from other candidates — and from
    plain builds — are neither kept-here nor deleted, so building candidate B is
    guaranteed not to purge candidate A's model (the incoming keys are already
    namespaced, so a foreign element could never match one anyway; the scope
    guard also keeps foreign elements out of the legacy-purge arm).
    """
    incoming = {}
    for kind, _source, key, fp in idents:
        flag = _KIND_FLAGS.get(kind)
        if flag is not None and not getattr(options, flag, True):
            continue
        incoming[key] = fp
    try:
        elems = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()
    except Exception:
        return {}
    kept = {}
    removed = 0
    legacy = 0
    for e in list(elems):
        if not (_is_managed(e) and _in_scope(e, candidate)):
            continue  # unmanaged, or another candidate's / a plain build's element
        ident = _es_identity(e)
        if ident is not None:
            key, fp = ident
            if incoming.get(key) == fp and key not in kept:
                kept[key] = e
                continue
            try:
                doc.Delete(e.Id)
                removed += 1
            except Exception:
                pass
        else:
            legacy += 1
            try:
                doc.Delete(e.Id)
            except Exception:
                pass
    if removed:
        report.note(
            "replaced %d changed/removed element(s) from the previous barndsl build"
            % removed
        )
    if legacy:
        report.note(
            "purged %d element(s) from an older barndsl build (no identity stamp)"
            % legacy
        )
    return kept


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


def _roof_type_named(doc, name):
    if not name:
        return None
    try:
        types = _collect(doc, DB.RoofType)
    except Exception:
        return None
    for rt in types:
        if _name(rt) == name:
            return rt
    return None


def _hint_tokens(hint):
    """The words of a finish hint, lowercased ("standing-seam metal" →
    ["standing", "seam", "metal"]) for a contains-any name match."""
    if not hint:
        return []
    text = str(hint).lower()
    for sep in ("-", "_", "/", ","):
        text = text.replace(sep, " ")
    return [t for t in text.split() if t]


def _hinted_wall_type(doc, hint):
    """An exterior wall type whose name contains a hint token (case-insensitive),
    e.g. siding "metal" → "Exterior - Metal Panel". Prefers Exterior-function
    types; falls back to any basic type whose name matches. None if no match."""
    tokens = _hint_tokens(hint)
    if not tokens:
        return None
    matches = []
    for wt in _basic_wall_types(doc):
        nm = _name(wt).lower()
        if any(t in nm for t in tokens):
            matches.append(wt)
    for wt in matches:
        try:
            if wt.Function == DB.WallFunction.Exterior:
                return wt
        except Exception:
            pass
    return matches[0] if matches else None


#: Name tokens that identify a wall type for a declared wall kind, and a
#: door/window family for an authored opening kind (contains-any, lowercase).
_WALL_KIND_TOKENS = {
    "plumbing": ("plumbing", "wet"),
    "rated": ("rated", "fire"),
    "bearing": ("bearing",),
}
_OPENING_KIND_TOKENS = {
    "casement": ("casement",),
    "slider": ("slid",),  # matches Slider and Sliding
    "fixed": ("fixed",),
    "double-hung": ("hung",),
    "double": ("double",),
    "french": ("french",),
}


def _kind_wall_type(doc, kind):
    """A basic wall type whose name reads like the declared ``kind``
    (plumbing/wet, rated/fire, bearing). Prefers Interior-function types —
    declared walls are shared room walls. None if no name matches."""
    tokens = _WALL_KIND_TOKENS.get(kind, ())
    matches = [
        wt for wt in _basic_wall_types(doc)
        if any(t in _name(wt).lower() for t in tokens)
    ]
    for wt in matches:
        try:
            if wt.Function == DB.WallFunction.Interior:
                return wt
        except Exception:
            pass
    return matches[0] if matches else None


def _kind_symbol(doc, category, tokens, exclude=()):
    """The first family symbol in ``category`` whose family or type name
    contains one of ``tokens`` (case-insensitive) and none of ``exclude`` —
    a `double` door must not match "Overhead-Sectional Double" (a garage
    family). None if nothing matches."""
    for sym in _symbols(doc, category):
        try:
            names = "%s %s" % (sym.Family.Name, _name(sym))
        except Exception:
            names = _name(sym)
        low = names.lower()
        if any(t in low for t in exclude):
            continue
        if any(t in low for t in tokens):
            return sym
    return None


def _hinted_roof_type(doc, hint):
    """A roof type whose name contains a hint token, e.g. roofing
    "standing-seam metal" → "Standing Seam Metal". None if no match."""
    tokens = _hint_tokens(hint)
    if not tokens:
        return None
    try:
        types = _collect(doc, DB.RoofType)
    except Exception:
        return None
    for rt in types:
        nm = _name(rt).lower()
        if any(t in nm for t in tokens):
            return rt
    return None


def _floor_type_named(doc, name):
    if not name:
        return None
    for ft in _collect(doc, DB.FloorType):
        if _name(ft) == name:
            return ft
    return None


#: Family-name fragments that read as an overhead/sectional garage door.
_GARAGE_DOOR_HINTS = ("garage", "overhead", "sectional")


def _garage_door_symbol(doc):
    """The first door family whose name reads like a garage door, or None.

    The auto-pick for kind == "overhead" openings: prefer a family named like
    "Garage-Sectional" / "Overhead Door" over the standard swing-leaf family.
    """
    for s in _symbols(doc, DB.BuiltInCategory.OST_Doors):
        name = (s.Family.Name or "").lower()
        for hint in _GARAGE_DOOR_HINTS:
            if hint in name:
                return s
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
        self.garage_door = None
        self.window = None
        self.floor = None
        self.column = None
        self.beam = None
        self.roof_type = None
        self.ceiling_type = None
        self.plumbing = None
        self.appliance = None
        self.footing = None
        # Declared-wall-kind → wall type, and opening-kind → family symbol,
        # resolved only for the kinds the incoming exchange actually uses.
        self.kind_walls = {}
        self.window_kinds = {}
        self.door_kinds = {}


def _resolve_resources(doc, options, report, data=None):
    """Pick the wall/floor/family types, honouring named overrides.

    Precedence per pass: a **named** config override, then (for the exterior
    wall / roof) a plan **finish hint** match (``siding``/``roofing`` keywords
    against type names), then the auto-pick. A missing named override falls back
    with a note. The chosen names are recorded in ``report.resources``.
    """
    res = _Resources()
    auto_ext, auto_int = _pick_wall_types(doc)
    plan_info = (data or {}).get("plan") or {}

    def named_or(name, finder, fallback, label):
        if name:
            found = finder(name)
            if found is not None:
                return found
            report.note("%s '%s' not found in project; using an auto-pick" % (label, name))
        return fallback

    # A finish hint sits between the named override and the auto-pick: honour it
    # when it matches a type, note which path won.
    siding = plan_info.get("siding")
    ext_fallback = auto_ext
    if siding:
        hinted = _hinted_wall_type(doc, siding)
        if hinted is not None:
            ext_fallback = hinted
        else:
            report.note("siding hint '%s' matched no wall type; using an auto-pick" % siding)

    res.ext_wall = named_or(
        options.exterior_wall_type, lambda n: _wall_type_named(doc, n), ext_fallback, "exterior wall type"
    )
    if siding and res.ext_wall is ext_fallback and ext_fallback is not auto_ext:
        report.note(
            "exterior wall type '%s' picked by siding hint '%s'" % (_name(res.ext_wall), siding)
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
    # Overhead (garage) doors get their own pick: a named override, else the
    # first door family whose name reads garage/overhead/sectional. None means
    # the standard door family stands in (noted per overhead opening).
    res.garage_door = named_or(
        options.garage_door_family,
        lambda n: _symbol_named(doc, DB.BuiltInCategory.OST_Doors, n),
        _garage_door_symbol(doc),
        "garage-door family",
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
    report.resources["garage_door_family"] = (
        res.garage_door.Family.Name if res.garage_door else "(standard door)"
    )
    report.resources["window_family"] = res.window.Family.Name if res.window else "(none loaded)"
    report.resources["floor_type"] = _name(res.floor) if res.floor else "(none)"

    # Roof type: named override → roofing hint → the first roof type.
    try:
        auto_roof = (_collect(doc, DB.RoofType) or [None])[0]
    except Exception:
        auto_roof = None
    roofing = plan_info.get("roofing")
    roof_fallback = auto_roof
    if roofing:
        hinted = _hinted_roof_type(doc, roofing)
        if hinted is not None:
            roof_fallback = hinted
        else:
            report.note("roofing hint '%s' matched no roof type; using an auto-pick" % roofing)
    res.roof_type = named_or(
        getattr(options, "roof_type", None),
        lambda n: _roof_type_named(doc, n),
        roof_fallback,
        "roof type",
    )
    if roofing and res.roof_type is roof_fallback and roof_fallback is not auto_roof:
        report.note(
            "roof type '%s' picked by roofing hint '%s'" % (_name(res.roof_type), roofing)
        )
    report.resources["roof_type"] = _name(res.roof_type) if res.roof_type else "(none)"
    try:
        res.ceiling_type = (_collect(doc, DB.CeilingType) or [None])[0]
    except Exception:
        res.ceiling_type = None
    report.resources["ceiling_type"] = _name(res.ceiling_type) if res.ceiling_type else "(none)"

    # Declared wall kinds (plumbing/bearing/rated) and opening kinds, resolved
    # only for the kinds the incoming exchange actually uses: a named config
    # override, else a wall type / family whose name reads like the kind, else
    # the pass's standard pick (interior wall / window / door family).
    walls_in = (data or {}).get("walls") or []
    openings_in = (data or {}).get("openings") or []
    for wall_kind in sorted({w.get("kind") for w in walls_in if w.get("kind")}):
        chosen = named_or(
            getattr(options, "%s_wall_type" % wall_kind, None),
            lambda n: _wall_type_named(doc, n),
            _kind_wall_type(doc, wall_kind) or res.int_wall,
            "%s wall type" % wall_kind,
        )
        res.kind_walls[wall_kind] = chosen
        report.resources["%s_wall" % wall_kind] = _name(chosen) if chosen else "(none)"
    for win_kind in sorted(
        {o.get("kind") for o in openings_in if o.get("category") == "window"}
    ):
        # The default kind (casement) always uses the standard window pick —
        # the config `window_family` override / auto-pick — so old plans build
        # exactly as before and a casement-named family in the template can't
        # hijack the override (or churn every default window's fingerprint).
        if win_kind == "casement":
            continue
        sym = _kind_symbol(doc, DB.BuiltInCategory.OST_Windows, _OPENING_KIND_TOKENS.get(win_kind, ()))
        if sym is None:
            sym = res.window
            if res.window is not None:
                report.note(
                    "no window family reads '%s'; the standard window family "
                    "stands in" % win_kind
                )
        res.window_kinds[win_kind] = sym
    for door_kind in sorted(
        {
            o.get("kind")
            for o in openings_in
            if o.get("category") == "door" and o.get("kind") in ("double", "french")
        }
    ):
        sym = _kind_symbol(
            doc,
            DB.BuiltInCategory.OST_Doors,
            _OPENING_KIND_TOKENS.get(door_kind, ()),
            exclude=_GARAGE_DOOR_HINTS,
        )
        if sym is None:
            sym = res.door
            if res.door is not None:
                report.note(
                    "no door family reads '%s'; the sized standard door family "
                    "stands in" % door_kind
                )
        res.door_kinds[door_kind] = sym
    return res


# --- element passes ----------------------------------------------------------


def _set_project_north(doc, data, report):
    """Rotate the project's true north to match the plan's ``orientation``.

    barndsl's ``orientation`` is the compass azimuth (degrees, **clockwise** from
    true north) that plan-north (+y) points; the model's geometry stays in the
    plan frame (project north = plan north). Revit's ``ProjectPosition.Angle`` is
    the rotation of **true north from project north, counterclockwise positive**
    (radians). Plan-north at azimuth ``A`` puts true north ``A`` degrees
    counterclockwise of project north, so ``Angle = +radians(A)``. Sign
    convention needs live-Revit confirmation; a failure is noted, never fatal.
    """
    az = float((data.get("plan") or {}).get("orientation") or 0.0)
    if abs(az) < 1e-9:
        return
    try:
        loc = doc.ActiveProjectLocation
        pos = loc.GetProjectPosition(DB.XYZ.Zero)
        pos.Angle = math.radians(az)
        loc.SetProjectPosition(pos)
        report.created(
            "project", "true north", revit_id=_rid(loc),
            message="rotated to azimuth %.1f deg (plan-north bearing)" % az,
        )
    except Exception as exc:
        report.note("project north not set from orientation %.1f deg: %s" % (az, exc))


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


def _build_walls(doc, data, levels, res, options, report, rebuild):
    made = {}
    loc_line = _wall_location_line_value(getattr(options, "location_line", None))
    constrained = 0
    for w in data["walls"]:
        key = rebuild.key(_exchange.wall_identity(w))
        kept = rebuild.take(key)
        if kept is not None:
            # Unchanged since the last build: the existing wall (and any user
            # dimensions/tags on it) stays. Hosted openings still index it.
            report.kept("wall", w["id"], revit_id=_rid(kept))
            made[w["id"]] = kept
            continue
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
        # A declared wall kind (plumbing/bearing/rated) maps to its resolved
        # type; declared walls are interior, so exterior segments keep theirs.
        kind_wt = res.kind_walls.get(w.get("kind"))
        if kind_wt is not None and not w.get("exterior"):
            wtype = kind_wt
        wall = None
        gable = _is_gable(w)
        if gable:
            try:
                wall = _gable_wall(doc, w, wtype, level)
                _made(report, "wall", w["id"], wall, message="gable-end profile",
                      key=key, fingerprint=rebuild.fp(key))
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
                _made(report, "wall", w["id"], wall, key=key, fingerprint=rebuild.fp(key))
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


def _wall_opening_points(o, wall_d, z):
    """The two opposite corners of a rectangular wall cut for a cased opening:
    floor to the opening height, centred at the location, ``width`` wide along
    the host wall's run direction."""
    (sx, sy), (ex, ey) = wall_d["start"], wall_d["end"]
    dx, dy = float(ex) - float(sx), float(ey) - float(sy)
    run = math.hypot(dx, dy)
    ux, uy = (dx / run, dy / run) if run > 1e-9 else (1.0, 0.0)
    cx, cy = float(o["location"][0]), float(o["location"][1])
    half = float(o.get("width", 0.0)) / 2.0
    height = float(o.get("height", 0.0))
    p1 = DB.XYZ(cx - ux * half, cy - uy * half, z)
    p2 = DB.XYZ(cx + ux * half, cy + uy * half, z + height)
    return p1, p2


def _cased_wall_opening(doc, o, host, wall_d, level, report, key=None, fp=None):
    """Cut a real rectangular wall opening for a doorless cased opening (the
    open-concept walk-through), instead of hanging a swinging leaf. Returns True
    on success; a failure is noted so the caller can fall back to a sized door
    family (the previous behaviour)."""
    if wall_d is None:
        report.note("cased opening %s: host wall not in exchange; door-family fallback" % o["id"])
        return False
    try:
        z = level.Elevation if level else 0.0
        p1, p2 = _wall_opening_points(o, wall_d, z)
        op = doc.Create.NewOpening(host, p1, p2)
        _made(report, "opening", o["id"], op, message="cased opening (wall cut, no leaf)",
              key=key, fingerprint=fp)
        return True
    except Exception as exc:
        _logger.warning("cased opening %s: %s", o["id"], exc)
        report.note(
            "cased opening %s: wall opening failed (%s); sized door-family fallback"
            % (o["id"], exc)
        )
        return False


def _flip_door_swing(inst, o, rooms_by_id, wall_d, report):
    """Set a placed door's facing/hand from the authored swing.

    ``swing_into`` names the room the leaf opens into: work out which side of the
    host wall that room's rectangle lies on and flip the instance's facing when
    its ``FacingOrientation`` points the other way. ``hinge`` "far" flips the
    hand (the family default is assumed hinged at the near/south-west end — a
    judgment call that needs live-Revit confirmation). Best-effort: a family or
    API that refuses is noted, never fatal. Returns a message fragment."""
    swing = o.get("swing_into")
    hinge = o.get("hinge")
    if not swing and not hinge:
        return ""
    parts = []
    try:
        if swing:
            room = rooms_by_id.get(swing)
            if room is None or wall_d is None:
                report.note("door %s: swing room/wall unknown; family default kept" % o["id"])
            else:
                (sx, sy), (ex, ey) = wall_d["start"], wall_d["end"]
                lx, ly = float(o["location"][0]), float(o["location"][1])
                # Which side of the (axis-aligned) host wall is the room's centre?
                if abs(float(ex) - float(sx)) >= abs(float(ey) - float(sy)):
                    # Wall runs east-west; the door faces north or south.
                    room_c = float(room["y"]) + float(room["length"]) / 2.0
                    want = (0.0, 1.0) if room_c >= ly else (0.0, -1.0)
                else:
                    # Wall runs north-south; the door faces east or west.
                    room_c = float(room["x"]) + float(room["width"]) / 2.0
                    want = (1.0, 0.0) if room_c >= lx else (-1.0, 0.0)
                facing = inst.FacingOrientation
                if facing.X * want[0] + facing.Y * want[1] < 0.0:
                    inst.flipFacing()
                    parts.append("flipped to swing into %s" % swing)
                else:
                    parts.append("swings into %s" % swing)
        if hinge == "far":
            inst.flipHand()
            parts.append("hinge far")
    except Exception as exc:
        report.note("door %s: swing/hand flip not applied (%s)" % (o["id"], exc))
    return "; ".join(parts)


def _stamp_egress(inst, o):
    """Write the egress flag to the door's Comments ("barndsl egress") so a Revit
    schedule can filter egress doors. Best-effort — read-only/missing param is
    fine (the managed marker lives in Extensible Storage, not here)."""
    if not o.get("egress"):
        return
    try:
        p = _comments_param(inst)
        if p is not None and not p.IsReadOnly:
            p.Set("barndsl egress")
    except Exception:
        pass


def _build_openings(doc, data, levels, walls, res, options, report, rebuild):
    door_base = _activate(res.door, doc)
    garage_base = _activate(res.garage_door, doc)
    win_base = _activate(res.window, doc)
    win_kind_bases = dict(
        (k, _activate(s, doc)) for k, s in res.window_kinds.items() if s is not None
    )
    door_kind_bases = dict(
        (k, _activate(s, doc)) for k, s in res.door_kinds.items() if s is not None
    )
    st = DB.Structure.StructuralType.NonStructural
    cache = {}
    wall_dicts = _exchange.walls_by_id(data)
    rooms_by_id = dict((r["id"], r) for r in data.get("rooms", []))

    for o in data["openings"]:
        cased = o["category"] == "cased_opening"
        kind = "window" if o["category"] == "window" else "door"
        key = rebuild.key(_exchange.opening_identity(o))
        kept = rebuild.take(key)
        if kept is not None:
            # Unchanged opening on an unchanged (kept) host wall — the
            # fingerprint folds the host wall's in, so a recreated wall can
            # never leave a stale hosted instance behind.
            report.kept("opening" if cased else kind, o["id"], revit_id=_rid(kept))
            continue
        host = walls.get(o.get("host_wall"))
        if host is None:
            report.skipped("opening" if cased else kind, o["id"], "no host wall")
            continue
        level = levels.get(o["level"])
        wall_d = wall_dicts.get(o.get("host_wall"))
        if cased:
            # A cased opening has no leaf: cut a real wall opening. Only on
            # failure fall through to the old sized-door-family stand-in.
            if _cased_wall_opening(doc, o, host, wall_d, level, report,
                                   key=key, fp=rebuild.fp(key)):
                continue
        overhead = o.get("kind") == "overhead"
        if kind == "window":
            base = win_kind_bases.get(o.get("kind"), win_base)
        else:
            base = door_kind_bases.get(o.get("kind"), door_base)
        if overhead:
            # Prefer the garage-door family for a sectional/overhead door; the
            # sized standard door family stands in (with a note) when none is
            # loaded, so the opening still lands at the right size.
            if garage_base is not None:
                base = garage_base
            elif base is not None:
                report.note(
                    "overhead door %s: no garage-door family loaded (a name "
                    "containing garage/overhead/sectional); the sized standard "
                    "door family stands in" % o["id"]
                )
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
        message = ""
        if kind == "window":
            try:
                p = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
                if p is not None and not p.IsReadOnly:
                    p.Set(float(o.get("sill", 0.0)))
            except Exception:
                pass
        elif overhead:
            # An overhead door rides up its tracks — no swing to flip, and it
            # is never egress, so there is no flag to stamp.
            message = "overhead (garage) door"
        elif not cased:
            # Honour the authored swing side/hinge and stamp the egress flag.
            message = _flip_door_swing(inst, o, rooms_by_id, wall_d, report)
            _stamp_egress(inst, o)
        _made(report, kind, o["id"], inst, message=message,
              key=key, fingerprint=rebuild.fp(key))


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


def _room_number(elem):
    """A placed room's number string, or None. Best-effort."""
    try:
        p = elem.get_Parameter(DB.BuiltInParameter.ROOM_NUMBER)
        return p.AsString() if p is not None else None
    except Exception:
        return None


def _build_rooms(doc, data, levels, report, rebuild):
    # Number rooms sequentially per level: 101.., 201.. — the residential
    # convention (floor number × 100 + running count). Kept rooms keep their
    # stamped numbers, so pre-collect those per level: a new/recreated room
    # must skip past them (or an inserted room would collide with a kept one).
    counters = {}
    taken = {}
    for r in data["rooms"]:
        elem = rebuild.kept.get(rebuild.key(_exchange.room_identity(r)))
        if elem is None:
            continue
        num = _room_number(elem)
        if num:
            taken.setdefault(r.get("level", 0), set()).add(num)
    for r in data["rooms"]:
        key = rebuild.key(_exchange.room_identity(r))
        kept = rebuild.take(key)
        if kept is not None:
            # A kept room keeps its number (and any user edits to its
            # parameters). Rooms are placed at seeds independent of walls; a
            # kept room whose bounding walls were recreated in the same
            # transaction should re-bound to the new walls (needs live-Revit
            # confirmation).
            report.kept("room", r["id"], revit_id=_rid(kept))
            continue
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
        if r.get("zone"):
            # The declared `zone` lands in the room's Department parameter — a
            # built-in every Room has and every room schedule can group by, so
            # zoned schedules need no shared-parameter setup.
            _set_string_param(room, DB.BuiltInParameter.ROOM_DEPARTMENT, r["zone"])
        lvl = r.get("level", 0)
        lvl_taken = taken.setdefault(lvl, set())
        counters[lvl] = counters.get(lvl, 0) + 1
        while "%d%02d" % (lvl + 1, counters[lvl]) in lvl_taken:
            counters[lvl] += 1
        number = "%d%02d" % (lvl + 1, counters[lvl])
        lvl_taken.add(number)
        _set_string_param(room, DB.BuiltInParameter.ROOM_NUMBER, number)
        floor, base, ceil, wall = _ROOM_FINISHES.get(r.get("type", ""), _ROOM_FINISH_DEFAULT)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_FLOOR, floor)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_BASE, base)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_CEILING, ceil)
        _set_string_param(room, DB.BuiltInParameter.ROOM_FINISH_WALL, wall)
        _made(report, "room", r["id"], room, key=key, fingerprint=rebuild.fp(key))


def _build_ceilings(doc, data, levels, res, report, rebuild):
    """A flat ceiling per room, hosted at the room's ceiling height above its
    level — so the model has a reflected-ceiling plane and somewhere to host
    lighting. Uses each room's rectangle; skipped (with a note) when the project
    has no ceiling type. A per-room ceiling height (vaulted rooms) is honoured."""
    rooms = data.get("rooms", [])
    if not rooms:
        return
    if res.ceiling_type is None:
        # No type to build *new* ceilings with — but ceilings the diff decided
        # to keep still exist in the document, so report them before bailing
        # (or they'd silently vanish from the report while staying built).
        for r in rooms:
            key = rebuild.key(_exchange.ceiling_identity(r))
            kept = rebuild.take(key)
            if kept is not None:
                report.kept("ceiling", r["id"], revit_id=_rid(kept))
        report.note("ceilings skipped: no ceiling type in project")
        return
    plan_h = float(data.get("plan", {}).get("ceiling_height", 8.0))
    from System.Collections.Generic import List

    for r in rooms:
        key = rebuild.key(_exchange.ceiling_identity(r))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("ceiling", r["id"], revit_id=_rid(kept))
            continue
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
            _made(report, "ceiling", r["id"], ceil, key=key, fingerprint=rebuild.fp(key))
        except Exception as exc:
            report.failed("ceiling", r["id"], str(exc))


#: Section width/depth parameter names structural families commonly use — steel
#: (b/d/bf), generic (Width/Depth), timber (b/h). Tried in order, defensively.
_SECTION_WIDTH_NAMES = ("b", "Width", "bf")
_SECTION_DEPTH_NAMES = ("h", "Depth", "d")


def _sized_section(doc, base, width, depth, cache, report):
    """A family symbol duplicated per unique section size ("barndsl WxD") with
    its width/depth type parameters set — the structural mirror of
    :func:`_sized_symbol`. Column/framing families vary wildly in parameter
    naming, so common names are tried (b/h, Width/Depth, d/bf); when none takes,
    the **default** symbol is returned with a note (no half-sized duplicate)."""
    key = ("section", _id_val(base.Id), round(float(width), 4), round(float(depth), 4))
    if key in cache:
        return cache[key]
    target_name = "barndsl %.2fx%.2f" % (float(width), float(depth))
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
            report.note("could not size section '%s': %s" % (target_name, exc))
            cache[key] = _activate(base, doc)
            return cache[key]
        set_w = _set_double_param(sym, [], _SECTION_WIDTH_NAMES, width)
        set_d = _set_double_param(sym, [], _SECTION_DEPTH_NAMES, depth)
        doc.Regenerate()
        if not (set_w or set_d):
            report.note(
                "family '%s' has no settable section (b/h, Width/Depth); using its default size"
                % fam.Name
            )
            cache[key] = _activate(base, doc)
            return cache[key]
    cache[key] = _activate(sym, doc)
    return cache[key]


def _build_structure(doc, data, levels, res, options, report, rebuild):
    structure = data.get("structure", {})
    columns = structure.get("columns", [])
    framing = structure.get("framing", [])
    if not columns and not framing:
        return
    col_sym = _activate(res.column, doc)
    beam_sym = _activate(res.beam, doc)
    cache = {}

    def sized(base, size_ft):
        # Nominal square section from the exchange (Post.size / the frame's post
        # size for beams); 0/absent or sizing disabled keeps the default type.
        size = float(size_ft or 0.0)
        if base is None or size <= 0.0 or not options.size_families:
            return base
        return _sized_section(doc, base, size, size, cache, report)

    if columns and col_sym is None:
        report.note("structural columns skipped: no structural-column family loaded")
    for i, c in enumerate(columns):
        src = "post %d" % i
        key = rebuild.key(_exchange.column_identity(c))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("column", src, revit_id=_rid(kept))
            continue
        if col_sym is None:
            continue
        level = levels.get(c["level"])
        if level is None:
            continue
        try:
            sym = sized(col_sym, c.get("size"))
            inst = doc.Create.NewFamilyInstance(
                _xyz(c["point"], level.Elevation), sym, level, DB.Structure.StructuralType.Column
            )
            # Give the post a real height: rise from the floor to the plate (the
            # beam it carries) rather than the family's default stub. Prefer an
            # existing level at the plate elevation; otherwise offset the top above
            # this level.
            _raise_column(doc, inst, level, c, levels, report)
            _made(report, "column", src, inst, key=key, fingerprint=rebuild.fp(key))
        except Exception as exc:
            report.failed("column", src, str(exc))

    if framing and beam_sym is None:
        report.note("structural framing skipped: no structural-framing family loaded")
    for i, f in enumerate(framing):
        src = "%s %d" % (f.get("role", "beam"), i)
        key = rebuild.key(_exchange.framing_identity(f))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("framing", src, revit_id=_rid(kept))
            continue
        if beam_sym is None:
            continue
        level = levels.get(f["level"])
        # The bent/ridge sits at the plate (top of the posts), carried on the
        # exchange as ``z`` — not down at the floor level. Fall back to the level
        # elevation only for an old exchange without ``z``.
        z = float(f.get("z", level.Elevation if level else 0.0))
        try:
            curve = DB.Line.CreateBound(_xyz(f["start"], z), _xyz(f["end"], z))
            # ``size`` is the frame's nominal post section (the beams share it in
            # this MVP); absent on an old exchange → the family default.
            sym = sized(beam_sym, f.get("size"))
            inst = doc.Create.NewFamilyInstance(
                curve, sym, level, DB.Structure.StructuralType.Beam
            )
            _made(report, "framing", src, inst, key=key, fingerprint=rebuild.fp(key))
        except Exception as exc:
            report.failed("framing", src, str(exc))


#: Fixture kinds hosted from a plumbing family vs. an appliance (specialty) family.
_WET_FIXTURES = ("toilet", "lavatory", "tub", "shower", "sink")

#: Z-rotation (radians, counterclockwise) so a fixture backs onto its wall,
#: assuming the family default faces +Y/north with its back at -Y: backing the
#: south wall is the default; north faces south (pi); east faces west (+pi/2 —
#: rotating +Y counterclockwise 90° points -X); west faces east (-pi/2).
_FIXTURE_ROTATION = {"S": 0.0, "N": math.pi, "E": math.pi / 2.0, "W": -math.pi / 2.0}


def _rotate_fixture(doc, inst, fx, z, report):
    """Rotate a placed fixture about its own vertical axis so it backs onto the
    wall the seed was laid against (``fx["wall"]``, S/N/E/W). The seed point
    stays put — the core already placed the footprint flush to the wall.
    Best-effort; returns a message fragment for the record."""
    angle = _FIXTURE_ROTATION.get(fx.get("wall"))
    if not angle:
        return ""
    try:
        axis = DB.Line.CreateBound(_xyz(fx["point"], z), _xyz(fx["point"], z + 1.0))
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, angle)
        return "rotated to back onto %s wall" % fx["wall"]
    except Exception as exc:
        report.note("fixture %s: rotation not applied (%s)" % (fx.get("id"), exc))
        return ""


def _build_fixtures(doc, data, levels, res, report, rebuild):
    """Place a family instance at each fixture/appliance seed — a plumbing family
    for wet fixtures, a specialty-equipment family for appliances. These are
    seeds: the family stands in at the right spot for the designer to swap/adjust.
    Each is rotated to back onto the wall the seed was laid against. Skips a
    fixture (with a note) when its family isn't loaded."""
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
        key = rebuild.key(_exchange.fixture_identity(fx))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("fixture", fx.get("id"), revit_id=_rid(kept))
            continue
        wet = fx.get("kind") in _WET_FIXTURES
        sym = plumb if wet else appl
        if sym is None:
            report.skipped("fixture", fx.get("id"), "no %s family" % ("plumbing" if wet else "appliance"))
            continue
        level = levels.get(fx.get("level", 0))
        z = level.Elevation if level else 0.0
        try:
            inst = doc.Create.NewFamilyInstance(_xyz(fx["point"], z), sym, level, st)
            message = fx.get("kind", "")
            rotated = _rotate_fixture(doc, inst, fx, z, report)
            if rotated:
                message = "%s (%s)" % (message, rotated) if message else rotated
            _made(report, "fixture", fx.get("id"), inst, message=message,
                  key=key, fingerprint=rebuild.fp(key))
        except Exception as exc:
            _logger.warning("fixture %s: %s", fx.get("id"), exc)
            report.failed("fixture", fx.get("id"), str(exc))


def _build_porches(doc, data, levels, res, report, rebuild):
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
        key = rebuild.key(_exchange.porch_identity(p))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("porch", p.get("id"), revit_id=_rid(kept))
            continue
        try:
            loop = _rect_loop(
                float(p["x"]), float(p["y"]), float(p["width"]), float(p["length"]), level0.Elevation
            )
            loops = List[DB.CurveLoop]()
            loops.Add(loop)
            floor = DB.Floor.Create(doc, loops, res.floor.Id, level0.Id)
            _made(report, "porch", p.get("id"), floor, key=key, fingerprint=rebuild.fp(key))
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


def _build_slabs(doc, data, levels, res, report, rebuild):
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
        key = rebuild.key(_exchange.slab_identity(s))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("slab", src, revit_id=_rid(kept))
            continue
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
            _made(report, "slab", src, floor, key=key, fingerprint=rebuild.fp(key))
        except Exception as exc:
            report.failed("slab", src, str(exc))


def _build_foundation(doc, data, levels, res, report, rebuild):
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
            src = "post %d" % i
            key = rebuild.key(_exchange.footing_identity(f))
            kept = rebuild.take(key)
            if kept is not None:
                report.kept("footing", src, revit_id=_rid(kept))
                continue
            if sym is None:
                report.skipped("footing", src, "no foundation family")
                continue
            try:
                inst = doc.Create.NewFamilyInstance(
                    _xyz(f["point"], level0.Elevation), sym, level0,
                    DB.Structure.StructuralType.Footing,
                )
                _made(report, "footing", src, inst, key=key, fingerprint=rebuild.fp(key))
            except Exception as exc:
                report.failed("footing", src, str(exc))

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


def _build_grids(doc, data, report, rebuild):
    grids = data.get("grids", [])
    if not grids:
        return
    for g in grids:
        label = str(g.get("label", "?"))
        key = rebuild.key(_exchange.grid_identity(g))
        kept = rebuild.take(key)
        if kept is not None:
            report.kept("grid", label, revit_id=_rid(kept))
            continue
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
            _made(report, "grid", label, grid, key=key, fingerprint=rebuild.fp(key))
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


def _build_roof(doc, data, levels, res, report, rebuild):
    """Build the roof(s) from the exchange (experimental).

    A plain gable/shed plan carries a single ``roof`` block and builds one
    footprint roof over the bounding box (as before). A richer plan carries
    ``roof["sections"]`` — one plane per footprint rectangle for an L/T/U plan
    (so the roof covers the wing, not the notch), or the three planes of a
    **monitor** (two low side sheds + a raised centre gable). Each plane builds
    as its own footprint roof with its eave edges made slope-defining; a monitor
    centre is lifted by its ``base_height`` so it sits on the clerestory. The
    clerestory stub walls that would close the gap under the raised gable are a
    manual refinement (see the extension README).
    """
    roof = data.get("roof")
    if not roof:
        return
    sections = roof.get("sections")
    if sections:
        for i, sec in enumerate(sections):
            _build_one_roof(
                doc, sec, levels, res, report, rebuild,
                identity=rebuild.key("%s|%d" % (_exchange.ROOF_IDENTITY, i)),
                source="roof %d" % i,
                base_height=float(sec.get("base_height", 0.0)),
            )
        return
    _build_one_roof(
        doc, roof, levels, res, report, rebuild,
        identity=rebuild.key(_exchange.ROOF_IDENTITY), source="roof", base_height=0.0,
    )


def _build_one_roof(doc, roof, levels, res, report, rebuild, identity, source, base_height):
    """Build one footprint-roof plane and slope its eaves (see :func:`_build_roof`).

    ``base_height`` lifts the plane above its level (a monitor centre sits on the
    clerestory); the single-roof and field-section cases pass ``0.0``, so their
    sketch elevation is unchanged.
    """
    kept = rebuild.take(identity)
    if kept is not None:
        report.kept("roof", source, revit_id=_rid(kept))
        return
    if res.roof_type is None:
        report.note("roof skipped: no roof type in project")
        report.skipped("roof", source, "no roof type")
        return
    level = levels.get(roof.get("top_level", 0))
    if level is None:
        report.skipped("roof", source, "no top level")
        return
    z = level.Elevation + float(base_height)
    try:
        arr = DB.CurveArray()
        for seg in roof.get("outline", []):
            (x1, y1), (x2, y2) = seg
            arr.Append(
                DB.Line.CreateBound(
                    DB.XYZ(float(x1), float(y1), z),
                    DB.XYZ(float(x2), float(y2), z),
                )
            )
        result = doc.Create.NewFootPrintRoof(arr, level, res.roof_type)
        roof_el = result[0] if isinstance(result, tuple) else result
        mapping = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        sloped = _slope_eaves(roof_el, mapping, roof, report) if mapping is not None else 0
        role = roof.get("role") or "gable"
        msg = "%s roof; %d eave edge(s) sloped" % (role, sloped) if sloped else (
            "flat footprint roof; %s pitch is a manual refinement" % role
        )
        _made(report, "roof", source, roof_el, message=msg,
              key=identity, fingerprint=rebuild.fp(identity))
    except Exception as exc:
        _logger.warning("roof: %s", exc)
        report.failed("roof", source, "experimental: %s" % exc)


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

    A ``replace`` re-build is **diff-based** by default (``rebuild: "diff"``):
    elements from the previous barndsl build whose exchange record is unchanged
    are *kept* — same Revit element ids, so user dimensions/tags on them
    survive — and only changed/removed elements are deleted (and changed ones
    recreated). ``rebuild: "full"`` restores the old purge-everything path.

    Walls, openings, rooms, structure and porches run in one transaction;
    **for a dry run that transaction is rolled back** (so the preview is real but
    nothing persists). Stairs run afterward in their own edit scopes.
    """
    if options is None:
        options = _report.BuildOptions()
    data = _exchange.load(data)
    candidate = getattr(options, "candidate", None)
    report = _report.BuildReport(dry_run=options.dry_run, candidate=candidate)
    report.problems = _exchange.validate(data)

    res = _resolve_resources(doc, options, report, data)
    if res.ext_wall is None or res.int_wall is None:
        report.note("no basic wall type found in this project; nothing built")
        return report

    # Every incoming record's identity key + fingerprint — stamped onto what
    # this build creates (so the *next* build can diff), and diffed against the
    # previous build's stamps below (when replacing in "diff" mode). The
    # resolved resources/options fold into the fingerprints, so a config change
    # (a different wall type, family, location line, sizing) recreates the
    # elements it affects instead of keeping ones built the old way.
    idents = _exchange.identities(data, _resource_context(res, options), candidate=candidate)
    rebuild = _Rebuild(fps=dict((k, f) for _kind, _src, k, f in idents), candidate=candidate)

    label = "Preview barndsl plan" if options.dry_run else "Build barndsl plan"
    t = DB.Transaction(doc, label)
    t.Start()
    try:
        if options.replace:
            if getattr(options, "rebuild", "diff") == "full":
                _purge_managed(doc, report, candidate)
            else:
                rebuild.kept = _diff_managed(doc, idents, options, report, candidate)
        _set_project_north(doc, data, report)
        levels = _ensure_levels(doc, data, report)
        walls = _build_walls(doc, data, levels, res, options, report, rebuild)
        _build_openings(doc, data, levels, walls, res, options, report, rebuild)
        _build_rooms(doc, data, levels, report, rebuild)
        if options.ceilings:
            _build_ceilings(doc, data, levels, res, report, rebuild)
        if options.structure:
            _build_structure(doc, data, levels, res, options, report, rebuild)
        if options.fixtures:
            _build_fixtures(doc, data, levels, res, report, rebuild)
        if options.slabs:
            _build_slabs(doc, data, levels, res, report, rebuild)
        if options.foundation:
            _build_foundation(doc, data, levels, res, report, rebuild)
        if options.porches:
            _build_porches(doc, data, levels, res, report, rebuild)
        if options.grids:
            _build_grids(doc, data, report, rebuild)
        if options.roof:
            _build_roof(doc, data, levels, res, report, rebuild)
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
            # A "doc|" key (never in a build's incoming set) so the next model
            # re-build purges it as an ordinary stale element — not with the
            # misleading "older barndsl build" legacy note. Re-created by the
            # next document() run.
            _mark(dim, key="doc|dimension|%s" % ("x" if along_x else "y"))
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
    # Keyed like the dimensions: stale (not legacy) to the next model re-build.
    _mark(marker, key="doc|elevation-marker")
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
