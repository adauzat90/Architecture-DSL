"""Surgical textual edits to DSL source — Tier 5, direct manipulation round-tripped.

Viewport gestures (drag a room, resize it, slide a door/window along its wall)
become the *smallest possible* change to the DSL **text**, so the source stays
the single source of truth. This is deliberately **not** :func:`barndsl.emit.emit_dsl`
(full model→text regeneration): that would flatten comments, spacing and the
author's statement order. Instead each function here compiles the source to find
the one statement to touch, re-tokenizes only that line, rewrites the tokens that
actually changed, and returns the source with every other byte preserved.

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
from dataclasses import dataclass

from .compiler import CompileResult, _tokenize_line, compile_source
from .geometry import shared_edge, wall_segment

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


@dataclass(frozen=True)
class EditError:
    """A refused edit — bad shape, unknown target, or unbuildable source.

    ``kind`` ∈ ``malformed`` (the edit itself is ill-formed), ``unknown_room`` /
    ``unknown_opening`` (no such target in the plan), ``not_editable`` (the source
    doesn't compile, or the target has no authored source line). Never raised —
    returned inside :class:`EditResult` so the API stays exception-free.
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
    return EditError("malformed", f"unknown edit kind {kind!r}")


def _as_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _as_num(v: object) -> float | None:
    if isinstance(v, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


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
    """
    out: list[dict] = []
    for kind, key, obj in iter_openings(plan):
        geom = _opening_geom(plan, kind, obj)
        if geom is None:
            continue
        out.append({"kind": kind, "key": key, "line": getattr(obj, "line", None), **geom})
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
    return EditError("malformed", f"unknown edit kind {edit.kind!r}")


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
