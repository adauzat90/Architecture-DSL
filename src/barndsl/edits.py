"""Surgical textual edits to DSL source — Tier 5, direct manipulation round-tripped.

Viewport gestures (drag a room, resize it, slide a door/window along its wall) and
the graphical design panel's form controls (retype a room, rename it, add/delete a
room, opening or fixture, edit the plan header) become the *smallest possible*
change to the DSL **text**, so the source stays the single source of truth. This is
deliberately **not** :func:`barndsl.emit.emit_dsl` (full model→text regeneration):
that would flatten comments, spacing and the author's statement order. Instead each
function here compiles the source to find the one statement to touch (``rename_room``
and ``delete_room`` touch several — every line that references the room), rewrites
only the tokens that actually changed, and returns the source with every other byte
preserved. A well-formed *add* appends one grammatical line at the natural spot; a
*delete* drops the referencing lines without leaving a doubled blank.

The public surface is :func:`apply_edit` plus the :class:`Edit` / :class:`EditResult`
/ :class:`EditError` value types. :func:`opening_overlays` and :func:`iter_openings`
expose the opening enumeration (and the geometry the frontend draws the drag
handles from) so the playground payload and this engine agree on the opening keys.

Coordinate & offset conventions (see :mod:`barndsl.geometry` and the element
docstrings): rooms are placed at their south-west ``(x, y)`` corner in feet; an
opening's ``offset`` is measured **along its wall from the low-coordinate end** —
the *west* end of a horizontal (north/south) wall, the *south* end of a vertical
(east/west) wall, and the low-``y``/low-``x`` end of the shared edge for an
interior door. That is exactly the corner :func:`barndsl.geometry.opening_endpoints`
and :class:`~barndsl.elements.InteriorDoor` measure from, so the value written
into the DSL is the value the geometry consumes, unmodified.

Opening key scheme (stable, derived from how :mod:`barndsl.elements` models
openings, so the frontend and the engine compute the same string):

* interior door / cased ``open`` — ``"<room_a>~<room_b>~<i>"`` where ``i`` is the
  0-based ordinal among interior doors sharing that ordered room pair;
* exterior ``entry`` / ``door … exterior`` / ``overhead`` — ``"<room>~<wall>~<i>"``,
  ``i`` the ordinal among exterior doors on that room+wall;
* ``window`` — ``"<room>~<wall>~<i>"``, ``i`` the ordinal among windows on that
  room+wall.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .compiler import _PLACEMENT, CompileResult, _tokenize_line, compile_source
from .elements import RoomType
from .geometry import shared_edge, wall_segment

#: A valid room identifier for :class:`Edit` kinds that mint a new id
#: (``add_room``, ``rename_room``): a leading letter/underscore then word chars.
#: Deliberately stricter than the tokenizer (which splits only on separators) so a
#: rewritten id can't smuggle in a ``-`` that would re-parse as a placement anchor.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: The DSL statement heads the ``add_opening`` edit can emit.
_OPENING_STATEMENTS = ("door", "open", "window", "entry")

#: Cardinal wall words a ``window``/``entry`` opening (and ``set_fixture wall``)
#: accepts.
_SIDES = ("north", "south", "east", "west")

#: Comparison tolerance after both operands are rounded to 2 dp — anything below
#: this is treated as "already there" (a no-op that must not touch the source).
_EPS = 5e-4

#: The opening kinds the overlay can drag, and the element list each maps to.
OPENING_KINDS = ("interior", "exterior", "window")


# --- value types -------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    """One viewport gesture, ready to apply to source text.

    ``kind`` selects the operation and which other fields are read:

    * ``"move_room"`` — ``room``, ``x``, ``y`` (absolute feet, plan frame).
    * ``"resize_room"`` — ``room``, ``w``, ``l`` (feet).
    * ``"move_opening"`` — ``opening`` (one of :data:`OPENING_KINDS`), ``key``,
      ``offset`` (feet along the wall from its low-coordinate end).
    """

    kind: str
    room: str | None = None
    x: float | None = None
    y: float | None = None
    w: float | None = None
    l: float | None = None
    opening: str | None = None
    key: str | None = None
    offset: float | None = None
    #: Fixture edits: ``move_fixture`` uses ``key`` (the ``<room>~<kind>~<i>`` id)
    #: + room-local ``x``/``y``; ``add_fixture`` (a seed materialised by a drag)
    #: uses ``room``/``fkind``/``wall`` + ``x``/``y``.
    fkind: str | None = None
    wall: str | None = None
    #: --- panel (form-control) edit fields, Tier 5 graphical design panel ------
    #: ``set_room_type`` / ``add_room`` room type; ``rename_room`` target id;
    #: ``add_room`` anchor placement (``anchor`` = ``east-of`` …, ``of`` = target
    #: room) and floor ``level``.
    rtype: str | None = None
    to: str | None = None
    anchor: str | None = None
    of: str | None = None
    level: int | None = None
    #: ``add_opening`` operands: door/open ``a``/``b``, window/entry ``side``, and
    #: the opening ``width`` (openings use ``width``, not the room ``w``).
    a: str | None = None
    b: str | None = None
    side: str | None = None
    width: float | None = None
    #: ``set_opening`` clauses: ``sill`` (windows), ``into``/``hinge`` (interior
    #: door swing). ``into_set`` distinguishes an absent ``into`` from ``into:null``
    #: (the latter removes the swing clause). ``set_fixture`` rotation is ``rotate``.
    sill: float | None = None
    into: str | None = None
    into_set: bool = False
    hinge: str | None = None
    rotate: float | None = None
    #: ``set_plan``: the quoted plan name, the envelope ``env_w`` × ``env_l``, and
    #: the ``ceiling`` height — each rewritten on its own existing line.
    pname: str | None = None
    env_w: float | None = None
    env_l: float | None = None
    ceiling: float | None = None


@dataclass(frozen=True)
class EditError:
    """A refused edit — bad shape, unknown target, or unbuildable source.

    ``kind`` ∈ ``malformed`` (the edit itself is ill-formed — missing/mistyped
    fields), ``bad_value`` (a well-typed field carries an out-of-range or unknown
    value — an unknown room type, a non-positive size, an id already taken),
    ``unknown_room`` / ``unknown_opening`` (no such target in the plan),
    ``not_editable`` (the source doesn't compile, or the target has no authored
    source line — e.g. an auto-placed fixture seed). Never raised — returned inside
    :class:`EditResult` so the API stays exception-free.
    """

    kind: str
    message: str


@dataclass(frozen=True)
class EditResult:
    """The outcome of :func:`apply_edit`.

    On success ``error`` is ``None``, ``source`` is the rewritten text, ``line``
    is the 1-based line that changed (or would have) and ``changed`` says whether
    a byte actually moved (``False`` on an idempotent no-op). On failure ``error``
    is set and ``source`` is the input, returned unchanged.
    """

    source: str
    changed: bool = False
    line: int | None = None
    summary: str | None = None
    error: EditError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# --- edit construction from JSON ---------------------------------------------


def edit_from_json(obj: object) -> Edit | EditError:
    """Build an :class:`Edit` from the JSON body the server received, or an
    :class:`EditError` if the envelope is structurally wrong. Value-level checks
    (finite numbers, positive sizes) happen in :func:`apply_edit`."""
    if not isinstance(obj, dict):
        return EditError("malformed", "edit must be a JSON object")
    kind = obj.get("kind")
    if kind == "move_room":
        return Edit("move_room", room=_as_str(obj.get("room")),
                    x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))
    if kind == "resize_room":
        return Edit("resize_room", room=_as_str(obj.get("room")),
                    w=_as_num(obj.get("w")), l=_as_num(obj.get("l")))
    if kind == "move_opening":
        return Edit("move_opening", opening=_as_str(obj.get("opening")),
                    key=_as_str(obj.get("key")), offset=_as_num(obj.get("offset")))
    if kind == "move_fixture":
        return Edit("move_fixture", key=_as_str(obj.get("key")),
                    x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))
    if kind == "add_fixture":
        return Edit("add_fixture", room=_as_str(obj.get("room")),
                    fkind=_as_str(obj.get("fkind")), wall=_as_str(obj.get("wall")),
                    x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))
    if kind == "set_room_type":
        return Edit("set_room_type", room=_as_str(obj.get("room")),
                    rtype=_as_str(obj.get("type")))
    if kind == "rename_room":
        return Edit("rename_room", room=_as_str(obj.get("room")),
                    to=_as_str(obj.get("to")))
    if kind == "add_room":
        ax, ay = _as_pair(obj.get("at"))
        return Edit("add_room", room=_as_str(obj.get("id")),
                    rtype=_as_str(obj.get("type")),
                    w=_as_num(obj.get("w")), l=_as_num(obj.get("l")),
                    x=ax, y=ay,
                    anchor=_as_str(obj.get("anchor")), of=_as_str(obj.get("of")),
                    level=_as_int(obj.get("level")))
    if kind == "delete_room":
        return Edit("delete_room", room=_as_str(obj.get("room")))
    if kind == "add_opening":
        return Edit("add_opening", opening=_as_str(obj.get("opening")),
                    a=_as_str(obj.get("a")), b=_as_str(obj.get("b")),
                    room=_as_str(obj.get("room")), side=_as_str(obj.get("side")),
                    width=_as_num(obj.get("width")), offset=_as_num(obj.get("offset")))
    if kind == "delete_opening":
        return Edit("delete_opening", opening=_as_str(obj.get("opening")),
                    key=_as_str(obj.get("key")))
    if kind == "set_opening":
        return Edit("set_opening", opening=_as_str(obj.get("opening")),
                    key=_as_str(obj.get("key")),
                    width=_as_num(obj.get("width")), offset=_as_num(obj.get("offset")),
                    sill=_as_num(obj.get("sill")),
                    into=_as_str(obj.get("into")), into_set=("into" in obj),
                    hinge=_as_str(obj.get("hinge")))
    if kind == "delete_fixture":
        return Edit("delete_fixture", key=_as_str(obj.get("id")))
    if kind == "set_fixture":
        return Edit("set_fixture", key=_as_str(obj.get("id")),
                    rotate=_as_num(obj.get("rotate")), wall=_as_str(obj.get("wall")),
                    width=_as_num(obj.get("width")))
    if kind == "set_plan":
        ew, el = _as_pair(obj.get("envelope"))
        return Edit("set_plan", pname=_as_str(obj.get("name")),
                    env_w=ew, env_l=el, ceiling=_as_num(obj.get("ceiling")))
    return EditError("malformed", f"unknown edit kind {kind!r}")


def _as_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _as_num(v: object) -> float | None:
    if isinstance(v, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _as_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _as_pair(v: object) -> tuple[float | None, float | None]:
    """Parse a two-element ``[a, b]`` JSON list of numbers (an ``at``/``envelope``
    coordinate), or ``(None, None)`` if it isn't one."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return _as_num(v[0]), _as_num(v[1])
    return None, None


