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

from .compiler import (
    _PLACEMENT,
    CompileResult,
    _parse_ft_in,
    _tokenize_line,
    compile_source,
)
from .elements import ALARM_KINDS, LIGHT_KINDS, RoomType
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
    #: uses ``room``/``fkind``/``wall`` + ``x``/``y``. An ``along`` counter run is
    #: added with ``along`` (N|S|E|W) and optional ``run_from``/``run_to``/``depth``
    #: instead of ``x``/``y``/``wall`` (the ``fkind`` is ``counter``).
    fkind: str | None = None
    wall: str | None = None
    along: str | None = None
    run_from: float | None = None
    run_to: float | None = None
    depth: float | None = None
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
    #: Positioned-note edits (the `note "text" at x,y [level n]` statement).
    #: ``add_note`` uses ``text``/``x``/``y``/``level``; ``move_note`` uses
    #: ``index`` (the 0-based ordinal among positioned notes) + ``x``/``y``;
    #: ``set_note`` uses ``index`` + any of ``text``/``x``/``y``;
    #: ``delete_note`` uses ``index``.
    text: str | None = None
    index: int | None = None
    #: Cross-file composition (the `use` statement). ``add_use`` uses
    #: ``relpath``/``alias``/``x``/``y``/``level``; ``move_use`` ``alias``+``x``/``y``;
    #: ``set_use`` ``alias`` + any of ``level``/``mirror``/``rotate`` (7b — a
    #: ``null`` mirror or ``0`` rotate clears that flag; ``*_set`` says the key was
    #: present); ``delete_use``/``inline_use`` ``alias``. The ``alias`` names the
    #: instance to act on.
    relpath: str | None = None
    alias: str | None = None
    #: ``set_use`` transform (7b). ``mirror`` is ``"x"``/``"y"``/``None``; ``rotate``
    #: reuses the ``rotate`` field above (0/90/180/270). ``mirror_set``/``rotate_set``
    #: distinguish an absent key from an explicit clear.
    mirror: str | None = None
    mirror_set: bool = False
    rotate_set: bool = False
    #: Electrical edits (the `outlet`/`switch`/`light` statements). ``add_outlet``
    #: uses ``room``/``wall``/``offset``/``gfci``; ``add_switch`` ``room``/``wall``/
    #: ``offset``; ``add_light`` ``room``/``x``/``y``/``fkind`` (the light kind).
    #: ``delete_outlet``/``delete_switch``/``delete_light`` use ``index`` (the
    #: 0-based ordinal within that device list, as the payload lists them).
    #: ``add_alarm`` uses ``room``/``fkind`` (the alarm kind smoke|co|smoke_co) +
    #: optional ``x``/``y``; ``delete_alarm`` uses ``index``.
    gfci: bool = False
    #: ``add_room`` auto-placement: when ``True`` (and no ``at``/``anchor`` is
    #: given) the verb scans the envelope for the first free spot the new room
    #: fits, preferring one that abuts an existing room so it can get a door.
    auto: bool = False


@dataclass(frozen=True)
class EditError:
    """A refused edit — bad shape, unknown target, or unbuildable source.

    ``kind`` ∈ ``malformed`` (the edit itself is ill-formed — missing/mistyped
    fields), ``bad_value`` (a well-typed field carries an out-of-range or unknown
    value — an unknown room type, a non-positive size, an id already taken),
    ``unknown_room`` / ``unknown_opening`` / ``unknown_note`` (no such target in
    the plan),
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
    #: ``add_room`` auto-placement outcome: ``"auto"`` when a free spot was found,
    #: ``"fallback"`` when the envelope was full and the room was dropped at the
    #: origin (the UI offers to enlarge the envelope). ``None`` for every other edit
    #: and for explicit (``at``/``anchor``) placements.
    placed: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# --- edit construction from JSON ---------------------------------------------


def edit_from_json(obj: object) -> Edit | EditError:
    """Build an :class:`Edit` from the JSON body the server received."""
    if not isinstance(obj, dict):
        return EditError("malformed", "edit must be a JSON object")
    kind = obj.get("kind")
    builder = _EDIT_BUILDERS.get(kind) if isinstance(kind, str) else None
    if builder is None:
        return EditError("malformed", f"unknown edit kind {kind!r}")
    return builder(obj)


def _build_move_room(obj: dict) -> Edit:
    return Edit("move_room", room=_as_str(obj.get("room")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))


def _build_resize_room(obj: dict) -> Edit:
    return Edit("resize_room", room=_as_str(obj.get("room")), w=_as_num(obj.get("w")), l=_as_num(obj.get("l")))


def _build_move_opening(obj: dict) -> Edit:
    return Edit("move_opening", opening=_as_str(obj.get("opening")), key=_as_str(obj.get("key")), offset=_as_num(obj.get("offset")))


def _build_move_fixture(obj: dict) -> Edit:
    return Edit("move_fixture", key=_as_str(obj.get("key")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))


def _build_add_fixture(obj: dict) -> Edit:
    return Edit(
        "add_fixture",
        room=_as_str(obj.get("room")),
        fkind=_as_str(obj.get("fkind")),
        wall=_as_str(obj.get("wall")),
        x=_as_num(obj.get("x")),
        y=_as_num(obj.get("y")),
        along=_as_str(obj.get("along")),
        run_from=_as_num(obj.get("from")),
        run_to=_as_num(obj.get("to")),
        depth=_as_num(obj.get("depth")),
    )


def _build_set_room_type(obj: dict) -> Edit:
    return Edit("set_room_type", room=_as_str(obj.get("room")), rtype=_as_str(obj.get("type")))


def _build_rename_room(obj: dict) -> Edit:
    return Edit("rename_room", room=_as_str(obj.get("room")), to=_as_str(obj.get("to")))


def _build_add_room(obj: dict) -> Edit:
    ax, ay = _as_pair(obj.get("at"))
    return Edit(
        "add_room",
        room=_as_str(obj.get("id")),
        rtype=_as_str(obj.get("type")),
        w=_as_num(obj.get("w")),
        l=_as_num(obj.get("l")),
        x=ax,
        y=ay,
        anchor=_as_str(obj.get("anchor")),
        of=_as_str(obj.get("of")),
        level=_as_int(obj.get("level")),
        auto=obj.get("auto") is True,
    )


def _build_delete_room(obj: dict) -> Edit:
    return Edit("delete_room", room=_as_str(obj.get("room")))


def _build_add_opening(obj: dict) -> Edit:
    return Edit(
        "add_opening",
        opening=_as_str(obj.get("opening")),
        a=_as_str(obj.get("a")),
        b=_as_str(obj.get("b")),
        room=_as_str(obj.get("room")),
        side=_as_str(obj.get("side")),
        width=_as_num(obj.get("width")),
        offset=_as_num(obj.get("offset")),
    )


def _build_delete_opening(obj: dict) -> Edit:
    return Edit("delete_opening", opening=_as_str(obj.get("opening")), key=_as_str(obj.get("key")))


def _build_set_opening(obj: dict) -> Edit:
    return Edit(
        "set_opening",
        opening=_as_str(obj.get("opening")),
        key=_as_str(obj.get("key")),
        width=_as_num(obj.get("width")),
        offset=_as_num(obj.get("offset")),
        sill=_as_num(obj.get("sill")),
        into=_as_str(obj.get("into")),
        into_set=("into" in obj),
        hinge=_as_str(obj.get("hinge")),
    )


def _build_delete_fixture(obj: dict) -> Edit:
    return Edit("delete_fixture", key=_as_str(obj.get("id")))


def _build_set_fixture(obj: dict) -> Edit:
    return Edit(
        "set_fixture",
        key=_as_str(obj.get("id")),
        rotate=_as_num(obj.get("rotate")),
        wall=_as_str(obj.get("wall")),
        width=_as_num(obj.get("width")),
    )


def _build_set_plan(obj: dict) -> Edit:
    ew, el = _as_pair(obj.get("envelope"))
    return Edit("set_plan", pname=_as_str(obj.get("name")), env_w=ew, env_l=el, ceiling=_as_num(obj.get("ceiling")))


def _build_add_note(obj: dict) -> Edit:
    return Edit("add_note", text=_as_str(obj.get("text")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")), level=_as_int(obj.get("level")))


def _build_move_note(obj: dict) -> Edit:
    return Edit("move_note", index=_as_int(obj.get("index")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))


def _build_set_note(obj: dict) -> Edit:
    return Edit(
        "set_note",
        index=_as_int(obj.get("index")),
        text=_as_str(obj.get("text")) if "text" in obj else None,
        x=_as_num(obj.get("x")),
        y=_as_num(obj.get("y")),
    )


def _build_delete_note(obj: dict) -> Edit:
    return Edit("delete_note", index=_as_int(obj.get("index")))


def _build_add_outlet(obj: dict) -> Edit:
    return Edit("add_outlet", room=_as_str(obj.get("room")), wall=_as_str(obj.get("wall")), offset=_as_num(obj.get("offset")), gfci=bool(obj.get("gfci")))


def _build_add_switch(obj: dict) -> Edit:
    return Edit("add_switch", room=_as_str(obj.get("room")), wall=_as_str(obj.get("wall")), offset=_as_num(obj.get("offset")))


def _build_add_light(obj: dict) -> Edit:
    return Edit("add_light", room=_as_str(obj.get("room")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")), fkind=_as_str(obj.get("lkind")))


def _build_add_alarm(obj: dict) -> Edit:
    return Edit("add_alarm", room=_as_str(obj.get("room")), fkind=_as_str(obj.get("akind")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))


def _build_delete_electrical(kind: str):
    return lambda obj: Edit(kind, index=_as_int(obj.get("index")))


def _build_add_use(obj: dict) -> Edit:
    return Edit(
        "add_use",
        relpath=_as_str(obj.get("relpath")),
        alias=_as_str(obj.get("alias")),
        x=_as_num(obj.get("x")),
        y=_as_num(obj.get("y")),
        level=_as_int(obj.get("level")),
        mirror=_as_str(obj.get("mirror")),
        rotate=_as_num(obj.get("rotate")),
    )


def _build_move_use(obj: dict) -> Edit:
    return Edit("move_use", alias=_as_str(obj.get("alias")), x=_as_num(obj.get("x")), y=_as_num(obj.get("y")))


def _build_set_use(obj: dict) -> Edit:
    return Edit(
        "set_use",
        alias=_as_str(obj.get("alias")),
        level=_as_int(obj.get("level")),
        mirror=_as_str(obj.get("mirror")),
        mirror_set=("mirror" in obj),
        rotate=_as_num(obj.get("rotate")),
        rotate_set=("rotate" in obj),
    )


def _build_alias_edit(kind: str):
    return lambda obj: Edit(kind, alias=_as_str(obj.get("alias")))


_EDIT_BUILDERS = {
    "move_room": _build_move_room,
    "resize_room": _build_resize_room,
    "move_opening": _build_move_opening,
    "move_fixture": _build_move_fixture,
    "add_fixture": _build_add_fixture,
    "set_room_type": _build_set_room_type,
    "rename_room": _build_rename_room,
    "add_room": _build_add_room,
    "fit_envelope": lambda obj: Edit("fit_envelope"),
    "delete_room": _build_delete_room,
    "add_opening": _build_add_opening,
    "delete_opening": _build_delete_opening,
    "set_opening": _build_set_opening,
    "delete_fixture": _build_delete_fixture,
    "set_fixture": _build_set_fixture,
    "set_plan": _build_set_plan,
    "add_note": _build_add_note,
    "move_note": _build_move_note,
    "set_note": _build_set_note,
    "delete_note": _build_delete_note,
    "add_outlet": _build_add_outlet,
    "add_switch": _build_add_switch,
    "add_light": _build_add_light,
    "add_alarm": _build_add_alarm,
    "delete_outlet": _build_delete_electrical("delete_outlet"),
    "delete_switch": _build_delete_electrical("delete_switch"),
    "delete_light": _build_delete_electrical("delete_light"),
    "delete_alarm": _build_delete_electrical("delete_alarm"),
    "add_use": _build_add_use,
    "move_use": _build_move_use,
    "set_use": _build_set_use,
    "delete_use": _build_alias_edit("delete_use"),
    "inline_use": _build_alias_edit("inline_use"),
}

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


def _as_feet(text: str) -> float:
    """Parse a length token (decimal feet or a feet-and-inches literal) to feet,
    or 0.0 if it isn't a number."""
    try:
        return float(text)
    except ValueError:
        v = _parse_ft_in(text)
        return v if v is not None else 0.0


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


