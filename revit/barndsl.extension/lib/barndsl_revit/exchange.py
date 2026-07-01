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