# --- number formatting (matches emit.py's `_n`, but 2-dp bounded) -------------


def _fmt(value: float) -> str:
    """Format feet the way the codebase does (integers bare, no float garbage).

    Mirrors :func:`barndsl.emit._n` (``f"{v:g}"``) but rounds to 2 dp first, so a
    dragged value never writes ``10.000000001`` into the source. ``-0.0`` is
    normalised to ``0``.
    """
    n = round(float(value), 2)
    if n == 0.0:
        n = 0.0  # collapse -0.0 → 0.0
    return f"{n:g}"


def _close(a: float, b: float) -> bool:
    return abs(round(a, 2) - round(b, 2)) < _EPS


def _splice(line: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply ``(start, end, text)`` replacements (0-based, end-exclusive) to
    ``line``, right-to-left so earlier offsets stay valid."""
    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        line = line[:start] + text + line[end:]
    return line


# --- opening enumeration + geometry (shared with the payload) ----------------


def iter_openings(plan) -> list[tuple[str, str, object]]:
    """Every draggable opening as ``(kind, key, element)`` in source order.

    ``kind`` is one of :data:`OPENING_KINDS`; ``key`` follows the scheme in this
    module's docstring; ``element`` is the underlying
    :class:`~barndsl.elements.InteriorDoor` / ``ExteriorDoor`` / ``Window``.
    """
    out: list[tuple[str, str, object]] = []
    seen: dict[tuple, int] = {}
    for d in plan.interior_doors:
        pair = (d.room_a, d.room_b)
        i = seen.get(pair, 0)
        seen[pair] = i + 1
        out.append(("interior", f"{d.room_a}~{d.room_b}~{i}", d))
    seen = {}
    for xd in plan.exterior_doors:
        k = (xd.room, xd.wall.value)
        i = seen.get(k, 0)
        seen[k] = i + 1
        out.append(("exterior", f"{xd.room}~{xd.wall.value}~{i}", xd))
    seen = {}
    for w in plan.windows:
        k = (w.room, w.wall.value)
        i = seen.get(k, 0)
        seen[k] = i + 1
        out.append(("window", f"{w.room}~{w.wall.value}~{i}", w))
    return out


def _opening_geom(plan, kind: str, obj) -> dict | None:
    """The wall segment + resolved offset a drag handle needs, or ``None`` when
    the opening can't be placed (non-adjacent interior door, missing room)."""
    if kind == "interior":
        a = plan.room(obj.room_a)
        b = plan.room(obj.room_b)
        if a is None or b is None:
            return None
        edge = shared_edge(a, b)
        if edge is None:
            return None
        if edge.orientation == "v":
            ax, ay, bx, by = edge.pos, edge.lo, edge.pos, edge.hi
        else:
            ax, ay, bx, by = edge.lo, edge.pos, edge.hi, edge.pos
        wall_len = edge.length
        offset = obj.offset if obj.offset is not None else max(0.0, (wall_len - obj.width) / 2.0)
        return {
            "ax": ax, "ay": ay, "bx": bx, "by": by, "width": obj.width,
            "offset": offset, "min": 0.0, "max": max(0.0, wall_len - obj.width),
            "wall": edge.orientation, "level": a.level,
        }
    room = plan.room(obj.room)
    if room is None:
        return None
    x1, y1, x2, y2 = wall_segment(room, obj.wall)
    wall_len = math.hypot(x2 - x1, y2 - y1)
    return {
        "ax": x1, "ay": y1, "bx": x2, "by": y2, "width": obj.width,
        "offset": obj.offset, "min": 0.0, "max": max(0.0, wall_len - obj.width),
        "wall": obj.wall.value, "level": room.level,
    }


def opening_overlays(plan) -> list[dict]:
    """Payload-ready opening descriptors for the edit overlay.

    Each carries its ``kind``/``key``, the wall segment endpoints ``(ax,ay)-(bx,by)``
    (offset is measured from ``(ax,ay)``), the opening ``width``, the resolved
    ``offset``, the draggable ``[min,max]`` offset range, the ``wall`` and the
    ``level``. Openings that can't be placed are dropped.

    The design panel's inspector needs the *authored* facts too, so each row also
    names its endpoints and editable clauses: interior rows carry ``a``/``b``,
    ``door`` (False for a cased passage) and the swing (``into``/``hinge``);
    exterior and window rows carry ``room``/``side``; windows add ``sill``. These
    mirror what ``set_opening`` can rewrite — the panel reads them and writes back
    through the same keys.
    """
    out: list[dict] = []
    for kind, key, obj in iter_openings(plan):
        geom = _opening_geom(plan, kind, obj)
        if geom is None:
            continue
        row = {"kind": kind, "key": key, "line": getattr(obj, "line", None), **geom}
        if kind == "interior":
            row["a"] = getattr(obj, "room_a", None)
            row["b"] = getattr(obj, "room_b", None)
            row["door"] = getattr(obj, "kind", "swing") != "cased"
            row["into"] = getattr(obj, "swing_into", None)
            row["hinge"] = getattr(obj, "hinge", None)
        else:
            row["room"] = getattr(obj, "room", None)
            row["side"] = getattr(obj, "wall").value
            if kind == "window":
                row["sill"] = getattr(obj, "sill_height", None)
        out.append(row)
    return out


# --- the edit engine ---------------------------------------------------------


def apply_edit(source: str, edit: Edit) -> EditResult:
    """Apply one :class:`Edit` to DSL ``source``, returning an :class:`EditResult`.

    Compiles ``source`` to locate the target statement, rewrites only the tokens
    that changed on that one line, and preserves every other byte — comments,
    blank lines, alignment and any inline ``# comment`` on the touched line. A
    no-op (moving/resizing to the current value) returns the source byte-identical
    with ``changed=False``; a relative placement is only converted to absolute
    when the coordinates actually change. Bad edits return a typed
    :class:`EditError`, never an exception.
    """
    shape = _validate_shape(edit)
    if shape is not None:
        return EditResult(source, error=shape)
    result = compile_source(source)
    if result.plan is None:
        return EditResult(
            source, error=EditError("not_editable", "source does not compile to a plan")
        )
    if edit.kind == "move_room":
        return _move_room(source, result, edit)
    if edit.kind == "resize_room":
        return _resize_room(source, result, edit)
    if edit.kind == "move_fixture":
        return _move_fixture(source, result, edit)
    if edit.kind == "add_fixture":
        return _add_fixture(source, result, edit)
    if edit.kind == "set_room_type":
        return _set_room_type(source, result, edit)
    if edit.kind == "rename_room":
        return _rename_room(source, result, edit)
    if edit.kind == "add_room":
        return _add_room(source, result, edit)
    if edit.kind == "delete_room":
        return _delete_room(source, result, edit)
    if edit.kind == "add_opening":
        return _add_opening(source, result, edit)
    if edit.kind == "delete_opening":
        return _delete_opening(source, result, edit)
    if edit.kind == "set_opening":
        return _set_opening(source, result, edit)
    if edit.kind == "delete_fixture":
        return _delete_fixture(source, result, edit)
    if edit.kind == "set_fixture":
        return _set_fixture(source, result, edit)
    if edit.kind == "set_plan":
        return _set_plan(source, result, edit)
    return _move_opening(source, result, edit)


def _validate_shape(edit: Edit) -> EditError | None:
    if edit.kind == "move_room":
        if not edit.room:
            return EditError("malformed", "move_room needs a room id")
        if not _finite(edit.x) or not _finite(edit.y):
            return EditError("malformed", "move_room needs finite x and y")
        return None
    if edit.kind == "resize_room":
        if not edit.room:
            return EditError("malformed", "resize_room needs a room id")
        if not _finite(edit.w) or not _finite(edit.l):
            return EditError("malformed", "resize_room needs finite w and l")
        if edit.w <= 0 or edit.l <= 0:  # type: ignore[operator]
            return EditError("malformed", "resize_room needs positive w and l")
        return None
    if edit.kind == "move_opening":
        if edit.opening not in OPENING_KINDS:
            return EditError("malformed", f"move_opening kind must be one of {OPENING_KINDS}")
        if not edit.key:
            return EditError("malformed", "move_opening needs a key")
        if not _finite(edit.offset) or edit.offset < 0:  # type: ignore[operator]
            return EditError("malformed", "move_opening needs a finite, non-negative offset")
        return None
    if edit.kind == "move_fixture":
        if not edit.key:
            return EditError("malformed", "move_fixture needs a fixture key")
        if not _finite(edit.x) or not _finite(edit.y):
            return EditError("malformed", "move_fixture needs finite x and y")
        return None
    if edit.kind == "add_fixture":
        if not edit.room or not edit.fkind:
            return EditError("malformed", "add_fixture needs a room and a kind")
        if not _finite(edit.x) or not _finite(edit.y):
            return EditError("malformed", "add_fixture needs finite x and y")
        return None
    if edit.kind == "set_room_type":
        if not edit.room:
            return EditError("malformed", "set_room_type needs a room id")
        return _validate_room_type(edit.rtype)
    if edit.kind == "rename_room":
        if not edit.room:
            return EditError("malformed", "rename_room needs a room id")
        if not edit.to:
            return EditError("malformed", "rename_room needs a target id")
        if not _IDENT_RE.match(edit.to):
            return EditError("bad_value",
                             f"{edit.to!r} is not a valid room id (letters, digits, "
                             "underscore; not starting with a digit)")
        return None
    if edit.kind == "add_room":
        if not edit.room:
            return EditError("malformed", "add_room needs an id")
        if not _IDENT_RE.match(edit.room):
            return EditError("bad_value",
                             f"{edit.room!r} is not a valid room id (letters, digits, "
                             "underscore; not starting with a digit)")
        type_err = _validate_room_type(edit.rtype)
        if type_err is not None:
            return type_err
        if not _finite(edit.w) or not _finite(edit.l):
            return EditError("malformed", "add_room needs finite w and l")
        if edit.w <= 0 or edit.l <= 0:  # type: ignore[operator]
            return EditError("bad_value", "add_room needs positive w and l")
        has_at = edit.x is not None or edit.y is not None
        if has_at:
            if not _finite(edit.x) or not _finite(edit.y):
                return EditError("malformed", "add_room `at` needs finite x and y")
        elif edit.anchor is not None:
            if edit.anchor not in _PLACEMENT:
                return EditError("bad_value", f"unknown anchor {edit.anchor!r}")
            if not edit.of:
                return EditError("malformed", "add_room anchor needs an `of` room")
        else:
            return EditError("malformed",
                             "add_room needs a placement: `at` [x,y] or `anchor`+`of`")
        if edit.level is not None and edit.level < 0:
            return EditError("bad_value", "add_room level must be >= 0")
        return None
    if edit.kind == "delete_room":
        if not edit.room:
            return EditError("malformed", "delete_room needs a room id")
        return None
    if edit.kind == "add_opening":
        if edit.opening not in _OPENING_STATEMENTS:
            return EditError("malformed",
                             f"add_opening kind must be one of {_OPENING_STATEMENTS}")
        if not _finite(edit.width) or edit.width <= 0:  # type: ignore[operator]
            return EditError("bad_value", "add_opening needs a positive width")
        if edit.offset is not None and (not _finite(edit.offset) or edit.offset < 0):
            return EditError("bad_value", "add_opening offset must be finite and >= 0")
        if edit.opening in ("door", "open"):
            if not edit.a or not edit.b:
                return EditError("malformed", f"{edit.opening} opening needs rooms a and b")
            if edit.a == edit.b:
                return EditError("bad_value", "a door/open joins two different rooms")
        else:
            if not edit.room:
                return EditError("malformed", f"{edit.opening} opening needs a room")
            if edit.side not in _SIDES:
                return EditError("bad_value", f"side must be one of {_SIDES}")
        return None
    if edit.kind == "delete_opening":
        if edit.opening not in OPENING_KINDS:
            return EditError("malformed",
                             f"delete_opening kind must be one of {OPENING_KINDS}")
        if not edit.key:
            return EditError("malformed", "delete_opening needs a key")
        return None
    if edit.kind == "set_opening":
        if edit.opening not in OPENING_KINDS:
            return EditError("malformed",
                             f"set_opening kind must be one of {OPENING_KINDS}")
        if not edit.key:
            return EditError("malformed", "set_opening needs a key")
        if not (edit.width is not None or edit.offset is not None
                or edit.into is not None or edit.into_set
                or edit.hinge is not None or edit.sill is not None):
            return EditError("malformed", "set_opening needs a property to set")
        if edit.width is not None and (not _finite(edit.width) or edit.width <= 0):
            return EditError("bad_value", "set_opening width must be positive")
        if edit.offset is not None and (not _finite(edit.offset) or edit.offset < 0):
            return EditError("bad_value", "set_opening offset must be finite and >= 0")
        if edit.sill is not None:
            if edit.opening != "window":
                return EditError("bad_value", "sill applies only to windows")
            if not _finite(edit.sill):
                return EditError("bad_value", "set_opening sill must be finite")
        if ((edit.into is not None or edit.into_set or edit.hinge is not None)
                and edit.opening != "interior"):
            return EditError("bad_value", "into/hinge apply only to interior doors")
        if edit.hinge is not None and edit.hinge not in ("near", "far"):
            return EditError("bad_value", "hinge must be 'near' or 'far'")
        return None
    if edit.kind == "delete_fixture":
        if not edit.key:
            return EditError("malformed", "delete_fixture needs a fixture id")
        return None
    if edit.kind == "set_fixture":
        if not edit.key:
            return EditError("malformed", "set_fixture needs a fixture id")
        if edit.rotate is None and edit.wall is None and edit.width is None:
            return EditError("malformed", "set_fixture needs rotate, wall, or width")
        if edit.rotate is not None and not _finite(edit.rotate):
            return EditError("bad_value", "set_fixture rotate must be finite")
        if edit.wall is not None and _fixture_wall(edit.wall) is None:
            return EditError("bad_value", "set_fixture wall must be N|S|E|W")
        if edit.width is not None and (not _finite(edit.width) or edit.width <= 0):
            return EditError("bad_value", "set_fixture width must be positive")
        return None
    if edit.kind == "set_plan":
        if edit.pname is None and edit.env_w is None and edit.env_l is None \
                and edit.ceiling is None:
            return EditError("malformed", "set_plan needs name, envelope, or ceiling")
        if edit.pname is not None and not edit.pname.strip():
            return EditError("bad_value", "plan name must be non-empty")
        if edit.env_w is not None or edit.env_l is not None:
            if not _finite(edit.env_w) or not _finite(edit.env_l):
                return EditError("bad_value", "envelope needs finite W and L")
            if edit.env_w <= 0 or edit.env_l <= 0:  # type: ignore[operator]
                return EditError("bad_value", "envelope W and L must be positive")
        if edit.ceiling is not None and (not _finite(edit.ceiling) or edit.ceiling <= 0):
            return EditError("bad_value", "ceiling must be positive")
        return None
    return EditError("malformed", f"unknown edit kind {edit.kind!r}")


def _validate_room_type(rtype: str | None) -> EditError | None:
    """Shared shape check: ``rtype`` names a real :class:`RoomType` value."""
    if not rtype:
        return EditError("malformed", "a room type is required")
    try:
        RoomType(rtype.lower())
    except ValueError:
        return EditError("bad_value", f"unknown room type {rtype!r}")
    return None


#: Single-letter/full wall words the fixture statement's ``wall`` clause accepts,
#: normalised to the ``N|S|E|W`` letter the DSL writes.
_FIXTURE_WALL_LETTER = {
    "n": "N", "north": "N", "s": "S", "south": "S",
    "e": "E", "east": "E", "w": "W", "west": "W",
}


def _fixture_wall(wall: str) -> str | None:
    """Normalise a fixture wall token (``N``/``north``/…) to its ``N|S|E|W``
    letter, or ``None`` if it isn't a wall."""
    return _FIXTURE_WALL_LETTER.get(wall.lower())


def _finite(v: float | None) -> bool:
    return v is not None and math.isfinite(v)


def _lines(source: str) -> list[str]:
    """Split ``source`` into lines aligned with the compiler's 1-based line
    numbers (which come from ``splitlines``). Rejoining with ``\\n`` reproduces
    the original, trailing newline and all, for the ``\\n``-terminated text the
    editor produces."""
    return source.split("\n")


def _room_line(result: CompileResult, room_id: str) -> tuple[int | None, EditError | None]:
    if result.plan is None or result.plan.room(room_id) is None:
        return None, EditError("unknown_room", f"no room {room_id!r} in the plan")
    line = result.room_lines.get(room_id)
    if line is None:
        return None, EditError("not_editable", f"room {room_id!r} has no source line")
    return line, None


def _room_size_index(toks: list) -> int:
    """Index of the ``size`` keyword on a ``room`` statement's token list."""
    for i in range(3, len(toks)):
        if toks[i].text.lower() == "size":
            return i
    raise ValueError("room statement without a size clause")  # unreachable: it compiled


def _move_room(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    line_no, err = _room_line(result, edit.room)  # type: ignore[arg-type]
    if err is not None:
        return EditResult(source, error=err)
    room = result.plan.room(edit.room)  # type: ignore[arg-type]
    assert room is not None and line_no is not None
    tx, ty = float(edit.x), float(edit.y)  # type: ignore[arg-type]
    if _close(tx, room.x) and _close(ty, room.y):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.room} already at {_fmt(tx)},{_fmt(ty)}")

    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    size_idx = _room_size_index(toks)
    placement = toks[3:size_idx]
    if placement and placement[0].text.lower() == "at":
        # Absolute: rewrite just the x and y numbers, leaving `at`, the comma and
        # any spacing exactly as the author wrote them.
        xt, yt = placement[1], placement[2]
        newraw = _splice(raw, [
            (xt.col - 1, xt.end_col - 1, _fmt(tx)),
            (yt.col - 1, yt.end_col - 1, _fmt(ty)),
        ])
        summary = f"{edit.room} → at {_fmt(tx)},{_fmt(ty)}"
    else:
        # Relative anchor: converting to absolute is the documented behaviour when
        # the coordinates actually change (they do — the no-op returned above).
        start = placement[0].col - 1
        end = placement[-1].end_col - 1
        newraw = _splice(raw, [(start, end, f"at {_fmt(tx)},{_fmt(ty)}")])
        summary = f"{edit.room} → at {_fmt(tx)},{_fmt(ty)} (was relative)"
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no, summary=summary)


