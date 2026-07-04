# -*- coding: utf-8 -*-
"""Load and validate a ``barndsl.revit/1`` exchange document.

This module is **pure Python with no Revit dependency** — it is the seam between
the barndsl core (which *produces* the exchange, see ``barndsl.revit``) and the
Revit builder (which *consumes* it, see ``builder.py``). Keeping it Revit-free
means it imports and unit-tests under ordinary CPython, and the builder can rely
on a normalised, validated document instead of re-checking the raw JSON.

It is intentionally written for broad interpreter compatibility (no f-strings,
no ``X | None`` annotations) so it also runs under the older CPython engines some
pyRevit builds ship.
"""

import hashlib
import json

SCHEMA = "barndsl.revit/1"

#: Top-level keys the builder relies on.
_REQUIRED_KEYS = ("schema", "units", "plan", "levels", "walls", "openings", "rooms")


class ExchangeError(ValueError):
    """The document is not a usable ``barndsl.revit/1`` exchange."""


# --- units normalisation -----------------------------------------------------
#
# The exchange's canonical unit is the decimal foot (Revit's internal unit too),
# and the producer only ever emits feet, so a feet document flows straight to the
# builder. A document that declares metric is normalised to feet HERE, before the
# builder reads any coordinate, so the builder itself stays unit-unaware.
#
# The conversion is schema-driven via _UNIT_FIELDS: an unknown numeric field
# (a length added to the exchange without a table entry) raises loudly rather
# than importing an unconverted value. Angles/ratios (orientation, roof_pitch,
# pitch, slope_angle) are "skip"; areas convert by the SQUARE of the factor.
#
# *** This block is a verbatim twin of the one in src/barndsl/revit.py ***
# (this module runs under pyRevit's engine and cannot import the core). The
# _UNIT_FIELDS table below MUST match its twin field-for-field; the repo test
# tests/test_revit_units.py compares the two and fails if they drift.

#: 1 m = 1/0.3048 ft, rounded to 6 decimals (3 m -> 9.84252 ft).
FOOT_PER_METER = round(1.0 / 0.3048, 6)  # 3.28084

#: Accepted ``units`` spellings; anything else is rejected naming the value.
_FEET_UNITS = frozenset(("feet",))
_METER_UNITS = frozenset(("meters", "metres", "m"))