def apply_edit(source: str, edit: Edit, base_dir: str | None = None) -> EditResult:
    """Apply one :class:`Edit` to DSL ``source``, returning an :class:`EditResult`."""
    shape = _validate_shape(edit)
    if shape is not None:
        return EditResult(source, error=shape)
    result = compile_source(source, base_dir=base_dir)
    if result.plan is None:
        return EditResult(source, error=EditError("not_editable", "source does not compile to a plan"))
    handled = _apply_composition_edit(source, result, edit)
    if handled is not None:
        return handled
    member_err = _refuse_stamped(result, edit)
    if member_err is not None:
        return EditResult(source, error=member_err)
    return _apply_plan_edit(source, result, edit)


def _apply_composition_edit(source: str, result: CompileResult, edit: Edit) -> EditResult | None:
    handlers = {
        "add_use": _add_use,
        "move_use": _move_use,
        "set_use": _set_use,
        "delete_use": _delete_use,
        "inline_use": _inline_use,
    }
    handler = handlers.get(edit.kind)
    return None if handler is None else handler(source, result, edit)


def _apply_plan_edit(source: str, result: CompileResult, edit: Edit) -> EditResult:
    handlers = {
        "move_room": _move_room,
        "resize_room": _resize_room,
        "move_fixture": _move_fixture,
        "add_fixture": _add_fixture,
        "set_room_type": _set_room_type,
        "rename_room": _rename_room,
        "add_room": _add_room,
        "fit_envelope": _fit_envelope,
        "delete_room": _delete_room,
        "add_opening": _add_opening,
        "delete_opening": _delete_opening,
        "set_opening": _set_opening,
        "delete_fixture": _delete_fixture,
        "set_fixture": _set_fixture,
        "set_plan": _set_plan,
        "add_note": _add_note,
        "move_note": _move_note,
        "set_note": _set_note,
        "delete_note": _delete_note,
        "add_outlet": _add_outlet,
        "add_switch": _add_switch,
        "add_light": _add_light,
        "add_alarm": _add_alarm,
        "delete_outlet": _delete_electrical,
        "delete_switch": _delete_electrical,
        "delete_light": _delete_electrical,
        "delete_alarm": _delete_electrical,
        "move_opening": _move_opening,
    }
    handler = handlers.get(edit.kind, _move_opening)
    return handler(source, result, edit)

def _validate_shape(edit: Edit) -> EditError | None:
    validator = _SHAPE_VALIDATORS.get(edit.kind)
    if validator is None:
        return EditError("malformed", f"unknown edit kind {edit.kind!r}")
    return validator(edit)


def _validate_move_room_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "move_room needs a room id")
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "move_room needs finite x and y")
    return None


def _validate_resize_room_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "resize_room needs a room id")
    if not _finite(edit.w) or not _finite(edit.l):
        return EditError("malformed", "resize_room needs finite w and l")
    if edit.w <= 0 or edit.l <= 0:  # type: ignore[operator]
        return EditError("malformed", "resize_room needs positive w and l")
    return None


def _validate_move_opening_shape(edit: Edit) -> EditError | None:
    if edit.opening not in OPENING_KINDS:
        return EditError("malformed", f"move_opening kind must be one of {OPENING_KINDS}")
    if not edit.key:
        return EditError("malformed", "move_opening needs a key")
    if not _finite(edit.offset) or edit.offset < 0:  # type: ignore[operator]
        return EditError("malformed", "move_opening needs a finite, non-negative offset")
    return None


def _validate_move_fixture_shape(edit: Edit) -> EditError | None:
    if not edit.key:
        return EditError("malformed", "move_fixture needs a fixture key")
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "move_fixture needs finite x and y")
    return None


def _validate_add_fixture_shape(edit: Edit) -> EditError | None:
    if not edit.room or not edit.fkind:
        return EditError("malformed", "add_fixture needs a room and a kind")
    if edit.along is None:
        if not _finite(edit.x) or not _finite(edit.y):
            return EditError("malformed", "add_fixture needs finite x and y")
        return None
    if _fixture_wall(edit.along) is None:
        return EditError("bad_value", "add_fixture along must be N|S|E|W")
    if (edit.run_from is None) != (edit.run_to is None):
        return EditError("malformed", "add_fixture needs both `from` and `to`")
    if edit.run_from is not None and (
        not _finite(edit.run_from) or not _finite(edit.run_to) or edit.run_to <= edit.run_from  # type: ignore[operator]
    ):
        return EditError("malformed", "add_fixture `to` must be past `from`")
    return None


def _validate_set_room_type_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "set_room_type needs a room id")
    return _validate_room_type(edit.rtype)


def _validate_rename_room_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "rename_room needs a room id")
    if not edit.to:
        return EditError("malformed", "rename_room needs a target id")
    return _validate_identifier(edit.to, "room id")