def _resize_room(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    line_no, err = _room_line(result, edit.room)  # type: ignore[arg-type]
    if err is not None:
        return EditResult(source, error=err)
    room = result.plan.room(edit.room)  # type: ignore[arg-type]
    assert room is not None and line_no is not None
    tw, tl = float(edit.w), float(edit.l)  # type: ignore[arg-type]
    if _close(tw, room.width) and _close(tl, room.length):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.room} already {_fmt(tw)} x {_fmt(tl)}")

    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    size_idx = _room_size_index(toks)
    wt, lt = toks[size_idx + 1], toks[size_idx + 3]  # size <W> x <L>
    newraw = _splice(raw, [
        (wt.col - 1, wt.end_col - 1, _fmt(tw)),
        (lt.col - 1, lt.end_col - 1, _fmt(tl)),
    ])
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"{edit.room} → size {_fmt(tw)} x {_fmt(tl)}")


def _move_opening(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    plan = result.plan
    match = None
    for kind, key, obj in iter_openings(plan):
        if kind == edit.opening and key == edit.key:
            match = obj
            break
    if match is None:
        return EditResult(
            source, error=EditError("unknown_opening", f"no {edit.opening} opening {edit.key!r}")
        )
    line_no = getattr(match, "line", None)
    if line_no is None:
        return EditResult(
            source, error=EditError("not_editable", f"opening {edit.key!r} has no source line")
        )

    target = float(edit.offset)  # type: ignore[arg-type]
    resolved = _resolved_offset(plan, edit.opening, match)  # type: ignore[arg-type]
    if resolved is not None and _close(target, resolved):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"opening {edit.key} already at offset {_fmt(target)}")

    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    off_num = _offset_number_token(toks)
    if off_num is not None:
        newraw = _splice(raw, [(off_num.col - 1, off_num.end_col - 1, _fmt(target))])
    else:
        # No `offset` on the statement — insert one after the last token, ahead of
        # any trailing inline comment (which the tokenizer excluded).
        insert_at = max(t.end_col for t in toks) - 1
        newraw = raw[:insert_at] + f" offset {_fmt(target)}" + raw[insert_at:]
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"opening {edit.key} → offset {_fmt(target)}")