#: Per-record field classification (see the twin in src/barndsl/revit.py). Values:
#: "len" (x factor), "area" (x factor^2), "vol" (x factor^3), "pts" (flat coord
#: list), "rects" (list of flat coord lists), "skip" (unchanged). Container fields
#: are "skip" here and handled structurally by the walker below.
_UNIT_FIELDS = {
    "plan": {
        "name": "skip", "ceiling_height": "len", "floor_depth": "len",
        "floor_to_floor": "len", "envelope_width": "len", "envelope_length": "len",
        "wings": "rects", "orientation": "skip", "siding": "skip", "roofing": "skip",
        "roof_style": "skip", "roof_pitch": "skip", "notes": "skip",
        "accessible": "skip", "program": "skip", "frame": "skip",
        "suites": "skip", "zones": "skip",
    },
    "program": {"beds": "skip", "baths": "skip", "required": "skip", "min_area": "area"},
    "frame": {"bay": "len", "span": "len", "post": "len", "ridge": "skip"},
    "level": {"index": "skip", "name": "skip", "elevation": "len", "height": "len"},
    "wall": {
        "id": "skip", "level": "skip", "start": "pts", "end": "pts", "height": "len",
        "exterior": "skip", "thickness": "len", "profile": "skip", "apex": "pts",
        "apex_height": "len", "kind": "skip",
    },
    "opening": {
        "id": "skip", "category": "skip", "kind": "skip", "level": "skip",
        "location": "pts", "width": "len", "height": "len", "sill": "len",
        "exterior": "skip", "egress": "skip", "rooms": "skip", "host_wall": "skip",
        "swing_into": "skip", "hinge": "skip",
    },
    "room": {
        "id": "skip", "name": "skip", "type": "skip", "level": "skip", "point": "pts",
        "area": "area", "x": "len", "y": "len", "width": "len", "length": "len",
        "clear_width": "len", "clear_length": "len", "clear_area": "area",
        "ceiling_height": "len", "vaulted": "skip", "zone": "skip",
    },
    "column": {
        "point": "pts", "size": "len", "role": "skip", "level": "skip",
        "base": "len", "top": "len",
    },
    "framing": {
        "start": "pts", "end": "pts", "role": "skip", "level": "skip",
        "z": "len", "size": "len",
    },
    "area": {
        "id": "skip", "kind": "skip", "x": "len", "y": "len", "width": "len",
        "length": "len", "level": "skip", "meta": "skip",
    },
    "meta": {
        "covered": "skip", "from_level": "skip", "to_level": "skip",
        "rise": "len", "plan": "skip",
    },
    "stairplan": {
        "risers": "skip", "riser_height": "len", "tread": "len", "layout": "skip",
        "fits": "skip", "runs": "skip", "landings": "skip",
    },
    "stairrun": {"start": "pts", "end": "pts", "width": "len", "risers": "skip"},
    "landing": {"x": "len", "y": "len", "width": "len", "length": "len"},
    "slab": {"level": "skip", "x": "len", "y": "len", "width": "len", "length": "len"},
    "grid": {"label": "skip", "start": "pts", "end": "pts"},
    "roof": {
        "top_level": "skip", "style": "skip", "pitch": "skip", "rise": "len",
        "slope_angle": "skip", "gable_axis": "skip", "outline_slopes": "skip",
        "ridge": "skip", "eaves": "skip", "outline": "skip", "sections": "skip",
        "role": "skip", "base_height": "len",
    },
    "foundation": {
        "top": "len", "slab_thickness": "len", "sections": "rects", "edge": "skip",
        "footings": "skip", "concrete_yd3": "vol",
    },
    "edge": {"width": "len", "depth": "len", "segments": "skip"},
    "footing": {"point": "pts", "size": "len", "depth": "len", "role": "skip"},
    "fixture": {
        "id": "skip", "kind": "skip", "room": "skip", "level": "skip", "x": "len",
        "y": "len", "width": "len", "length": "len", "wall": "skip", "point": "pts",
        "rotation": "skip", "seed": "skip", "source_line": "skip",
    },
    "site": {"width": "len", "length": "len", "setbacks": "skip"},
    "setbacks": {"front": "len", "side": "len", "rear": "len"},
}


def _scale(v, f):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v * f
    return v


def _scale_list(v, f):
    if v is None:
        return v
    return [_scale(x, f) for x in v]


def _convert_record(rec, table, factor, where):
    if not isinstance(rec, dict):
        return
    for k in list(rec.keys()):
        kind = table.get(k)
        if kind is None:
            v = rec[k]
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                raise ExchangeError(
                    "unit conversion: unknown numeric field %r in a %s record "
                    "(the units table needs an entry for it)" % (k, where)
                )
            continue
        if kind == "len":
            rec[k] = _scale(rec[k], factor)
        elif kind == "area":
            rec[k] = _scale(rec[k], factor * factor)
        elif kind == "vol":
            rec[k] = _scale(rec[k], factor * factor * factor)
        elif kind == "pts":
            rec[k] = _scale_list(rec[k], factor)
        elif kind == "rects":
            v = rec[k]
            if v is not None:
                rec[k] = [_scale_list(x, factor) for x in v]
        # "skip": intentionally unchanged


def _convert_seg(seg, factor):
    if isinstance(seg, dict):
        if seg.get("start") is not None:
            seg["start"] = _scale_list(seg["start"], factor)
        if seg.get("end") is not None:
            seg["end"] = _scale_list(seg["end"], factor)