def _validate_add_room_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "add_room needs an id")
    id_err = _validate_identifier(edit.room, "room id")
    if id_err is not None:
        return id_err
    type_err = _validate_room_type(edit.rtype)
    if type_err is not None:
        return type_err
    size_err = _validate_positive_size(edit.w, edit.l, "add_room")
    if size_err is not None:
        return size_err
    placement_err = _validate_add_room_placement(edit)
    if placement_err is not None:
        return placement_err
    if edit.level is not None and edit.level < 0:
        return EditError("bad_value", "add_room level must be >= 0")
    return None


def _validate_positive_size(w: float | None, l: float | None, label: str) -> EditError | None:
    if not _finite(w) or not _finite(l):
        return EditError("malformed", f"{label} needs finite w and l")
    if w <= 0 or l <= 0:  # type: ignore[operator]
        return EditError("bad_value", f"{label} needs positive w and l")
    return None


def _validate_add_room_placement(edit: Edit) -> EditError | None:
    has_at = edit.x is not None or edit.y is not None
    if has_at:
        if not _finite(edit.x) or not _finite(edit.y):
            return EditError("malformed", "add_room `at` needs finite x and y")
        return None
    if edit.anchor is not None:
        if edit.anchor not in _PLACEMENT:
            return EditError("bad_value", f"unknown anchor {edit.anchor!r}")
        if not edit.of:
            return EditError("malformed", "add_room anchor needs an `of` room")
        return None
    if edit.auto:
        return None
    return EditError("malformed", "add_room needs a placement: `at` [x,y], `anchor`+`of`, or `auto`")


def _validate_identifier(value: str, label: str) -> EditError | None:
    if _IDENT_RE.match(value):
        return None
    return EditError(
        "bad_value",
        f"{value!r} is not a valid {label} (letters, digits, underscore; not starting with a digit)",
    )


def _validate_delete_room_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "delete_room needs a room id")
    return None


def _validate_add_opening_shape(edit: Edit) -> EditError | None:
    if edit.opening not in _OPENING_STATEMENTS:
        return EditError("malformed", f"add_opening kind must be one of {_OPENING_STATEMENTS}")
    width_err = _validate_positive_width(edit.width, "add_opening")
    if width_err is not None:
        return width_err
    if edit.offset is not None and (not _finite(edit.offset) or edit.offset < 0):
        return EditError("bad_value", "add_opening offset must be finite and >= 0")
    return _validate_opening_endpoints(edit)


def _validate_opening_endpoints(edit: Edit) -> EditError | None:
    if edit.opening in ("door", "open"):
        if not edit.a or not edit.b:
            return EditError("malformed", f"{edit.opening} opening needs rooms a and b")
        if edit.a == edit.b:
            return EditError("bad_value", "a door/open joins two different rooms")
        return None
    if not edit.room:
        return EditError("malformed", f"{edit.opening} opening needs a room")
    if edit.side not in _SIDES:
        return EditError("bad_value", f"side must be one of {_SIDES}")
    return None


def _validate_delete_opening_shape(edit: Edit) -> EditError | None:
    if edit.opening not in OPENING_KINDS:
        return EditError("malformed", f"delete_opening kind must be one of {OPENING_KINDS}")
    if not edit.key:
        return EditError("malformed", "delete_opening needs a key")
    return None


def _validate_set_opening_shape(edit: Edit) -> EditError | None:
    base_err = _validate_set_opening_base(edit)
    if base_err is not None:
        return base_err
    size_err = _validate_set_opening_numbers(edit)
    if size_err is not None:
        return size_err
    return _validate_set_opening_door_fields(edit)


def _validate_set_opening_base(edit: Edit) -> EditError | None:
    if edit.opening not in OPENING_KINDS:
        return EditError("malformed", f"set_opening kind must be one of {OPENING_KINDS}")
    if not edit.key:
        return EditError("malformed", "set_opening needs a key")
    if not (
        edit.width is not None or edit.offset is not None or edit.into is not None
        or edit.into_set or edit.hinge is not None or edit.sill is not None
    ):
        return EditError("malformed", "set_opening needs a property to set")
    return None


def _validate_set_opening_numbers(edit: Edit) -> EditError | None:
    if edit.width is not None and (not _finite(edit.width) or edit.width <= 0):
        return EditError("bad_value", "set_opening width must be positive")
    if edit.offset is not None and (not _finite(edit.offset) or edit.offset < 0):
        return EditError("bad_value", "set_opening offset must be finite and >= 0")
    if edit.sill is None:
        return None
    if edit.opening != "window":
        return EditError("bad_value", "sill applies only to windows")
    if not _finite(edit.sill):
        return EditError("bad_value", "set_opening sill must be finite")
    return None


def _validate_set_opening_door_fields(edit: Edit) -> EditError | None:
    if (edit.into is not None or edit.into_set or edit.hinge is not None) and edit.opening != "interior":
        return EditError("bad_value", "into/hinge apply only to interior doors")
    if edit.hinge is not None and edit.hinge not in ("near", "far"):
        return EditError("bad_value", "hinge must be 'near' or 'far'")
    return None


def _validate_delete_fixture_shape(edit: Edit) -> EditError | None:
    if not edit.key:
        return EditError("malformed", "delete_fixture needs a fixture id")
    return None


def _validate_set_fixture_shape(edit: Edit) -> EditError | None:
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


def _validate_set_plan_shape(edit: Edit) -> EditError | None:
    if edit.pname is None and edit.env_w is None and edit.env_l is None and edit.ceiling is None:
        return EditError("malformed", "set_plan needs name, envelope, or ceiling")
    if edit.pname is not None and not edit.pname.strip():
        return EditError("bad_value", "plan name must be non-empty")
    env_err = _validate_set_plan_envelope(edit)
    if env_err is not None:
        return env_err
    if edit.ceiling is not None and (not _finite(edit.ceiling) or edit.ceiling <= 0):
        return EditError("bad_value", "ceiling must be positive")
    return None


def _validate_set_plan_envelope(edit: Edit) -> EditError | None:
    if edit.env_w is None and edit.env_l is None:
        return None
    if not _finite(edit.env_w) or not _finite(edit.env_l):
        return EditError("bad_value", "envelope needs finite W and L")
    if edit.env_w <= 0 or edit.env_l <= 0:  # type: ignore[operator]
        return EditError("bad_value", "envelope W and L must be positive")
    return None


def _validate_add_note_shape(edit: Edit) -> EditError | None:
    if edit.text is None or not edit.text.strip():
        return EditError("malformed", "add_note needs a non-empty text")
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "add_note needs finite x and y")
    if edit.level is not None and edit.level < 0:
        return EditError("bad_value", "add_note level must be >= 0")
    return None


def _validate_move_note_shape(edit: Edit) -> EditError | None:
    index_err = _validate_note_index(edit, "move_note")
    if index_err is not None:
        return index_err
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "move_note needs finite x and y")
    return None


def _validate_set_note_shape(edit: Edit) -> EditError | None:
    index_err = _validate_note_index(edit, "set_note")
    if index_err is not None:
        return index_err
    if edit.text is None and edit.x is None and edit.y is None:
        return EditError("malformed", "set_note needs text, x, or y")
    if edit.text is not None and not edit.text.strip():
        return EditError("bad_value", "note text must be non-empty")
    if edit.x is not None and not _finite(edit.x):
        return EditError("bad_value", "set_note x must be finite")
    if edit.y is not None and not _finite(edit.y):
        return EditError("bad_value", "set_note y must be finite")
    return None


def _validate_delete_note_shape(edit: Edit) -> EditError | None:
    return _validate_note_index(edit, "delete_note")


def _validate_note_index(edit: Edit, kind: str) -> EditError | None:
    if edit.index is None or edit.index < 0:
        return EditError("malformed", f"{kind} needs a note index >= 0")
    return None


def _validate_add_device_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", f"{edit.kind} needs a room")
    if not edit.wall or _fixture_wall(edit.wall) is None:
        return EditError("bad_value", f"{edit.kind} wall must be N|S|E|W")
    if edit.offset is not None and (not _finite(edit.offset) or edit.offset < 0):
        return EditError("bad_value", f"{edit.kind} offset must be finite and >= 0")
    return None


def _validate_add_light_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "add_light needs a room")
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "add_light needs finite x and y")
    if edit.fkind is not None and edit.fkind.lower() not in LIGHT_KINDS:
        return EditError("bad_value", f"add_light kind must be one of {', '.join(LIGHT_KINDS)}")
    return None