def _resolved_offset(plan, kind: str, obj) -> float | None:
    """The opening's current effective offset in feet, resolving an interior
    door's ``None`` (centred) to its geometric centre. ``None`` when it can't be
    resolved (a non-adjacent door with no explicit offset)."""
    if kind == "interior":
        if obj.offset is not None:
            return float(obj.offset)
        a = plan.room(obj.room_a)
        b = plan.room(obj.room_b)
        if a is not None and b is not None:
            edge = shared_edge(a, b)
            if edge is not None:
                return max(0.0, (edge.length - obj.width) / 2.0)
        return None
    return float(obj.offset)


def _offset_number_token(toks: list):
    """The number token following an ``offset`` keyword on a line, or ``None``."""
    for i in range(len(toks) - 1):
        if toks[i].text.lower() == "offset":
            return toks[i + 1]
    return None


# --- fixtures ----------------------------------------------------------------


def _resolved_fixture(plan, key: str):
    """The resolved :class:`~barndsl.fixtures.Fixture` with id ``key``, or ``None``.

    The id (``<room>~<kind>~<i>``) encodes its room, so we only resolve that one
    room's fixtures — the same deterministic layout the payload/render/exchange use."""
    from .fixtures import resolve_room_fixtures

    room_id = key.split("~", 1)[0]
    room = plan.room(room_id)
    if room is None:
        return None
    for f in resolve_room_fixtures(plan, room):
        if f.id == key:
            return f
    return None