def _convert_outline(outline, factor):
    if not outline:
        return outline
    return [[_scale_list(pt, factor) for pt in seg] for seg in outline]


def _convert_roof(roof, factor):
    _convert_record(roof, _UNIT_FIELDS["roof"], factor, "roof")
    _convert_seg(roof.get("ridge"), factor)
    for e in roof.get("eaves", []) or []:
        _convert_seg(e, factor)
    if roof.get("outline"):
        roof["outline"] = _convert_outline(roof["outline"], factor)
    for sec in roof.get("sections", []) or []:
        _convert_roof(sec, factor)


def _convert_foundation(found, factor):
    _convert_record(found, _UNIT_FIELDS["foundation"], factor, "foundation")
    edge = found.get("edge")
    if isinstance(edge, dict):
        _convert_record(edge, _UNIT_FIELDS["edge"], factor, "foundation edge")
        if edge.get("segments"):
            edge["segments"] = _convert_outline(edge["segments"], factor)
    for ft in found.get("footings", []) or []:
        _convert_record(ft, _UNIT_FIELDS["footing"], factor, "footing")


def _convert_area(area, factor):
    _convert_record(area, _UNIT_FIELDS["area"], factor, "area")
    meta = area.get("meta")
    if isinstance(meta, dict):
        _convert_record(meta, _UNIT_FIELDS["meta"], factor, "area meta")
        sp = meta.get("plan")
        if isinstance(sp, dict):
            _convert_record(sp, _UNIT_FIELDS["stairplan"], factor, "stair plan")
            for run in sp.get("runs", []) or []:
                _convert_record(run, _UNIT_FIELDS["stairrun"], factor, "stair run")
            for land in sp.get("landings", []) or []:
                _convert_record(land, _UNIT_FIELDS["landing"], factor, "stair landing")


def _convert_document(data, factor):
    """Convert every length/area in an exchange dict in place by ``factor``."""
    plan = data.get("plan")
    if isinstance(plan, dict):
        _convert_record(plan, _UNIT_FIELDS["plan"], factor, "plan")
        prog = plan.get("program")
        if isinstance(prog, dict):
            _convert_record(prog, _UNIT_FIELDS["program"], factor, "program")
        fr = plan.get("frame")
        if isinstance(fr, dict):
            _convert_record(fr, _UNIT_FIELDS["frame"], factor, "frame")
    for lv in data.get("levels", []) or []:
        _convert_record(lv, _UNIT_FIELDS["level"], factor, "level")
    for w in data.get("walls", []) or []:
        _convert_record(w, _UNIT_FIELDS["wall"], factor, "wall")
    for o in data.get("openings", []) or []:
        _convert_record(o, _UNIT_FIELDS["opening"], factor, "opening")
    for r in data.get("rooms", []) or []:
        _convert_record(r, _UNIT_FIELDS["room"], factor, "room")
    struct = data.get("structure")
    if isinstance(struct, dict):
        for c in struct.get("columns", []) or []:
            _convert_record(c, _UNIT_FIELDS["column"], factor, "column")
        for f in struct.get("framing", []) or []:
            _convert_record(f, _UNIT_FIELDS["framing"], factor, "framing")
    for a in data.get("areas", []) or []:
        _convert_area(a, factor)
    for s in data.get("slabs", []) or []:
        _convert_record(s, _UNIT_FIELDS["slab"], factor, "slab")
    for g in data.get("grids", []) or []:
        _convert_record(g, _UNIT_FIELDS["grid"], factor, "grid")
    roof = data.get("roof")
    if isinstance(roof, dict):
        _convert_roof(roof, factor)
    found = data.get("foundation")
    if isinstance(found, dict):
        _convert_foundation(found, factor)
    for fx in data.get("fixtures", []) or []:
        _convert_record(fx, _UNIT_FIELDS["fixture"], factor, "fixture")
    site = data.get("site")
    if isinstance(site, dict):
        _convert_record(site, _UNIT_FIELDS["site"], factor, "site")
        sb = site.get("setbacks")
        if isinstance(sb, dict):
            _convert_record(sb, _UNIT_FIELDS["setbacks"], factor, "setback")
    return data


