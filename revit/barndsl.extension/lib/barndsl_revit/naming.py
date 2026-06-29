# -*- coding: utf-8 -*-
"""Name → room-type and name → DSL-id heuristics — **pure Python, no Revit**.

Used when reading a Revit model back into the DSL (`builder.read_model`): Revit
rooms carry a free-text *name*, the DSL wants a *type* and a valid *identifier*.
Kept Revit-free so the heuristics are unit-tested in the repo suite, where
they're easy to tune.
"""

import re

#: Keyword → room type, tried in order. Order matters: "bath" is checked before
#: "master"/"bed" so a "Master Bath" reads as a bathroom, not a bedroom.
TYPE_KEYWORDS = (
    ("powder", "half_bath"),
    ("half bath", "half_bath"),
    ("bath", "bathroom"),
    ("kitchen", "kitchen"),
    ("dining", "dining"),
    ("great", "living"),
    ("living", "living"),
    ("family", "living"),
    ("bed", "bedroom"),
    ("master", "bedroom"),
    ("primary", "bedroom"),
    ("hall", "hallway"),
    ("corridor", "hallway"),
    ("foyer", "hallway"),
    ("closet", "closet"),
    ("pantry", "pantry"),
    ("mud", "mudroom"),
    ("laundry", "laundry"),
    ("utility", "utility"),
    ("mechanical", "utility"),
    ("office", "office"),
    ("study", "office"),
    ("den", "office"),
    ("loft", "loft"),
    ("garage", "garage"),
    ("shop", "shop"),
    ("porch", "porch"),
)


def guess_room_type(name):
    """Best-guess barndsl room type from a Revit room name (default ``other``)."""
    low = (name or "").lower()
    for kw, rtype in TYPE_KEYWORDS:
        if kw in low:
            return rtype
    return "other"


def slug_id(name, used):
    """A unique, valid DSL identifier from ``name`` (mutates ``used`` with it).

    Lowercases, collapses non-alphanumerics to ``_``, ensures it starts with a
    letter, and disambiguates collisions with a numeric suffix.
    """
    s = re.sub(r"[^a-z0-9]+", "_", (name or "room").strip().lower()).strip("_")
    if not s or not s[0].isalpha():
        s = ("r_" + s) if s else "room"
    base, i = s, 2
    while s in used:
        s = "%s_%d" % (base, i)
        i += 1
    used.add(s)
    return s