def _move_fixture(source: str, result: CompileResult, edit: Edit) -> EditResult:
    """Rewrite an **explicit** fixture's ``at x,y`` (room-local) on its source line.

    A seed fixture has no source line — the frontend materialises it with an
    ``add_fixture`` edit instead, so a seed key here is ``not_editable``."""
    assert result.plan is not None
    f = _resolved_fixture(result.plan, edit.key)  # type: ignore[arg-type]
    if f is None:
        return EditResult(source, error=EditError("unknown_opening",
                          f"no fixture {edit.key!r}"))
    if f.seed or f.source_line is None:
        return EditResult(source, error=EditError("not_editable",
                          f"fixture {edit.key!r} is an auto-seed (materialise it first)"))
    line_no = f.source_line
    room = result.plan.room(edit.key.split("~", 1)[0])  # type: ignore[union-attr]
    assert room is not None
    tx, ty = float(edit.x), float(edit.y)  # type: ignore[arg-type]
    # The fixture's current room-local position (its world SW minus the room SW).
    cur_lx, cur_ly = f.x - room.x, f.y - room.y
    if _close(tx, cur_lx) and _close(ty, cur_ly):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.key} already at {_fmt(tx)},{_fmt(ty)}")

    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    at_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "at"), None)
    if at_idx is not None and at_idx + 2 < len(toks):
        xt, yt = toks[at_idx + 1], toks[at_idx + 2]
        newraw = _splice(raw, [
            (xt.col - 1, xt.end_col - 1, _fmt(tx)),
            (yt.col - 1, yt.end_col - 1, _fmt(ty)),
        ])
    else:
        # No `at` yet (an auto-placed explicit fixture): insert one right after the
        # room id — the 4th token (`fixture <kind> in <room>`).
        insert_at = toks[3].end_col - 1
        newraw = raw[:insert_at] + f" at {_fmt(tx)},{_fmt(ty)}" + raw[insert_at:]
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"{edit.key} → at {_fmt(tx)},{_fmt(ty)}")