def normalize_units(data):
    """Return an exchange in feet: feet passes through untouched, metric is
    deep-copied and normalised, anything else raises :class:`ExchangeError`."""
    units = data.get("units", "feet")
    if units in _FEET_UNITS:
        return data
    if units in _METER_UNITS:
        import copy

        out = copy.deepcopy(data)
        _convert_document(out, FOOT_PER_METER)
        out["units"] = "feet"
        return out
    raise ExchangeError(
        "unsupported units %r (expected feet %s or metric %s)"
        % (units, sorted(_FEET_UNITS), sorted(_METER_UNITS))
    )


def load_path(path):
    """Read and validate an exchange JSON file at ``path``."""
    with open(path, "r") as fh:
        data = json.load(fh)
    return load(data)


def loads(text):
    """Validate an exchange from a JSON string."""
    return load(json.loads(text))


def load(data):
    """Validate an already-parsed exchange dict; return it ready for the builder.

    A ``feet`` document is returned as-is (only optional sections defaulted); a
    metric document (``meters``/``metres``/``m``) is deep-copied and normalised
    to feet, so the builder always works in feet. Raises :class:`ExchangeError`
    on anything that would make the builder choke: wrong schema, unsupported
    units, missing top-level sections, or duplicate wall ids.
    """
    if not isinstance(data, dict):
        raise ExchangeError("exchange must be a JSON object")
    schema = data.get("schema")
    if schema != SCHEMA:
        raise ExchangeError(
            "unsupported schema %r (expected %r)" % (schema, SCHEMA)
        )
    for key in _REQUIRED_KEYS:
        if key not in data:
            raise ExchangeError("exchange is missing the %r section" % key)
    # Normalise units to feet (feet passes through untouched; metric is
    # converted; anything else raises). Everything below reads feet.
    data = normalize_units(data)

    seen = set()
    for w in data["walls"]:
        wid = w.get("id")
        if wid in seen:
            raise ExchangeError("duplicate wall id %r" % wid)
        seen.add(wid)

    # Structure/areas are optional; normalise so the builder can index freely.
    data.setdefault("structure", {"columns": [], "framing": []})
    data["structure"].setdefault("columns", [])
    data["structure"].setdefault("framing", [])
    data.setdefault("areas", [])
    data.setdefault("fixtures", [])
    data.setdefault("foundation", None)
    return data


def validate(data):
    """Return a list of non-fatal problems (each a human-readable string).

    Distinct from :func:`load`'s hard errors: these are dangling references the
    builder can route around (e.g. an opening whose host wall went missing), but
    that are worth surfacing to the user before a build.
    """
    problems = []
    wall_ids = set(w.get("id") for w in data.get("walls", []))
    level_indexes = set(l.get("index") for l in data.get("levels", []))

    for o in data.get("openings", []):
        host = o.get("host_wall")
        if host is not None and host not in wall_ids:
            problems.append(
                "opening %s references missing wall %r" % (o.get("id"), host)
            )
        if host is None:
            problems.append(
                "opening %s (%s) has no host wall and will be skipped"
                % (o.get("id"), o.get("kind"))
            )
        if o.get("level") not in level_indexes:
            problems.append(
                "opening %s is on unknown level %r" % (o.get("id"), o.get("level"))
            )
        # Optional swing side (additive barndsl.revit/1 field): must name one of
        # the rooms the opening serves, or the builder can't pick a side.
        swing = o.get("swing_into")
        if swing is not None and swing not in (o.get("rooms") or []):
            problems.append(
                "opening %s swings into %r, which is not one of its rooms"
                % (o.get("id"), swing)
            )

    for w in data.get("walls", []):
        if w.get("level") not in level_indexes:
            problems.append("wall %s is on unknown level %r" % (w.get("id"), w.get("level")))

    return problems