def _validate_add_alarm_shape(edit: Edit) -> EditError | None:
    if not edit.room:
        return EditError("malformed", "add_alarm needs a room")
    kind = (edit.fkind or "smoke").lower()
    if kind not in ALARM_KINDS:
        return EditError("bad_value", f"add_alarm kind must be one of {', '.join(ALARM_KINDS)}")
    if edit.x is not None and (not _finite(edit.x) or not _finite(edit.y)):
        return EditError("malformed", "add_alarm x needs a matching finite y")
    return None


def _validate_delete_electrical_shape(edit: Edit) -> EditError | None:
    if edit.index is None or edit.index < 0:
        return EditError("malformed", f"{edit.kind} needs an index >= 0")
    return None


def _validate_add_use_shape(edit: Edit) -> EditError | None:
    if not edit.relpath:
        return EditError("malformed", "add_use needs a part path")
    if not edit.alias or not _IDENT_RE.match(edit.alias):
        return EditError("bad_value", "add_use needs a valid alias (a plain identifier)")
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "add_use needs finite x and y")
    if edit.level is not None and edit.level < 0:
        return EditError("bad_value", "add_use level must be >= 0")
    return None


def _validate_move_use_shape(edit: Edit) -> EditError | None:
    if not edit.alias:
        return EditError("malformed", "move_use needs an alias")
    if not _finite(edit.x) or not _finite(edit.y):
        return EditError("malformed", "move_use needs finite x and y")
    return None


def _validate_set_use_shape(edit: Edit) -> EditError | None:
    if not edit.alias:
        return EditError("malformed", "set_use needs an alias")
    if edit.level is not None and edit.level < 0:
        return EditError("malformed", "set_use level must be >= 0")
    if edit.level is None and not edit.mirror_set and not edit.rotate_set:
        return EditError("malformed", "set_use needs a level, mirror or rotate")
    return None


def _validate_alias_edit_shape(edit: Edit) -> EditError | None:
    if not edit.alias:
        return EditError("malformed", f"{edit.kind} needs an alias")
    return None


def _validate_positive_width(width: float | None, kind: str) -> EditError | None:
    if not _finite(width) or width <= 0:  # type: ignore[operator]
        return EditError("bad_value", f"{kind} needs a positive width")
    return None


_SHAPE_VALIDATORS = {
    "move_room": _validate_move_room_shape,
    "resize_room": _validate_resize_room_shape,
    "move_opening": _validate_move_opening_shape,
    "move_fixture": _validate_move_fixture_shape,
    "add_fixture": _validate_add_fixture_shape,
    "set_room_type": _validate_set_room_type_shape,
    "rename_room": _validate_rename_room_shape,
    "add_room": _validate_add_room_shape,
    "fit_envelope": lambda edit: None,
    "delete_room": _validate_delete_room_shape,
    "add_opening": _validate_add_opening_shape,
    "delete_opening": _validate_delete_opening_shape,
    "set_opening": _validate_set_opening_shape,
    "delete_fixture": _validate_delete_fixture_shape,
    "set_fixture": _validate_set_fixture_shape,
    "set_plan": _validate_set_plan_shape,
    "add_note": _validate_add_note_shape,
    "move_note": _validate_move_note_shape,
    "set_note": _validate_set_note_shape,
    "delete_note": _validate_delete_note_shape,
    "add_outlet": _validate_add_device_shape,
    "add_switch": _validate_add_device_shape,
    "add_light": _validate_add_light_shape,
    "add_alarm": _validate_add_alarm_shape,
    "delete_outlet": _validate_delete_electrical_shape,
    "delete_switch": _validate_delete_electrical_shape,
    "delete_light": _validate_delete_electrical_shape,
    "delete_alarm": _validate_delete_electrical_shape,
    "add_use": _validate_add_use_shape,
    "move_use": _validate_move_use_shape,
    "set_use": _validate_set_use_shape,
    "delete_use": _validate_alias_edit_shape,
    "inline_use": _validate_alias_edit_shape,
}

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
    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)

    along_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "along"), None)
    if along_idx is not None:
        # An `along` counter run: dragging slides it ALONG its wall (the sugar is
        # kept) — the perpendicular drag component is ignored, and the run keeps its
        # length. A full-wall run gains a `from`/`to`; a partial run's span shifts.
        return _slide_along_counter(source, lines, toks, line_no, room, f, along_idx, tx, ty)

    # The fixture's current room-local position (its world SW minus the room SW).
    cur_lx, cur_ly = f.x - room.x, f.y - room.y
    if _close(tx, cur_lx) and _close(ty, cur_ly):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.key} already at {_fmt(tx)},{_fmt(ty)}")

    at_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "at"), None)
    if at_idx is not None and at_idx + 2 < len(toks):
        xt, yt = toks[at_idx + 1], toks[at_idx + 2]
        newraw = _splice(raw, [
            (xt.col - 1, xt.end_col - 1, _fmt(tx)),
            (yt.col - 1, yt.end_col - 1, _fmt(ty)),
        ])
    else:
        # No `at` yet (an auto-placed or wall-pinned explicit fixture): insert one
        # right after the room id — the 4th token (`fixture <kind> in <room>`) —
        # and drop any `offset <n>` clause, since `at` and `offset` are mutually
        # exclusive position pins.
        splices = [(toks[3].end_col - 1, toks[3].end_col - 1,
                    f" at {_fmt(tx)},{_fmt(ty)}")]
        off_idx = next(
            (i for i, t in enumerate(toks) if t.text.lower() == "offset"), None
        )
        if off_idx is not None and off_idx + 1 < len(toks):
            splices.append(
                (toks[off_idx].col - 1, toks[off_idx + 1].end_col - 1, "")
            )
        newraw = _splice(raw, splices)
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"{edit.key} → at {_fmt(tx)},{_fmt(ty)}")


def _slide_along_counter(source, lines, toks, line_no, room, f, along_idx, tx, ty):
    """Slide an ``along`` counter run along its wall to the dragged position, keeping
    the run length and the ``along`` sugar. Rewrites (or inserts) the ``from``/``to``
    span on the run's own line — a single, one-undo edit."""
    horiz = f.wall in ("S", "N")
    run_len = f.width if horiz else f.length
    wall_len = room.width if horiz else room.length
    new_from = tx if horiz else ty
    new_from = max(0.0, min(new_from, max(0.0, wall_len - run_len)))
    new_to = new_from + run_len
    cur_from = (f.x - room.x) if horiz else (f.y - room.y)
    if _close(new_from, cur_from):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{f.id} already along {f.wall} at {_fmt(new_from)}")
    raw = lines[line_no - 1]
    from_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "from"), None)
    to_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "to"), None)
    if from_idx is not None and to_idx is not None and from_idx + 1 < len(toks) \
            and to_idx + 1 < len(toks):
        ft, tt = toks[from_idx + 1], toks[to_idx + 1]
        newraw = _splice(raw, [
            (ft.col - 1, ft.end_col - 1, _fmt(new_from)),
            (tt.col - 1, tt.end_col - 1, _fmt(new_to)),
        ])
    else:
        # A full-wall run: insert `from A to B` right after the `along <wall>` token.
        wall_tok = toks[along_idx + 1]
        ins = wall_tok.end_col - 1
        newraw = raw[:ins] + f" from {_fmt(new_from)} to {_fmt(new_to)}" + raw[ins:]
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"{f.id} → along {f.wall} from {_fmt(new_from)} to {_fmt(new_to)}")


def _add_fixture(source: str, result: CompileResult, edit: Edit) -> EditResult:
    """Materialise a dragged auto-seed: insert a new ``fixture`` line just after the
    room's ``room`` statement, carrying the dragged room-local position."""
    assert result.plan is not None
    line_no, err = _room_line(result, edit.room)  # type: ignore[arg-type]
    if err is not None:
        return EditResult(source, error=err)
    assert line_no is not None
    if edit.along is not None:  # an `along` counter run
        stmt = f"fixture {edit.fkind} in {edit.room} along {_fixture_wall(edit.along)}"
        if edit.run_from is not None and edit.run_to is not None:
            stmt += f" from {_fmt(float(edit.run_from))} to {_fmt(float(edit.run_to))}"
        if edit.depth is not None:
            stmt += f" depth {_fmt(float(edit.depth))}"
    else:
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
    """Token indices on one statement's line that name a **room id**."""
    if not toks:
        return []
    finder = _ROOM_REF_FINDERS.get(toks[0].text.lower())
    return [] if finder is None else finder(toks)


def _room_statement_refs(toks: list) -> list[int]:
    refs = [1]  # the room being defined
    refs.extend(i + 1 for i in range(3, len(toks)) if toks[i].text.lower() in _PLACEMENT and i + 1 < len(toks))
    return refs