def _add_fixture(source: str, result: CompileResult, edit: Edit) -> EditResult:
    """Materialise a dragged auto-seed: insert a new ``fixture`` line just after the
    room's ``room`` statement, carrying the dragged room-local position."""
    assert result.plan is not None
    line_no, err = _room_line(result, edit.room)  # type: ignore[arg-type]
    if err is not None:
        return EditResult(source, error=err)
    assert line_no is not None
    wall = f" wall {edit.wall}" if edit.wall else ""
    stmt = (
        f"fixture {edit.fkind} in {edit.room} "
        f"at {_fmt(float(edit.x))},{_fmt(float(edit.y))}{wall}"  # type: ignore[arg-type]
    )
    lines = _lines(source)
    lines.insert(line_no, stmt)  # after the room line (1-based line_no → index)
    return EditResult("\n".join(lines), changed=True, line=line_no + 1,
                      summary=f"placed {edit.fkind} in {edit.room}")


# --- room-reference discovery (shared by rename / delete) --------------------


def _room_ref_indices(toks: list) -> list[int]:
    """Token indices on one statement's line that name a **room id**.

    Purely positional, from the compiler's grammar (see :mod:`barndsl.compiler`),
    so a rename touches only real references — never a type token, a quoted string,
    a comment, or a prefix-collision (``bed`` must not match ``bedroom`` or a room
    named ``bed2``). Includes the room's own id on a ``room`` line (index 1, the
    definition), the two ids on ``door``/``open``/``wall`` interior lines, the
    single id on ``door``/``window``/``entry`` exterior lines, the ``in <room>``
    id on a ``fixture`` line, ``require``/``suite``/``zone`` members, and every
    ``<dir>-of <room>`` anchor on a ``room`` line's placement.
    """
    if not toks:
        return []
    head = toks[0].text.lower()
    refs: list[int] = []
    if head == "room":
        refs.append(1)  # the room being defined
        for i in range(3, len(toks)):
            if toks[i].text.lower() in _PLACEMENT and i + 1 < len(toks):
                refs.append(i + 1)
    elif head in ("door", "open", "wall"):
        if len(toks) > 3 and toks[2].text.lower() in ("-", "to"):
            refs.extend((1, 3))  # interior `a - b`
        else:
            refs.append(1)  # exterior `door <id> <wall> …`
        for i in range(len(toks) - 1):  # `into <room>` swing target (interior door)
            if toks[i].text.lower() == "into":
                refs.append(i + 1)
    elif head in ("window", "entry"):
        refs.append(1)
    elif head == "fixture":
        for i in range(len(toks) - 1):
            if toks[i].text.lower() == "in":
                refs.append(i + 1)
                break
    elif head == "require" and len(toks) > 1:
        sub = toks[1].text.lower()
        if sub in ("adjacent", "separate"):
            refs.extend(j for j in (2, 3) if j < len(toks))
        elif sub in ("exterior", "area") and len(toks) > 2:
            refs.append(2)
    elif head in ("suite", "zone"):
        refs.extend(range(2, len(toks)))  # index 1 is the suite/zone id, not a room
    return refs


def _rebuild_without(
    lines: list[str], delete: set[int], convert: dict[int, str] | None = None
) -> str:
    """Drop the 1-based line numbers in ``delete`` (rewriting any in ``convert``)
    and rejoin, collapsing a blank line a deletion pushed against another blank so
    a removal never leaves a doubled gap. Untouched blank runs survive byte-exact."""
    convert = convert or {}
    out: list[str] = []
    pending = False  # a line was just deleted → watch for a blank it collided with
    for i, raw in enumerate(lines, start=1):
        if i in delete:
            pending = True
            continue
        line = convert.get(i, raw)
        if pending and line.strip() == "" and out and out[-1].strip() == "":
            pending = False  # this blank now abuts a blank only because of the delete
            continue
        pending = False
        out.append(line)
    return "\n".join(out)


# --- room edits --------------------------------------------------------------


def _set_room_type(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    line_no, err = _room_line(result, edit.room)  # type: ignore[arg-type]
    if err is not None:
        return EditResult(source, error=err)
    room = result.plan.room(edit.room)  # type: ignore[arg-type]
    assert room is not None and line_no is not None
    new_type = RoomType(edit.rtype.lower()).value  # type: ignore[union-attr]
    if room.type.value == new_type:
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.room} already a {new_type}")
    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    tt = toks[2]  # `room <id>: <type> …`
    lines[line_no - 1] = _splice(raw, [(tt.col - 1, tt.end_col - 1, new_type)])
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"{edit.room} → {new_type}")


def _rename_room(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    old, new = edit.room, edit.to
    assert old is not None and new is not None  # _validate_shape guaranteed both
    if result.plan.room(old) is None:
        return EditResult(source, error=EditError("unknown_room",
                          f"no room {old!r} in the plan"))
    line_no = result.room_lines.get(old)
    if line_no is None:
        return EditResult(source, error=EditError("not_editable",
                          f"room {old!r} has no source line"))
    if new == old:
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{old} unchanged")
    if result.plan.room(new) is not None:
        return EditResult(source, error=EditError("bad_value",
                          f"room id {new!r} is already taken"))
    lines = _lines(source)
    touched = 0
    for idx, raw in enumerate(lines):
        toks = _tokenize_line(raw, idx + 1)
        edits = [
            (t.col - 1, t.end_col - 1, new)
            for ri in _room_ref_indices(toks)
            for t in (toks[ri],)
            if not t.quoted and t.text == old
        ]
        if edits:
            lines[idx] = _splice(raw, edits)
            touched += 1
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"renamed {old} → {new} across {touched} line(s)")


def _envelope_line(lines: list[str]) -> int | None:
    """The 1-based line of the ``envelope`` statement, or ``None``."""
    for i, raw in enumerate(lines, start=1):
        toks = _tokenize_line(raw, i)
        if toks and toks[0].text.lower() == "envelope":
            return i
    return None