def levels_by_index(data):
    """A dict ``{index: level_dict}`` for the document's levels."""
    return dict((l["index"], l) for l in data.get("levels", []))


def walls_by_id(data):
    """A dict ``{id: wall_dict}`` for the document's walls."""
    return dict((w["id"], w) for w in data.get("walls", []))


# --- element identity + fingerprints (for update-in-place rebuilds) ----------
#
# The builder stamps every element it creates with an **identity key** (which
# exchange record produced it) and a **fingerprint** (a stable hash of that
# record). On a rebuild it can then keep any element whose incoming record is
# byte-identical — preserving Revit element ids, so user annotations survive —
# and delete/recreate only what changed. Both halves of that diff are pure
# exchange-vs-exchange bookkeeping, so they live here (Revit-free, unit-tested).
#
# Identity keys must be **stable across re-exports of an unchanged plan** but
# must not lean on the exchange's *sequential* ids (``w0``/``o3`` renumber when
# an unrelated room moves). So keys derive from stable fields: geometry for
# walls/openings/structure, the DSL-authored ids for rooms/porches/fixtures,
# labels for grids. Volatile fields (the sequential ``id``; an opening's
# ``host_wall`` id) are likewise stripped from fingerprints — the *real* host
# dependency is carried by folding the host wall's fingerprint into the
# opening's (deleting a wall deletes its hosted instances in Revit, so an
# opening can only be kept when its host wall is kept).


# --- candidate namespacing (Build Option / Design Options workflow) ----------
#
# The agent's design() loop shortlists several scored iterations; an architect
# then chooses among them **in Revit** as native Design Options. Each candidate
# is a separate exchange built into whatever Design Option the user has active in
# the UI (elements created while an option is active are auto-assigned to it — the
# only reach the read-only ``DB.DesignOption`` API leaves us). For the diff
# rebuild to treat those option-scoped builds independently — rebuilding one
# candidate keeps its elements, building another candidate never purges the
# first's — every identity key is prefixed with the candidate's label, so a
# candidate build only ever matches (and only ever deletes) its own elements.
#
# The prefix is applied **only** when a candidate label is set; a plain build's
# keys stay byte-identical, so existing models never recreate. The delimiter is a
# control character absent from human labels and from every base identity key
# ("wall|...", "room|...", …), so two labels always name disjoint namespaces and
# a plain key never reads as a candidate key.

_CAND_SEP = "\x1f"
#: An identity key beginning with this prefix belongs to *some* candidate.
CANDIDATE_KEY_PREFIX = "candidate" + _CAND_SEP


def candidate_prefix(candidate):
    """The identity-key namespace prefix for ``candidate`` (a label string), or
    ``""`` for a plain (non-candidate) build."""
    if not candidate:
        return ""
    label = str(candidate).replace(_CAND_SEP, " ")
    return "%s%s%s" % (CANDIDATE_KEY_PREFIX, label, _CAND_SEP)


def namespaced_key(key, candidate=None):
    """Prefix an identity ``key`` with ``candidate``'s namespace.

    A no-op when ``candidate`` is falsy, so non-candidate builds produce
    byte-identical keys (and their models never needlessly recreate).
    """
    prefix = candidate_prefix(candidate)
    return prefix + key if prefix else key


def is_candidate_key(key):
    """Whether an identity key belongs to *some* candidate namespace."""
    return bool(key) and key.startswith(CANDIDATE_KEY_PREFIX)


def fingerprint(record, extra=""):
    """A stable hash of an exchange record, plus optional dependency context.

    ``extra`` folds a dependency into the hash (an opening's host-wall
    fingerprint, a record's level elevation) so a change there re-fingerprints
    the dependent record too. Deterministic for a deterministic exchange —
    ``to_revit_model`` on the same plan yields the same fingerprints.
    """
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    if extra:
        blob = blob + "|" + str(extra)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _strip(record, keys):
    """The record without its volatile keys (sequential ids that renumber)."""
    return dict((k, v) for k, v in record.items() if k not in keys)