def _opening_statement_refs(toks: list) -> list[int]:
    refs = [1, 3] if len(toks) > 3 and toks[2].text.lower() in ("-", "to") else [1]
    refs.extend(i + 1 for i in range(len(toks) - 1) if toks[i].text.lower() == "into")
    return refs


def _fixture_statement_refs(toks: list) -> list[int]:
    for i in range(len(toks) - 1):
        if toks[i].text.lower() == "in":
            return [i + 1]
    return []


def _require_statement_refs(toks: list) -> list[int]:
    if len(toks) <= 1:
        return []
    sub = toks[1].text.lower()
    if sub in ("adjacent", "separate"):
        return [j for j in (2, 3) if j < len(toks)]
    if sub in ("exterior", "area") and len(toks) > 2:
        return [2]
    return []


def _members_statement_refs(toks: list) -> list[int]:
    return list(range(2, len(toks)))  # index 1 is the suite/zone id, not a room


_ROOM_REF_FINDERS = {
    "room": _room_statement_refs,
    "door": _opening_statement_refs,
    "open": _opening_statement_refs,
    "wall": _opening_statement_refs,
    "window": lambda toks: [1],
    "entry": lambda toks: [1],
    "fixture": _fixture_statement_refs,
    "require": _require_statement_refs,
    "suite": _members_statement_refs,
    "zone": _members_statement_refs,
}


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
        return EditResult(source, error=EditError("bad_value", f"room id {edit.room!r} is already taken"))
    level = int(edit.level) if edit.level is not None else 0
    placement = _new_room_placement(plan, edit, level)
    if isinstance(placement, EditError):
        return EditResult(source, error=placement)
    stmt, placed, rtype = _new_room_statement(edit, placement)
    lines = _lines(source)
    after = _new_room_insert_after(result, plan, lines, level)
    lines.insert(after, stmt)
    return EditResult(
        "\n".join(lines), changed=True, line=after + 1,
        summary=f"added room {edit.room} ({rtype})", placed=placed,
    )


def _new_room_placement(plan, edit: Edit, level: int) -> tuple[str, float, float, str | None] | EditError:
    w, l = float(edit.w), float(edit.l)  # type: ignore[arg-type]
    if edit.anchor is not None:
        if plan.room(edit.of) is None:
            return EditError("unknown_room", f"anchor room {edit.of!r} not in the plan")
        return f"{edit.anchor} {edit.of}", w, l, None
    if edit.x is not None or edit.y is not None:
        return f"at {_fmt(float(edit.x))},{_fmt(float(edit.y))}", w, l, None  # type: ignore[arg-type]
    return _auto_room_placement(plan, level, w, l)


def _auto_room_placement(plan, level: int, w: float, l: float) -> tuple[str, float, float, str]:
    spot = _free_spot(plan, level, w, l)
    if spot is None and (w > 8.0 or l > 8.0):
        sw, sl = min(w, 8.0), min(l, 8.0)
        shrunk = _free_spot(plan, level, sw, sl)
        if shrunk is not None:
            spot, w, l = shrunk, sw, sl
    placed = "auto"
    if spot is None:
        spot = (0.0, 0.0)
        placed = "fallback"
    return f"at {_fmt(spot[0])},{_fmt(spot[1])}", w, l, placed


def _new_room_statement(edit: Edit, placement: tuple[str, float, float, str | None]) -> tuple[str, str | None, str]:
    placement_text, w, l, placed = placement
    rtype = RoomType(edit.rtype.lower()).value  # type: ignore[union-attr]
    level = int(edit.level) if edit.level is not None else 0
    stmt = f"room {edit.room}: {rtype} {placement_text} size {_fmt(w)} x {_fmt(l)}"
    if level:
        stmt += f" level {level}"
    return stmt, placed, rtype


def _new_room_insert_after(result: CompileResult, plan, lines: list[str], level: int) -> int:
    same_level = [result.room_lines[r.id] for r in plan.rooms if r.level == level and r.id in result.room_lines]
    if same_level:
        return max(same_level)
    if result.room_lines:
        return max(result.room_lines.values())
    return _envelope_line(lines) or len(lines)


def _rects_overlap(ax: float, ay: float, aw: float, al: float, r) -> bool:
    """True if the ``aw×al`` rect at ``(ax, ay)`` overlaps room ``r`` (>tol area)."""
    return (ax < r.x2 - 1e-6 and r.x < ax + aw - 1e-6
            and ay < r.y2 - 1e-6 and r.y < ay + al - 1e-6)


def _rects_abut(ax: float, ay: float, aw: float, al: float, r) -> bool:
    """True if the ``aw×al`` rect at ``(ax, ay)`` shares a wall segment with ``r``
    (a touching edge with positive overlap along it) — the tell of a spot that can
    later carry a door."""
    ax2, ay2 = ax + aw, ay + al
    y_over = min(ay2, r.y2) - max(ay, r.y) > 1e-6
    x_over = min(ax2, r.x2) - max(ax, r.x) > 1e-6
    vert = (abs(ax2 - r.x) < 1e-6 or abs(ax - r.x2) < 1e-6) and y_over
    horiz = (abs(ay2 - r.y) < 1e-6 or abs(ay - r.y2) < 1e-6) and x_over
    return vert or horiz


def _free_spot(plan, level: int, w: float, l: float) -> tuple[float, float] | None:
    """First envelope spot (1-ft grid) a ``w×l`` room fits without overlapping any
    same-level room, preferring a spot that abuts one. ``None`` if none fits.

    Deterministic row-major scan from the origin, so the pick is reproducible and
    naturally lands beside what's already placed."""
    env_w = float(plan.envelope_width)
    env_l = float(plan.envelope_length)
    if w > env_w + 1e-6 or l > env_l + 1e-6 or env_w <= 0 or env_l <= 0:
        return None
    rooms = [r for r in plan.rooms if r.level == level]
    max_x = int(math.floor(env_w - w + 1e-6))
    max_y = int(math.floor(env_l - l + 1e-6))
    first: tuple[float, float] | None = None
    for gy in range(0, max_y + 1):
        y = float(gy)
        for gx in range(0, max_x + 1):
            x = float(gx)
            if any(_rects_overlap(x, y, w, l, r) for r in rooms):
                continue
            if first is None:
                first = (x, y)
            if not rooms or any(_rects_abut(x, y, w, l, r) for r in rooms):
                return (x, y)  # prefers an abutting spot; first-fit when placed alone
    return first


def _fit_envelope(source: str, result: CompileResult, edit: Edit) -> EditResult:
    """Scale every room's position and size proportionally into the current
    envelope, then clamp, so an envelope shrink that stranded rooms out of bounds
    is repaired in one undoable step. Adjacencies survive because every room shares
    the same per-axis scale and origin shift; clamping only nudges float overshoot."""
    assert result.plan is not None
    plan = result.plan
    env_w = float(plan.envelope_width)
    env_l = float(plan.envelope_length)
    rooms = [r for r in plan.rooms if r.id in result.room_lines]
    if not rooms or env_w <= 0 or env_l <= 0:
        return EditResult(source, changed=False,
                          summary="nothing to fit into the envelope")

    min_x = min(r.x for r in rooms)
    min_y = min(r.y for r in rooms)
    bbox_w = max(r.x2 for r in rooms) - min_x
    bbox_l = max(r.y2 for r in rooms) - min_y
    # Shrink to fit when the footprint overflows an axis; never enlarge a plan
    # that already fits (scale caps at 1.0). Guard degenerate zero-width footprints.
    sx = min(1.0, env_w / bbox_w) if bbox_w > 1e-6 else 1.0
    sy = min(1.0, env_l / bbox_l) if bbox_l > 1e-6 else 1.0

    lines = _lines(source)
    changed = False
    for r in rooms:
        nw = min(r.width * sx, env_w)
        nl = min(r.length * sy, env_l)
        nx = (r.x - min_x) * sx
        ny = (r.y - min_y) * sy
        nx = min(max(0.0, nx), max(0.0, env_w - nw))
        ny = min(max(0.0, ny), max(0.0, env_l - nl))
        line_no = result.room_lines[r.id]
        raw = lines[line_no - 1]
        toks = _tokenize_line(raw, line_no)
        size_idx = _room_size_index(toks)
        placement = toks[3:size_idx]
        wt, lt = toks[size_idx + 1], toks[size_idx + 3]  # size <W> x <L>
        splices = [
            (placement[0].col - 1, placement[-1].end_col - 1,
             f"at {_fmt(nx)},{_fmt(ny)}"),
            (wt.col - 1, wt.end_col - 1, _fmt(nw)),
            (lt.col - 1, lt.end_col - 1, _fmt(nl)),
        ]
        newraw = _splice(raw, splices)
        if newraw != raw:
            lines[line_no - 1] = newraw
            changed = True
    return EditResult("\n".join(lines), changed=changed,
                      line=result.room_lines[rooms[0].id],
                      summary=f"fit {len(rooms)} room(s) into {_fmt(env_w)} × {_fmt(env_l)}")