def _add_room(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    plan = result.plan
    if plan.room(edit.room) is not None:  # type: ignore[arg-type]
        return EditResult(source, error=EditError("bad_value",
                          f"room id {edit.room!r} is already taken"))
    rtype = RoomType(edit.rtype.lower()).value  # type: ignore[union-attr]
    level = int(edit.level) if edit.level is not None else 0
    if edit.anchor is not None:  # relative placement — the target must exist
        if plan.room(edit.of) is None:  # type: ignore[arg-type]
            return EditResult(source, error=EditError("unknown_room",
                              f"anchor room {edit.of!r} not in the plan"))
        placement = f"{edit.anchor} {edit.of}"
    else:
        placement = f"at {_fmt(float(edit.x))},{_fmt(float(edit.y))}"  # type: ignore[arg-type]
    stmt = (f"room {edit.room}: {rtype} {placement} "
            f"size {_fmt(float(edit.w))} x {_fmt(float(edit.l))}")  # type: ignore[arg-type]
    if level:
        stmt += f" level {level}"

    lines = _lines(source)
    same_level = [result.room_lines[r.id] for r in plan.rooms
                  if r.level == level and r.id in result.room_lines]
    if same_level:
        after = max(same_level)
    elif result.room_lines:
        after = max(result.room_lines.values())
    else:
        after = _envelope_line(lines) or len(lines)
    lines.insert(after, stmt)
    return EditResult("\n".join(lines), changed=True, line=after + 1,
                      summary=f"added room {edit.room} ({rtype})")


def _delete_room(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    plan = result.plan
    rid = edit.room
    assert rid is not None  # _validate_shape guaranteed
    if plan.room(rid) is None:
        return EditResult(source, error=EditError("unknown_room",
                          f"no room {rid!r} in the plan"))
    room_line = result.room_lines.get(rid)
    if room_line is None:
        return EditResult(source, error=EditError("not_editable",
                          f"room {rid!r} has no source line"))

    # Lines to remove: the room, plus every opening/fixture that references it.
    delete: set[int] = {room_line}
    for d in plan.interior_doors:
        if rid in (d.room_a, d.room_b) and d.line is not None:
            delete.add(d.line)
    for xd in plan.exterior_doors:
        if xd.room == rid and xd.line is not None:
            delete.add(xd.line)
    for w in plan.windows:
        if w.room == rid and w.line is not None:
            delete.add(w.line)
    for pf in plan.fixtures:
        if pf.room == rid and pf.line is not None:
            delete.add(pf.line)

    # Rooms anchored to the doomed room would dangle — pin each to the absolute
    # position the compiler already resolved for it (mirrors _move_room's convert).
    lines = _lines(source)
    convert: dict[int, str] = {}
    for other in plan.rooms:
        if other.id == rid:
            continue
        oline = result.room_lines.get(other.id)
        if oline is None or oline in delete:
            continue
        toks = _tokenize_line(lines[oline - 1], oline)
        anchors = [i for i in _room_ref_indices(toks) if i != 1]
        if any(toks[i].text == rid for i in anchors):
            size_idx = _room_size_index(toks)
            placement = toks[3:size_idx]
            convert[oline] = _splice(lines[oline - 1], [
                (placement[0].col - 1, placement[-1].end_col - 1,
                 f"at {_fmt(other.x)},{_fmt(other.y)}"),
            ])

    new_source = _rebuild_without(lines, delete, convert)
    return EditResult(new_source, changed=True, line=room_line,
                      summary=f"deleted room {rid} and {len(delete) - 1} dependent line(s)")


# --- opening edits -----------------------------------------------------------


def _add_opening(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    plan = result.plan
    if edit.opening in ("door", "open"):
        for who in (edit.a, edit.b):
            if plan.room(who) is None:  # type: ignore[arg-type]
                return EditResult(source, error=EditError("unknown_room",
                                  f"no room {who!r} in the plan"))
        stmt = f"{edit.opening} {edit.a} - {edit.b} width {_fmt(float(edit.width))}"  # type: ignore[arg-type]
    else:
        if plan.room(edit.room) is None:  # type: ignore[arg-type]
            return EditResult(source, error=EditError("unknown_room",
                              f"no room {edit.room!r} in the plan"))
        stmt = f"{edit.opening} {edit.room} {edit.side} width {_fmt(float(edit.width))}"  # type: ignore[arg-type]
    if edit.offset is not None:
        stmt += f" offset {_fmt(float(edit.offset))}"

    opening_lines = [o.line for o in plan.interior_doors]
    opening_lines += [o.line for o in plan.exterior_doors]
    opening_lines += [o.line for o in plan.windows]
    present = [ln for ln in opening_lines if ln is not None]
    lines = _lines(source)
    if present:
        after = max(present)
    elif result.room_lines:
        after = max(result.room_lines.values())
    else:
        after = _envelope_line(lines) or len(lines)
    lines.insert(after, stmt)
    return EditResult("\n".join(lines), changed=True, line=after + 1,
                      summary=f"added {edit.opening} {stmt.split(None, 1)[1]}")


def _find_opening(plan, edit: Edit):
    """Resolve the (kind, key) opening for delete/set, returning (obj, line) or a
    typed :class:`EditError`."""
    for kind, key, obj in iter_openings(plan):
        if kind == edit.opening and key == edit.key:
            line_no = getattr(obj, "line", None)
            if line_no is None:
                return EditError("not_editable", f"opening {edit.key!r} has no source line")
            return obj, line_no
    return EditError("unknown_opening", f"no {edit.opening} opening {edit.key!r}")


def _delete_opening(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    found = _find_opening(result.plan, edit)
    if isinstance(found, EditError):
        return EditResult(source, error=found)
    _obj, line_no = found
    new_source = _rebuild_without(_lines(source), {line_no})
    return EditResult(new_source, changed=True, line=line_no,
                      summary=f"deleted {edit.opening} opening {edit.key}")


def _apply_clause(raw: str, line_no: int, kw: str, value: str,
                  aliases: tuple[str, ...] = ()) -> str:
    """Rewrite the token after keyword ``kw`` (or any of ``aliases``) to ``value``,
    or, when the clause is absent, append ``kw value`` before any inline comment."""
    toks = _tokenize_line(raw, line_no)
    names = (kw, *aliases)
    for i in range(len(toks) - 1):
        if toks[i].text.lower() in names:
            vt = toks[i + 1]
            return _splice(raw, [(vt.col - 1, vt.end_col - 1, value)])
    insert_at = max(t.end_col for t in toks) - 1
    return raw[:insert_at] + f" {kw} {value}" + raw[insert_at:]


def _remove_clause(raw: str, line_no: int, kw: str) -> str:
    """Delete a ``kw <value>`` pair (and the single space in front of it) from
    ``raw``; a no-op when the keyword isn't present."""
    toks = _tokenize_line(raw, line_no)
    for i in range(len(toks) - 1):
        if toks[i].text.lower() == kw:
            start = toks[i].col - 1
            end = toks[i + 1].end_col - 1
            while start > 0 and raw[start - 1] == " ":
                start -= 1
            return raw[:start] + raw[end:]
    return raw


def _set_opening(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    found = _find_opening(result.plan, edit)
    if isinstance(found, EditError):
        return EditResult(source, error=found)
    _obj, line_no = found
    lines = _lines(source)
    raw = lines[line_no - 1]
    new = raw
    if edit.width is not None:
        new = _apply_clause(new, line_no, "width", _fmt(float(edit.width)))
    if edit.offset is not None:
        new = _apply_clause(new, line_no, "offset", _fmt(float(edit.offset)))
    if edit.sill is not None:
        new = _apply_clause(new, line_no, "sill", _fmt(float(edit.sill)))
    # A non-None `into` always applies; the into_set sentinel only matters for the
    # removal case (`into:null` over JSON), so a directly-constructed Edit with
    # into="kitchen" behaves the same as one parsed from the wire.
    if edit.into is not None:
        new = _apply_clause(new, line_no, "into", edit.into)
    elif edit.into_set:  # `into:null` — drop the swing clause entirely
        new = _remove_clause(new, line_no, "into")
        new = _remove_clause(new, line_no, "hinge")
    if edit.hinge is not None:
        new = _apply_clause(new, line_no, "hinge", edit.hinge)
    if new == raw:
        return EditResult(source, changed=False, line=line_no,
                          summary=f"opening {edit.key} unchanged")
    lines[line_no - 1] = new
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"set {edit.opening} opening {edit.key}")


# --- fixture edits -----------------------------------------------------------


def _delete_fixture(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    f = _resolved_fixture(result.plan, edit.key)  # type: ignore[arg-type]
    if f is None:
        return EditResult(source, error=EditError("unknown_opening",
                          f"no fixture {edit.key!r}"))
    if f.seed or f.source_line is None:
        return EditResult(source, error=EditError("not_editable",
                          f"fixture {edit.key!r} is an auto-placed seed — seeds have no "
                          "`fixture` line to delete; author one to replace it, or delete "
                          "a fixture the room seeds by changing the room."))
    new_source = _rebuild_without(_lines(source), {f.source_line})
    return EditResult(new_source, changed=True, line=f.source_line,
                      summary=f"deleted fixture {edit.key}")


def _set_fixture(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    f = _resolved_fixture(result.plan, edit.key)  # type: ignore[arg-type]
    if f is None:
        return EditResult(source, error=EditError("unknown_opening",
                          f"no fixture {edit.key!r}"))
    room = result.plan.room(edit.key.split("~", 1)[0])  # type: ignore[union-attr]
    assert room is not None
    wall = _fixture_wall(edit.wall) if edit.wall is not None else None

    if f.seed or f.source_line is None:
        # Materialise the seed exactly like the drag path (_add_fixture): a new
        # `fixture` line at the seed's current room-local spot, then apply the props.
        lx, ly = _fmt(f.x - room.x), _fmt(f.y - room.y)
        stmt = f"fixture {f.kind} in {room.id} at {lx},{ly}"
        w = wall or (f.wall or None)
        if w:
            stmt += f" wall {w}"
        if edit.rotate is not None:
            stmt += f" rotate {_fmt(float(edit.rotate))}"
        if edit.width is not None:
            stmt += f" width {_fmt(float(edit.width))}"
        line_no = result.room_lines.get(room.id)
        if line_no is None:
            return EditResult(source, error=EditError("not_editable",
                              f"room {room.id!r} has no source line"))
        lines = _lines(source)
        lines.insert(line_no, stmt)
        return EditResult("\n".join(lines), changed=True, line=line_no + 1,
                          summary=f"materialised {f.kind} in {room.id}")

    # Authored fixture: rewrite/add the requested clauses on its own line.
    lines = _lines(source)
    raw = lines[f.source_line - 1]
    new = raw
    if wall is not None:
        new = _apply_clause(new, f.source_line, "wall", wall)
    if edit.rotate is not None:
        new = _apply_clause(new, f.source_line, "rotate",
                            _fmt(float(edit.rotate)), aliases=("rotation",))
    if edit.width is not None:
        new = _apply_clause(new, f.source_line, "width", _fmt(float(edit.width)))
    if new == raw:
        return EditResult(source, changed=False, line=f.source_line,
                          summary=f"fixture {edit.key} unchanged")
    lines[f.source_line - 1] = new
    return EditResult("\n".join(lines), changed=True, line=f.source_line,
                      summary=f"set fixture {edit.key}")


# --- plan edits --------------------------------------------------------------


def _set_plan(source: str, result: CompileResult, edit: Edit) -> EditResult:
    lines = _lines(source)

    def _head(name: str) -> int | None:
        for i, raw in enumerate(lines, start=1):
            toks = _tokenize_line(raw, i)
            if toks and toks[0].text.lower() == name:
                return i
        return None

    if edit.pname is not None:
        pl = _head("plan")
        if pl is None:
            return EditResult(source, error=EditError("not_editable",
                              "no `plan` line to rename"))
        toks = _tokenize_line(lines[pl - 1], pl)
        nt = next((t for t in toks[1:] if t.quoted), None)
        if nt is None:
            return EditResult(source, error=EditError("not_editable",
                              "the `plan` line has no quoted name to rewrite"))
        escaped = edit.pname.replace("\\", "\\\\").replace('"', '\\"')
        lines[pl - 1] = _splice(lines[pl - 1], [(nt.col - 1, nt.end_col - 1, f'"{escaped}"')])

    if edit.env_w is not None or edit.env_l is not None:
        el = _head("envelope")
        if el is None:
            return EditResult(source, error=EditError("not_editable",
                              "no `envelope` line to rewrite"))
        toks = _tokenize_line(lines[el - 1], el)  # envelope <W> x <L>
        wt, lt = toks[1], toks[3]
        lines[el - 1] = _splice(lines[el - 1], [
            (wt.col - 1, wt.end_col - 1, _fmt(float(edit.env_w))),  # type: ignore[arg-type]
            (lt.col - 1, lt.end_col - 1, _fmt(float(edit.env_l))),  # type: ignore[arg-type]
        ])

    if edit.ceiling is not None:
        cl = _head("ceiling")
        if cl is not None:
            toks = _tokenize_line(lines[cl - 1], cl)  # ceiling <H>
            ht = toks[1]
            lines[cl - 1] = _splice(lines[cl - 1], [
                (ht.col - 1, ht.end_col - 1, _fmt(float(edit.ceiling))),
            ])
        else:  # add a ceiling line right after the envelope (else after the plan)
            anchor = _head("envelope") or _head("plan")
            if anchor is None:
                return EditResult(source, error=EditError("not_editable",
                                  "nowhere to add a `ceiling` line"))
            lines.insert(anchor, f"ceiling {_fmt(float(edit.ceiling))}")

    new_source = "\n".join(lines)
    if new_source == source:
        return EditResult(source, changed=False, summary="plan unchanged")
    return EditResult(new_source, changed=True, summary="updated plan")


__all__ = [
    "Edit",
    "EditError",
    "EditResult",
    "OPENING_KINDS",
    "apply_edit",
    "edit_from_json",
    "iter_openings",
    "opening_overlays",
]