def _pt(p):
    return "%.4f,%.4f" % (float(p[0]), float(p[1]))


def wall_identity(w):
    """Geometry-keyed: the sequential ``w3`` id renumbers when rooms change."""
    return "wall|%s|%s|%s" % (w.get("level"), _pt(w["start"]), _pt(w["end"]))


def opening_identity(o):
    return "opening|%s|%s|%s" % (o.get("category"), o.get("level"), _pt(o["location"]))


def room_identity(r):
    """Room ids are DSL-authored, stable across exports."""
    return "room|%s" % r.get("id")


def ceiling_identity(r):
    return "ceiling|%s" % r.get("id")


def column_identity(c):
    return "column|%s|%s" % (c.get("level"), _pt(c["point"]))


def framing_identity(f):
    return "framing|%s|%s|%s|%s|%.4f" % (
        f.get("role", "beam"), f.get("level"), _pt(f["start"]), _pt(f["end"]),
        float(f.get("z", 0.0)),
    )


def fixture_identity(fx):
    return "fixture|%s|%s" % (fx.get("id"), _pt(fx["point"]))


def slab_identity(s):
    return "slab|%s|%s" % (s.get("level"), _pt((s.get("x", 0.0), s.get("y", 0.0))))


def porch_identity(a):
    return "porch|%s" % a.get("id")


def grid_identity(g):
    # The label alone can collide (letters wrap A..Z on a very long frame), so
    # the start point disambiguates — a collision would let the diff stale-keep
    # the wrong grid line.
    return "grid|%s|%s" % (g.get("label"), _pt(g.get("start", (0.0, 0.0))))


def footing_identity(f):
    return "footing|%s" % _pt(f["point"])


#: The (single) roof's identity key.
ROOF_IDENTITY = "roof"


