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


def load_path(path):
    """Read and validate an exchange JSON file at ``path``."""
    with open(path, "r") as fh:
        data = json.load(fh)
    return load(data)


def loads(text):
    """Validate an exchange from a JSON string."""
    return load(json.loads(text))


def load(data):
    """Validate an already-parsed exchange dict; returns it unchanged.

    Raises :class:`ExchangeError` on anything that would make the builder choke:
    wrong schema/units, missing top-level sections, or duplicate wall ids.
    """
    if not isinstance(data, dict):
        raise ExchangeError("exchange must be a JSON object")
    schema = data.get("schema")
    if schema != SCHEMA:
        raise ExchangeError(
            "unsupported schema %r (expected %r)" % (schema, SCHEMA)
        )
    if data.get("units") != "feet":
        raise ExchangeError("unsupported units %r (expected 'feet')" % data.get("units"))
    for key in _REQUIRED_KEYS:
        if key not in data:
            raise ExchangeError("exchange is missing the %r section" % key)

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


def identities(data, context=None):
    """Every managed record's ``(kind, source, identity_key, fingerprint)``.

    One entry per element the builder would create and stamp — the incoming
    side of a diff rebuild. Kinds/sources match what the builder reports
    (stairs and levels are excluded: stairs are never purged, levels are
    reused). Level elevations are folded into each record's fingerprint so an
    edited level rebuilds what sits on it.

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

    return out