def _delete_room(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    plan = result.plan
    rid = edit.room
    assert rid is not None  # _validate_shape guaranteed
    room_line, err = _deletable_room_line(plan, result, rid)
    if err is not None:
        return EditResult(source, error=err)
    assert room_line is not None
    delete = _room_dependent_lines(plan, rid, room_line)
    lines = _lines(source)
    convert = _room_anchor_conversions(plan, result, lines, rid, delete)
    new_source = _rebuild_without(lines, delete, convert)
    return EditResult(
        new_source, changed=True, line=room_line,
        summary=f"deleted room {rid} and {len(delete) - 1} dependent line(s)",
    )


def _deletable_room_line(plan, result: CompileResult, rid: str) -> tuple[int | None, EditError | None]:
    if plan.room(rid) is None:
        return None, EditError("unknown_room", f"no room {rid!r} in the plan")
    room_line = result.room_lines.get(rid)
    if room_line is None:
        return None, EditError("not_editable", f"room {rid!r} has no source line")
    return room_line, None


def _room_dependent_lines(plan, rid: str, room_line: int) -> set[int]:
    delete: set[int] = {room_line}
    delete.update(d.line for d in plan.interior_doors if rid in (d.room_a, d.room_b) and d.line is not None)
    delete.update(door.line for door in plan.exterior_doors if door.room == rid and door.line is not None)
    delete.update(window.line for window in plan.windows if window.room == rid and window.line is not None)
    delete.update(fixture.line for fixture in plan.fixtures if fixture.room == rid and fixture.line is not None)
    return delete


def _room_anchor_conversions(plan, result: CompileResult, lines: list[str], rid: str, delete: set[int]) -> dict[int, str]:
    convert: dict[int, str] = {}
    for other in plan.rooms:
        rewrite = _anchor_conversion_for_room(other, result, lines, rid, delete)
        if rewrite is not None:
            line_no, raw = rewrite
            convert[line_no] = raw
    return convert


def _anchor_conversion_for_room(other, result: CompileResult, lines: list[str], rid: str, delete: set[int]) -> tuple[int, str] | None:
    if other.id == rid:
        return None
    line_no = result.room_lines.get(other.id)
    if line_no is None or line_no in delete:
        return None
    toks = _tokenize_line(lines[line_no - 1], line_no)
    anchors = [i for i in _room_ref_indices(toks) if i != 1]
    if not any(toks[i].text == rid for i in anchors):
        return None
    size_idx = _room_size_index(toks)
    placement = toks[3:size_idx]
    raw = _splice(lines[line_no - 1], [
        (placement[0].col - 1, placement[-1].end_col - 1, f"at {_fmt(other.x)},{_fmt(other.y)}"),
    ])
    return line_no, raw


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
    fixture = _resolved_fixture(result.plan, edit.key)  # type: ignore[arg-type]
    if fixture is None:
        return EditResult(source, error=EditError("unknown_opening", f"no fixture {edit.key!r}"))
    room = result.plan.room(edit.key.split("~", 1)[0])  # type: ignore[union-attr]
    assert room is not None
    wall = _fixture_wall(edit.wall) if edit.wall is not None else None
    if fixture.seed or fixture.source_line is None:
        return _materialize_fixture_seed(source, result, edit, fixture, room, wall)
    return _rewrite_authored_fixture(source, edit, fixture, wall)


def _materialize_fixture_seed(source: str, result: CompileResult, edit: Edit, fixture, room, wall: str | None) -> EditResult:
    stmt = _fixture_seed_statement(edit, fixture, room, wall)
    line_no = result.room_lines.get(room.id)
    if line_no is None:
        return EditResult(source, error=EditError("not_editable", f"room {room.id!r} has no source line"))
    lines = _lines(source)
    lines.insert(line_no, stmt)
    return EditResult("\n".join(lines), changed=True, line=line_no + 1, summary=f"materialised {fixture.kind} in {room.id}")


def _fixture_seed_statement(edit: Edit, fixture, room, wall: str | None) -> str:
    lx, ly = _fmt(fixture.x - room.x), _fmt(fixture.y - room.y)
    stmt = f"fixture {fixture.kind} in {room.id} at {lx},{ly}"
    fixture_wall = wall or (fixture.wall or None)
    if fixture_wall:
        stmt += f" wall {fixture_wall}"
    if edit.rotate is not None:
        stmt += f" rotate {_fmt(float(edit.rotate))}"
    if edit.width is not None:
        stmt += f" width {_fmt(float(edit.width))}"
    return stmt


def _rewrite_authored_fixture(source: str, edit: Edit, fixture, wall: str | None) -> EditResult:
    lines = _lines(source)
    raw = lines[fixture.source_line - 1]
    toks = _tokenize_line(raw, fixture.source_line)
    along_idx = next((i for i, token in enumerate(toks) if token.text.lower() == "along"), None)
    if along_idx is not None:
        return _rewrite_counter_run_fixture(source, lines, raw, toks, along_idx, edit, fixture.source_line)
    new = _rewrite_fixture_clauses(raw, fixture.source_line, edit, wall)
    if new == raw:
        return EditResult(source, changed=False, line=fixture.source_line, summary=f"fixture {edit.key} unchanged")
    lines[fixture.source_line - 1] = new
    return EditResult("\n".join(lines), changed=True, line=fixture.source_line, summary=f"set fixture {edit.key}")


def _rewrite_counter_run_fixture(source: str, lines: list[str], raw: str, toks: list, along_idx: int, edit: Edit, line_no: int) -> EditResult:
    if edit.width is None:
        return EditResult(source, changed=False, line=line_no, summary=f"fixture {edit.key} unchanged")
    run = float(edit.width)
    new = _counter_run_width_line(raw, toks, along_idx, run)
    if new == raw:
        return EditResult(source, changed=False, line=line_no, summary=f"fixture {edit.key} unchanged")
    lines[line_no - 1] = new
    return EditResult("\n".join(lines), changed=True, line=line_no, summary=f"set counter run {edit.key} to {_fmt(run)} ft")


def _counter_run_width_line(raw: str, toks: list, along_idx: int, run: float) -> str:
    from_idx = next((i for i, token in enumerate(toks) if token.text.lower() == "from"), None)
    to_idx = next((i for i, token in enumerate(toks) if token.text.lower() == "to"), None)
    if from_idx is not None and to_idx is not None:
        base = _as_feet(toks[from_idx + 1].text)
        target = toks[to_idx + 1]
        return _splice(raw, [(target.col - 1, target.end_col - 1, _fmt(base + run))])
    wall_tok = toks[along_idx + 1]
    insert_at = wall_tok.end_col - 1
    return raw[:insert_at] + f" from 0 to {_fmt(run)}" + raw[insert_at:]


def _rewrite_fixture_clauses(raw: str, line_no: int, edit: Edit, wall: str | None) -> str:
    new = raw
    if wall is not None:
        new = _apply_clause(new, line_no, "wall", wall)
    if edit.rotate is not None:
        new = _apply_clause(new, line_no, "rotate", _fmt(float(edit.rotate)), aliases=("rotation",))
    if edit.width is not None:
        new = _apply_clause(new, line_no, "width", _fmt(float(edit.width)))
    return new


# --- plan edits --------------------------------------------------------------


def _set_plan(source: str, result: CompileResult, edit: Edit) -> EditResult:
    lines = _lines(source)
    for updater in (_set_plan_name, _set_plan_envelope, _set_plan_ceiling):
        err = updater(lines, edit)
        if err is not None:
            return EditResult(source, error=err)
    new_source = "\n".join(lines)
    if new_source == source:
        return EditResult(source, changed=False, summary="plan unchanged")
    return EditResult(new_source, changed=True, summary="updated plan")


def _statement_head_line(lines: list[str], name: str) -> int | None:
    for i, raw in enumerate(lines, start=1):
        toks = _tokenize_line(raw, i)
        if toks and toks[0].text.lower() == name:
            return i
    return None


def _set_plan_name(lines: list[str], edit: Edit) -> EditError | None:
    if edit.pname is None:
        return None
    line_no = _statement_head_line(lines, "plan")
    if line_no is None:
        return EditError("not_editable", "no `plan` line to rename")
    toks = _tokenize_line(lines[line_no - 1], line_no)
    name_token = next((token for token in toks[1:] if token.quoted), None)
    if name_token is None:
        return EditError("not_editable", "the `plan` line has no quoted name to rewrite")
    escaped = edit.pname.replace("\\", "\\\\").replace('"', '\\"')
    lines[line_no - 1] = _splice(lines[line_no - 1], [(name_token.col - 1, name_token.end_col - 1, f'"{escaped}"')])
    return None


def _set_plan_envelope(lines: list[str], edit: Edit) -> EditError | None:
    if edit.env_w is None and edit.env_l is None:
        return None
    line_no = _statement_head_line(lines, "envelope")
    if line_no is None:
        return EditError("not_editable", "no `envelope` line to rewrite")
    toks = _tokenize_line(lines[line_no - 1], line_no)  # envelope <W> x <L>
    width_token, length_token = toks[1], toks[3]
    lines[line_no - 1] = _splice(lines[line_no - 1], [
        (width_token.col - 1, width_token.end_col - 1, _fmt(float(edit.env_w))),  # type: ignore[arg-type]
        (length_token.col - 1, length_token.end_col - 1, _fmt(float(edit.env_l))),  # type: ignore[arg-type]
    ])
    return None


def _set_plan_ceiling(lines: list[str], edit: Edit) -> EditError | None:
    if edit.ceiling is None:
        return None
    line_no = _statement_head_line(lines, "ceiling")
    if line_no is not None:
        _rewrite_ceiling_line(lines, line_no, float(edit.ceiling))
        return None
    anchor = _statement_head_line(lines, "envelope") or _statement_head_line(lines, "plan")
    if anchor is None:
        return EditError("not_editable", "nowhere to add a `ceiling` line")
    lines.insert(anchor, f"ceiling {_fmt(float(edit.ceiling))}")
    return None


def _rewrite_ceiling_line(lines: list[str], line_no: int, ceiling: float) -> None:
    toks = _tokenize_line(lines[line_no - 1], line_no)  # ceiling <H>
    height_token = toks[1]
    lines[line_no - 1] = _splice(lines[line_no - 1], [
        (height_token.col - 1, height_token.end_col - 1, _fmt(ceiling)),
    ])


# --- positioned-note edits ---------------------------------------------------
# Notes carry no id, so a positioned note is keyed by its 0-based ordinal among
# `plan.note_marks` — the order they appear in the source (the same order the
# payload lists them), exactly how a repeated fixture-without-id is keyed.


def _quote_note(text: str) -> str:
    """Quote note text for the lexer, escaping backslashes and quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _note_target(result: CompileResult, index: int | None):
    """Resolve a positioned note by index → ``(note, line)`` or an EditError."""
    assert result.plan is not None
    marks = result.plan.note_marks
    if index is None or index < 0 or index >= len(marks):
        return EditError("unknown_note", f"no positioned note at index {index}")
    nm = marks[index]
    if nm.line is None:
        return EditError("not_editable", f"note #{index} has no source line")
    return nm, nm.line


def _plan_head_line(lines: list[str], name: str) -> int | None:
    for i, raw in enumerate(lines, start=1):
        toks = _tokenize_line(raw, i)
        if toks and toks[0].text.lower() == name:
            return i
    return None


def _add_note(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    plan = result.plan
    level = int(edit.level) if edit.level is not None else 0
    stmt = (f"note {_quote_note(edit.text)} "  # type: ignore[arg-type]
            f"at {_fmt(float(edit.x))},{_fmt(float(edit.y))}")  # type: ignore[arg-type]
    if level:
        stmt += f" level {level}"
    lines = _lines(source)
    note_lines = [nm.line for nm in plan.note_marks if nm.line is not None]
    if note_lines:
        after = max(note_lines)
    else:
        after = (_envelope_line(lines) or _plan_head_line(lines, "ceiling")
                 or _plan_head_line(lines, "plan") or len(lines))
    lines.insert(after, stmt)
    return EditResult("\n".join(lines), changed=True, line=after + 1,
                      summary=f"added note {stmt.split(None, 1)[1]}")


def _move_note(source: str, result: CompileResult, edit: Edit) -> EditResult:
    found = _note_target(result, edit.index)
    if isinstance(found, EditError):
        return EditResult(source, error=found)
    nm, line_no = found
    tx, ty = float(edit.x), float(edit.y)  # type: ignore[arg-type]
    if _close(tx, nm.x) and _close(ty, nm.y):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"note already at {_fmt(tx)},{_fmt(ty)}")
    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    at_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "at"), None)
    if at_idx is None or at_idx + 2 >= len(toks):
        return EditResult(source, error=EditError("not_editable",
                          "note line has no `at x,y` to rewrite"))
    xt, yt = toks[at_idx + 1], toks[at_idx + 2]
    newraw = _splice(raw, [
        (xt.col - 1, xt.end_col - 1, _fmt(tx)),
        (yt.col - 1, yt.end_col - 1, _fmt(ty)),
    ])
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"note → at {_fmt(tx)},{_fmt(ty)}")


def _set_note(source: str, result: CompileResult, edit: Edit) -> EditResult:
    found = _note_target(result, edit.index)
    if isinstance(found, EditError):
        return EditResult(source, error=found)
    nm, line_no = found
    lines = _lines(source)
    raw = lines[line_no - 1]
    toks = _tokenize_line(raw, line_no)
    splices: list[tuple[int, int, str]] = []
    if edit.text is not None:
        qt = next((t for t in toks[1:] if t.quoted), None)
        if qt is None:
            return EditResult(source, error=EditError("not_editable",
                              "note line has no quoted text to rewrite"))
        splices.append((qt.col - 1, qt.end_col - 1, _quote_note(edit.text)))
    if edit.x is not None or edit.y is not None:
        at_idx = next((i for i, t in enumerate(toks) if t.text.lower() == "at"), None)
        if at_idx is None or at_idx + 2 >= len(toks):
            return EditResult(source, error=EditError("not_editable",
                              "note line has no `at x,y` to rewrite"))
        if edit.x is not None:
            xt = toks[at_idx + 1]
            splices.append((xt.col - 1, xt.end_col - 1, _fmt(float(edit.x))))
        if edit.y is not None:
            yt = toks[at_idx + 2]
            splices.append((yt.col - 1, yt.end_col - 1, _fmt(float(edit.y))))
    newraw = _splice(raw, splices)
    if newraw == raw:
        return EditResult(source, changed=False, line=line_no, summary="note unchanged")
    lines[line_no - 1] = newraw
    return EditResult("\n".join(lines), changed=True, line=line_no, summary="set note")


def _delete_note(source: str, result: CompileResult, edit: Edit) -> EditResult:
    found = _note_target(result, edit.index)
    if isinstance(found, EditError):
        return EditResult(source, error=found)
    _nm, line_no = found
    new_source = _rebuild_without(_lines(source), {line_no})
    return EditResult(new_source, changed=True, line=line_no,
                      summary=f"deleted note #{edit.index}")


# --- electrical devices (outlet / switch / light) --------------------------
# Each `add_*` inserts a one-line statement right after the target room's `room`
# line (like `add_fixture`); each `delete_*` drops the device's own source line,
# resolved by its 0-based ordinal in that device list.


def _insert_after_room(source: str, result: CompileResult, room: str, stmt: str,
                       summary: str) -> EditResult:
    line_no, err = _room_line(result, room)
    if err is not None:
        return EditResult(source, error=err)
    assert line_no is not None
    lines = _lines(source)
    lines.insert(line_no, stmt)
    return EditResult("\n".join(lines), changed=True, line=line_no + 1, summary=summary)


def _add_outlet(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    wall = _fixture_wall(edit.wall)  # type: ignore[arg-type]
    off = _fmt(float(edit.offset)) if edit.offset is not None else "1"
    stmt = f"outlet in {edit.room} wall {wall} offset {off}"
    if edit.gfci:
        stmt += " gfci"
    return _insert_after_room(source, result, edit.room, stmt,  # type: ignore[arg-type]
                              f"added outlet in {edit.room}")


def _add_switch(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    wall = _fixture_wall(edit.wall)  # type: ignore[arg-type]
    off = _fmt(float(edit.offset)) if edit.offset is not None else "1"
    stmt = f"switch in {edit.room} wall {wall} offset {off}"
    return _insert_after_room(source, result, edit.room, stmt,  # type: ignore[arg-type]
                              f"added switch in {edit.room}")


def _add_light(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    stmt = (f"light in {edit.room} "
            f"at {_fmt(float(edit.x))},{_fmt(float(edit.y))}")  # type: ignore[arg-type]
    kind = (edit.fkind or "ceiling").lower()
    if kind != "ceiling":
        stmt += f" kind {kind}"
    return _insert_after_room(source, result, edit.room, stmt,  # type: ignore[arg-type]
                              f"placed light in {edit.room}")


def _add_alarm(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    kind = (edit.fkind or "smoke").lower()
    stmt = f"alarm {kind} in {edit.room}"
    if edit.x is not None and edit.y is not None:
        stmt += f" at {_fmt(float(edit.x))},{_fmt(float(edit.y))}"
    return _insert_after_room(source, result, edit.room, stmt,  # type: ignore[arg-type]
                              f"placed {kind} alarm in {edit.room}")


def _delete_electrical(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    which = {
        "delete_outlet": ("outlets", "outlet"),
        "delete_switch": ("switches", "switch"),
        "delete_light": ("lights", "light"),
        "delete_alarm": ("alarms", "alarm"),
    }[edit.kind]
    devices = getattr(result.plan, which[0])
    idx = edit.index
    if idx is None or idx < 0 or idx >= len(devices):
        return EditResult(source, error=EditError(
            "unknown_opening", f"no {which[1]} at index {idx}"))
    dev = devices[idx]
    if dev.line is None:
        return EditResult(source, error=EditError(
            "not_editable", f"{which[1]} #{idx} has no source line"))
    new_source = _rebuild_without(_lines(source), {dev.line})
    return EditResult(new_source, changed=True, line=dev.line,
                      summary=f"deleted {which[1]} #{idx}")


# --- cross-file composition edits (the `use` statement) ----------------------
# Stamped members are read-only; instances are first-class. Member edits are
# refused with a typed teaching error (edit the part, or Inline); the instance
# lives on the `use` line, edited by add/move/set/delete/inline_use.


def _instance_by_alias(result: CompileResult, alias: str | None):
    for inst in getattr(result.plan, "instances", []):
        if inst.alias == alias:
            return inst
    return None


def _refuse_stamped(result: CompileResult, edit: Edit) -> EditError | None:
    """A typed ``not_editable`` if ``edit`` would mutate a stamped member.

    The stamped element is owned by its part file — the message points the author
    at the part, or at Inlining the instance to make it local (§6)."""
    plan = result.plan
    assert plan is not None
    stamped: set[str] = getattr(plan, "stamped_rooms", set())
    if not stamped:
        return None

    def refuse(rid: str) -> EditError:
        inst = next((i for i in plan.instances if rid in i.room_ids), None)
        where = inst.relpath if inst is not None else "a part"
        return EditError(
            "not_editable",
            f"{rid} is stamped from {where} — edit that file, or Inline the "
            "instance to make it local.",
        )

    room_edits = {
        "move_room", "resize_room", "set_room_type", "rename_room", "delete_room",
        "add_fixture", "add_outlet", "add_switch", "add_light", "add_alarm",
    }
    if edit.kind in room_edits and edit.room in stamped:
        return refuse(edit.room)
    if edit.kind == "add_opening" and edit.opening in ("window", "entry") and edit.room in stamped:
        return refuse(edit.room)
    if edit.kind in ("move_fixture", "set_fixture", "delete_fixture") and edit.key:
        # The fixture key is `<room>~<kind>~<i>`; its room is the leading segment.
        room = edit.key.split("~", 1)[0]
        if room in stamped:
            return refuse(room)
    return None


def _use_line(result: CompileResult, alias: str) -> tuple[object | None, int | None]:
    """The ``UseSpec`` (and its 1-based source line) for ``alias``, or ``(None, None)``."""
    for u in getattr(result.plan, "uses", []):
        if u.alias == alias:
            return u, u.line
    return None, None


def _use_stmt(relpath: str, alias: str, x: float, y: float, level: int,
              mirror: str | None = None, rotate: int = 0) -> str:
    line = f'use "{relpath}" as {alias} at {_fmt(x)},{_fmt(y)}'
    if level:
        line += f" level {level}"
    if mirror:
        line += f" mirror {mirror}"
    if rotate:
        line += f" rotate {int(rotate)}"
    return line


def _add_use(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    if _instance_by_alias(result, edit.alias) is not None or any(
        u.alias == edit.alias for u in result.plan.uses
    ):
        return EditResult(source, error=EditError(
            "bad_value", f"alias {edit.alias!r} is already used"))
    level = int(edit.level) if edit.level is not None else 0
    mirror = edit.mirror if edit.mirror in ("x", "y") else None
    rotate = int(edit.rotate) if edit.rotate in (90, 180, 270) else 0
    stmt = _use_stmt(edit.relpath, edit.alias, float(edit.x), float(edit.y), level,  # type: ignore[arg-type]
                     mirror, rotate)
    lines = _lines(source)
    use_lines = [u.line for u in result.plan.uses if u.line is not None]
    if use_lines:
        after = max(use_lines)
    else:
        after = _envelope_line(lines) or len(lines)
    lines.insert(after, stmt)
    return EditResult("\n".join(lines), changed=True, line=after + 1,
                      summary=f"added use {edit.alias}")


def _move_use(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    use, line_no = _use_line(result, edit.alias)  # type: ignore[arg-type]
    if use is None or line_no is None:
        return EditResult(source, error=EditError(
            "unknown_room", f"no instance {edit.alias!r} in the plan"))
    nx, ny = float(edit.x), float(edit.y)  # type: ignore[arg-type]
    if _close(use.x, nx) and _close(use.y, ny):  # type: ignore[attr-defined]
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.alias} unchanged")
    umir, urot = use.mirror, use.rotate  # type: ignore[attr-defined]
    stmt = _use_stmt(use.relpath, use.alias, nx, ny, use.level,  # type: ignore[attr-defined]
                     umir, urot)
    lines = _lines(source)
    lines[line_no - 1] = _with_comment(lines[line_no - 1], stmt)
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"moved {edit.alias} to {_fmt(nx)},{_fmt(ny)}")


def _set_use(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    use, line_no = _use_line(result, edit.alias)  # type: ignore[arg-type]
    if use is None or line_no is None:
        return EditResult(source, error=EditError(
            "unknown_room", f"no instance {edit.alias!r} in the plan"))
    level = int(edit.level) if edit.level is not None else use.level  # type: ignore[attr-defined]
    mirror = use.mirror  # type: ignore[attr-defined]
    if edit.mirror_set:
        mirror = edit.mirror if edit.mirror in ("x", "y") else None
    rotate: int = use.rotate  # type: ignore[attr-defined]
    if edit.rotate_set:
        r = int(edit.rotate) if edit.rotate is not None else 0
        if r not in (0, 90, 180, 270):
            return EditResult(source, error=EditError(
                "bad_value", "set_use rotate must be 0, 90, 180 or 270"))
        rotate = r
    cur = (use.level, use.mirror, use.rotate)  # type: ignore[attr-defined]
    if cur == (level, mirror, rotate):
        return EditResult(source, changed=False, line=line_no,
                          summary=f"{edit.alias} unchanged")
    stmt = _use_stmt(use.relpath, use.alias, use.x, use.y, level,  # type: ignore[attr-defined]
                     mirror, rotate)
    lines = _lines(source)
    lines[line_no - 1] = _with_comment(lines[line_no - 1], stmt)
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"set instance {edit.alias}")


def _delete_use(source: str, result: CompileResult, edit: Edit) -> EditResult:
    assert result.plan is not None
    _use, line_no = _use_line(result, edit.alias)  # type: ignore[arg-type]
    if line_no is None:
        return EditResult(source, error=EditError(
            "unknown_room", f"no instance {edit.alias!r} in the plan"))
    new_source = _rebuild_without(_lines(source), {line_no})
    return EditResult(new_source, changed=True, line=line_no,
                      summary=f"deleted instance {edit.alias}")


def _inline_use(source: str, result: CompileResult, edit: Edit) -> EditResult:
    """Replace the ``use`` line with the stamped members as literal statements —
    the escape hatch that keeps text sovereign (§6). One undo step; the inlined
    plan recompiles to the same composed plan."""
    assert result.plan is not None
    inst = _instance_by_alias(result, edit.alias)
    _use, line_no = _use_line(result, edit.alias)  # type: ignore[arg-type]
    if inst is None or line_no is None:
        return EditResult(source, error=EditError(
            "not_editable", f"instance {edit.alias!r} isn't stamped (nothing to inline)"))
    from .emit import instance_lines

    body = instance_lines(inst)
    lines = _lines(source)
    lines[line_no - 1:line_no] = body
    return EditResult("\n".join(lines), changed=True, line=line_no,
                      summary=f"inlined instance {edit.alias} ({len(body)} statement(s))")


def _with_comment(raw: str, stmt: str) -> str:
    """Replace ``raw``'s statement with ``stmt``, preserving any trailing comment
    and leading indentation."""
    from .pragma import _comment_start

    indent = raw[:len(raw) - len(raw.lstrip())]
    cut = _comment_start(raw)
    comment = "" if cut is None else " " + raw[cut:].strip()
    return f"{indent}{stmt}{comment}"


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