def identities(data, context=None, candidate=None):
    """Every managed record's ``(kind, source, identity_key, fingerprint)``.

    One entry per element the builder would create and stamp — the incoming
    side of a diff rebuild. Kinds/sources match what the builder reports
    (stairs and levels are excluded: stairs are never purged, levels are
    reused). Level elevations are folded into each record's fingerprint so an
    edited level rebuilds what sits on it.

    ``candidate`` (optional) namespaces every identity key under that candidate
    label (see :func:`namespaced_key`), so an option-scoped build diff-rebuilds
    independently of every other candidate. Fingerprints are unchanged by it —
    keys alone separate the namespaces — so two candidates with identical
    geometry still never collide (distinct keys) yet a plain build stays
    byte-identical.

    ``context`` (optional) maps a kind to an opaque environment string — the
    builder passes its *resolved* resources/options per kind, so a config
    change (a different wall type, family, location line, sizing) changes the
    affected fingerprints and the elements are recreated with the new
    resources. It is folded here, not post-hoc, so dependent fingerprints see
    it too: an opening's fingerprint builds on its host wall's *contextual*
    fingerprint (a wall recreated for a type change cascade-deletes its hosted
    openings, so they must re-fingerprint with it).
    """
    ctx = context or {}
    out = []
    levels = levels_by_index(data)

    def lvl_extra(idx):
        lv = levels.get(idx)
        if lv is None:
            return ""
        return "%.4f" % float(lv.get("elevation", 0.0))

    def fp_of(kind, record, extra="", sub=None):
        # A record with a declared/authored sub-kind (a plumbing wall, a fixed
        # window) reads the more specific context key when the builder provides
        # one ("wall/plumbing"), so a changed kind mapping recreates exactly the
        # matching records; everything else shares the plain per-kind context.
        add = None
        if sub:
            add = ctx.get("%s/%s" % (kind, sub))
        if add is None:
            add = ctx.get(kind)
        if add:
            extra = (extra + "|" + str(add)) if extra else str(add)
        return fingerprint(record, extra)

    wall_fp = {}
    for w in data.get("walls", []):
        fp = fp_of("wall", _strip(w, ("id",)), lvl_extra(w.get("level")), sub=w.get("kind"))
        wall_fp[w.get("id")] = fp
        out.append(("wall", w.get("id"), wall_identity(w), fp))

    for o in data.get("openings", []):
        cased = o.get("category") == "cased_opening"
        kind = "opening" if cased else (
            "window" if o.get("category") == "window" else "door"
        )
        # The host wall's (contextual) fingerprint is the dependency: a
        # recreated wall means every opening hosted on it must be recreated
        # too (Revit deletes hosted instances with their host).
        host_fp = wall_fp.get(o.get("host_wall"), "")
        fp = fp_of(kind, _strip(o, ("id", "host_wall")), host_fp, sub=o.get("kind"))
        out.append((kind, o.get("id"), opening_identity(o), fp))

    plan_ceiling = str((data.get("plan") or {}).get("ceiling_height", ""))
    for r in data.get("rooms", []):
        extra = lvl_extra(r.get("level"))
        out.append(("room", r.get("id"), room_identity(r), fp_of("room", r, extra)))
        # The ceiling is derived from the room record + the plan default height.
        out.append((
            "ceiling", r.get("id"), ceiling_identity(r),
            fp_of("ceiling", r, extra + "|" + plan_ceiling),
        ))

    structure = data.get("structure") or {}
    for i, c in enumerate(structure.get("columns", [])):
        out.append((
            "column", "post %d" % i, column_identity(c),
            fp_of("column", c, lvl_extra(c.get("level"))),
        ))
    for i, f in enumerate(structure.get("framing", [])):
        out.append((
            "framing", "%s %d" % (f.get("role", "beam"), i), framing_identity(f),
            fp_of("framing", f, lvl_extra(f.get("level"))),
        ))

    for fx in data.get("fixtures", []) or []:
        out.append((
            "fixture", fx.get("id"), fixture_identity(fx),
            fp_of("fixture", fx, lvl_extra(fx.get("level", 0))),
        ))

    for s in data.get("slabs", []) or []:
        out.append((
            "slab", "level %s" % s.get("level"), slab_identity(s),
            fp_of("slab", s, lvl_extra(s.get("level"))),
        ))

    for a in data.get("areas", []) or []:
        if a.get("kind") == "porch":
            out.append((
                "porch", a.get("id"), porch_identity(a), fp_of("porch", a, lvl_extra(0)),
            ))

    for g in data.get("grids", []) or []:
        out.append(("grid", str(g.get("label", "?")), grid_identity(g), fp_of("grid", g)))

    roof = data.get("roof")
    if roof:
        sections = roof.get("sections")
        if sections:
            # One managed roof per plane (per footprint rectangle, or the three
            # monitor planes). Keyed by index so the diff tracks each separately;
            # each plane's fingerprint hashes only its own section dict.
            for i, sec in enumerate(sections):
                out.append((
                    "roof", "roof %d" % i, "%s|%d" % (ROOF_IDENTITY, i),
                    fp_of("roof", sec, lvl_extra(sec.get("top_level", roof.get("top_level", 0)))),
                ))
        else:
            # A single bounding roof keeps the pre-sections identity + fingerprint
            # byte-for-byte, so an unchanged rectangular plan never recreates it.
            out.append((
                "roof", "roof", ROOF_IDENTITY,
                fp_of("roof", roof, lvl_extra(roof.get("top_level", 0))),
            ))

    foundation = data.get("foundation") or {}
    for i, f in enumerate(foundation.get("footings", []) or []):
        out.append((
            "footing", "post %d" % i, footing_identity(f), fp_of("footing", f, lvl_extra(0)),
        ))

    if candidate:
        prefix = candidate_prefix(candidate)
        out = [(kind, src, prefix + key, fp) for (kind, src, key, fp) in out]
    return out
