"""Cross-file composition — the ``use`` loader and stamper (Phase 7a).

A ``use "<relpath>" as <alias> at <x>,<y> [level <n>]`` statement instantiates a
**part** — any ``.barn`` file with no ``plan`` header — into the host plan. This
module owns the compile-time machinery behind that:

* **The resolver** (:func:`_resolve`) is rooted at the top-level file's directory:
  relative paths only, ``realpath`` containment (symlink escapes rejected), a size
  cap, never an OS error — a bad path is a teaching ``USE_UNRESOLVED`` diagnostic.
* **The loader** (:func:`load_part`) fragment-compiles each part **once** per
  top-level compile (memoized by resolved path) into a :class:`PartComponent`:
  its normalized rooms/openings/fixtures/devices and its own *local* diagnostics.
* **The stamper** (:func:`stamp_instance`) transforms every element (translate,
  plus the optional ``rotate``-then-``mirror`` of Phase 7b — geometry about the
  part bbox, wall directions and offsets remapped per §5) and prefixes every id
  **and** every internal reference with ``<alias>.``, then appends the copies to
  the host plan *before* validation — so overlap, envelope bounds, egress,
  adjacency, electrical spacing all run on the composed plan with no new code
  downstream.

Two diagnostic classes fall out (see :func:`compose_uses`): *part-internal*
findings (fire inside the part regardless of placement) are reported **once per
part file**, anchored to the part's own ``file:line``; *instance* findings
(placement-dependent — overlap, out of envelope, egress) fire **per use**,
anchored to the ``use`` line with the alias named.

Phase 7b adds the ``mirror``/``rotate`` transform (the §5 remap table): the part
is rotated (ccw, 90° steps) then mirrored in its own local frame before placing.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from .elements import (
    Barndominium,
    Direction,
    InteriorDoor,
    Instance,
    PlacedFixture,
    Room,
    UseSpec,
)
from .geometry import shared_edge
from .validation import Issue, Severity

#: Nesting depth (Phase 20): the host is depth 0, a part it ``use``s is depth 1,
#: a part *that* part uses is depth 2. A ``use`` that would reach depth 3 is the
#: ``USE_NESTED`` error ("deeper than 2"). Instance count and file-size guards keep
#: a pathological plan from turning into a resource problem (each is a teaching
#: diagnostic, never a crash — see §8). The instance cap is **global across all
#: depths** (a shared budget), so nesting can't multiply past it.
MAX_USE_DEPTH = 2
MAX_INSTANCES = 64
MAX_PART_BYTES = 256 * 1024

#: Diagnostic codes that are *part-internal*: a finding about a part's own rooms,
#: fixtures, openings or devices that fires **regardless of where the part is
#: placed**. Only these survive a fragment compile as the part's own diagnostics;
#: every other (whole-building / placement-dependent) code is dropped in fragment
#: mode and instead surfaces per-instance on the composed plan. Kept deliberately
#: conservative — a code left out here simply surfaces per-use instead of once,
#: never the reverse (which would wrongly dedupe a real placement issue).
PART_LOCAL_CODES = frozenset({
    "DUP_ID",
    # room geometry (a room's own shape/size — independent of placement)
    "ROOM_SIZE", "ROOM_TIGHT", "ROOM_PROPORTION", "MUDROOM_SHAPE", "ROOM_GEOMETRY",
    "ROOM_CLEAR", "ROOM_HABITABLE",
    "BEDROOM_AREA", "BEDROOM_DIM", "CLOSET_SHAPE", "BATH_CLEARANCE",
    "LAUNDRY_FIT", "BATH_OVERSIZE", "OPEN_BATH", "BATH_DISTANCE",
    # fixtures & furniture clearances (room-local)
    "FIXTURE_TOILET_CLEARANCE", "FIXTURE_FRONT", "FIXTURE_BACKING",
    "FIXTURE_DOOR", "FIXTURE_OVERLAP", "FIXTURE_OOB", "FIXTURE_ROOM",
    "FIXTURE_ROOM_TYPE", "FIXTURE_EGRESS", "BED_CLEARANCE", "DINING_CLEARANCE",
    "KITCHEN_TRIANGLE", "KITCHEN_FIT", "RANGE_LANDING", "DRYER_VENT",
    # openings that clash with the part's own geometry/fixtures
    "OPENING_CLASH", "OPENING_SIZE", "DOOR_HITS_FIXTURE", "DOOR_SWING_CLASH",
    # room overlaps within the part
    "OVERLAP",
    # devices (room-local rules)
    "OUTLET_SPACING", "OUTLET_GFCI", "ROOM_NO_LIGHT", "DEVICE_ROOM",
})


@dataclass
class PartComponent:
    """A fragment-compiled part, cached once per resolved path.

    :attr:`plan` is the normalized fragment plan (SW corner at 0,0);
    :attr:`diagnostics` are its *local* diagnostics (already anchored to the part's
    own lines, pragmas applied); :attr:`width`/:attr:`length` are its stamped
    bounding-box size; :attr:`has_errors` is True if the part doesn't compile
    cleanly on its own (then it isn't stamped — see :func:`compose_uses`).
    """

    path: str
    relpath: str
    plan: Barndominium
    diagnostics: list[Issue]
    width: float
    length: float
    has_errors: bool


@dataclass
class Composition:
    """The result of stamping every ``use`` — the bookkeeping the host compile
    needs to reclassify the composed plan's diagnostics (see :func:`compose_uses`)."""

    #: Stamped room id -> ``(local_id, Instance)`` (``local_id`` is the id inside
    #: the part, without the ``<alias>.`` prefix — one level only, so a nested
    #: stamped room ``outer.inner.room`` maps to ``inner.room``).
    stamped_map: dict[str, tuple[str, Instance]] = field(default_factory=dict)
    #: Resolved part path -> the set of ``(code, local_id)`` its fragment compile
    #: reported — the keys a composed stamped-room diagnostic is deduped against.
    part_keys: dict[str, set[tuple[str, str]]] = field(default_factory=dict)


@dataclass
class _ComposeCtx:
    """Recursion state threaded through a (possibly nested) composition — the
    sandbox root, the current depth, the cycle stack, and the *shared* part memo +
    instance budget (Phase 20). Built once at the top level; each nested part
    compile gets a child context (:meth:`descend`)."""

    #: The sandbox root — the top-level (host) file's directory, realpath'd. Every
    #: resolved part, at every depth, must stay under this after realpath. ``None``
    #: for a source with no home directory (any ``use`` is ``USE_UNRESOLVED``).
    root: str | None
    #: Depth of the file whose ``use``s are being composed (host = 0).
    depth: int
    #: Realpaths of every ancestor **including the current file** — a resolved
    #: candidate already in here is a ``USE_CYCLE`` (self-use or mutual use).
    stack: tuple[str, ...]
    #: Shared across the whole compile tree, so a part reused at several points (or
    #: depths) fragment-compiles once. Keyed ``(path, sorted-param-items)``.
    memo: dict[tuple, PartComponent]
    #: Single-element mutable [remaining instances] — decremented on every stamp at
    #: every depth, so the cap is global.
    budget: list[int]

    @classmethod
    def top_level(cls, base_dir: str | None, self_path: str | None) -> "_ComposeCtx":
        root = os.path.realpath(base_dir) if base_dir is not None else None
        stack = (os.path.realpath(self_path),) if self_path else ()
        return cls(root=root, depth=0, stack=stack, memo={}, budget=[MAX_INSTANCES])

    def descend(self, child_path: str) -> "_ComposeCtx":
        """The context a nested part compiles under — one deeper, with the child on
        the cycle stack; root, memo and budget are shared by reference."""
        return _ComposeCtx(
            root=self.root, depth=self.depth + 1,
            stack=self.stack + (child_path,), memo=self.memo, budget=self.budget,
        )


# --- fragment-compile counter (memoization is observable in tests) ------------

#: Incremented on every *actual* fragment compile (a memo hit doesn't bump it), so
#: a test can assert one part compile for N uses of the same file. Reset via
#: :func:`reset_fragment_compiles`.
_fragment_compiles = 0


def reset_fragment_compiles() -> None:
    global _fragment_compiles
    _fragment_compiles = 0


def fragment_compiles() -> int:
    return _fragment_compiles


# --- origin normalization -----------------------------------------------------


def normalize_part_origin(plan: Barndominium) -> tuple[float, float]:
    """Shift a part so its south-west-most room corner sits at ``(0, 0)``.

    Returns the ``(dx, dy)`` applied (``(0, 0)`` when the part already starts at
    the origin). Translates rooms, positioned notes and porches; room-local
    offsets (fixtures, lights, alarms, wall-relative devices/openings) are
    unaffected by definition.
    """
    if not plan.rooms:
        return (0.0, 0.0)
    minx = min(r.x for r in plan.rooms)
    miny = min(r.y for r in plan.rooms)
    if abs(minx) < 1e-9 and abs(miny) < 1e-9:
        return (0.0, 0.0)
    for r in plan.rooms:
        r.x -= minx
        r.y -= miny
    for nm in plan.note_marks:
        nm.x -= minx
        nm.y -= miny
    for p in plan.porches:
        p.x -= minx
        p.y -= miny
    return (minx, miny)


# --- the resolver -------------------------------------------------------------


def _resolve(relpath: str, base_dir: str | None, root: str | None) -> tuple[str | None, str | None]:
    """Resolve ``relpath`` against the **using file's** directory ``base_dir``,
    then require the result to stay under the sandbox ``root`` (the top-level host
    directory). Returns ``(resolved_path, None)`` or ``(None, reason)`` where
    ``reason`` is one of ``no_base_dir`` / ``absolute`` / ``escape`` / ``missing`` /
    ``too_big``.

    A nested ``use`` (Phase 20) resolves relative to the part that wrote it
    (``base_dir``), exactly as the host's ``use`` resolves relative to the host —
    but the containment check is always against the host ``root``, so a nested
    ``../`` (or a symlink) that leaves the host tree is an ``escape``, never a read
    outside the sandbox."""
    if base_dir is None or root is None:
        return None, "no_base_dir"
    if not relpath or os.path.isabs(relpath):
        return None, "absolute"
    candidate = os.path.realpath(os.path.join(base_dir, relpath))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None, "escape"
    if not os.path.isfile(candidate):
        return None, "missing"
    try:
        if os.path.getsize(candidate) > MAX_PART_BYTES:
            return None, "too_big"
    except OSError:
        return None, "missing"
    return candidate, None


_UNRESOLVED_TEXT = {
    "no_base_dir": (
        "can't resolve a part — this source has no home directory to resolve "
        "against (a pasted or browser-opened buffer).",
        "Compile the file from disk, or serve its folder, so `use` paths have a "
        "root to resolve against.",
    ),
    "absolute": (
        "part paths must be relative (resolved against the including file's "
        "directory) — an absolute path isn't allowed.",
        'Write it relative, e.g. `use "parts/bath_core.barn" as b at 0,0`.',
    ),
    "escape": (
        "part path escapes the including file's directory (`..` or a symlink "
        "leaving the root).",
        "Keep parts under the folder you compile/serve; the loader never reads "
        "outside it.",
    ),
    "missing": (
        "no such part file under the including file's directory.",
        "Check the path (relative to the including file), or create the part.",
    ),
    "too_big": (
        f"part file exceeds the {MAX_PART_BYTES // 1024} KiB size limit.",
        "A reusable block should be small — split it, or trim it down.",
    ),
    "cap": (
        f"this plan would stamp more than {MAX_INSTANCES} part instances.",
        f"Composition is capped at {MAX_INSTANCES} instances per plan — inline "
        "some, or reduce the count.",
    ),
}


def _unresolved(relpath: str, reason: str, use: UseSpec) -> Issue:
    body, hint = _UNRESOLVED_TEXT[reason]
    return Issue(
        Severity.ERROR,
        "USE_UNRESOLVED",
        f"use \"{relpath}\" — {body}",
        line=use.line,
        col=use.col,
        end_col=use.end_col,
        hint=hint,
    )


# --- the loader ---------------------------------------------------------------


def _memo_key(path: str, params: dict[str, float]) -> tuple:
    """The per-instance memo key (Phase 20): a parametric part's compiled result
    depends on its param values, so key the memo on ``(path, sorted items)`` — two
    instances with the same params share a compile, different params recompile."""
    return (path, tuple(sorted(params.items())))


def load_part(
    use: UseSpec,
    base_dir: str | None,
    ctx: "_ComposeCtx",
    profile: object | None,
) -> tuple[PartComponent | None, Issue | None]:
    """Resolve + fragment-compile the part named by ``use`` (memoized per
    ``(path, params)``).

    Returns ``(component, None)`` on success, ``(None, issue)`` for an unresolvable
    path / a cycle / an over-deep nesting (each anchored to the ``use`` line). The
    part is compiled at most once per ``(resolved path, param values)`` per
    top-level compile; its own nested ``use``s are composed under a child context.
    """
    from .compiler import compile_source

    candidate, reason = _resolve(use.relpath, base_dir, ctx.root)
    if reason is not None:
        return None, _unresolved(use.relpath, reason, use)
    assert candidate is not None
    # Cycle (self-use or mutual use) before depth, so a→b→a reads as USE_CYCLE
    # rather than a depth overflow. The candidate resolves (it exists) and is
    # already an ancestor on the stack.
    if candidate in ctx.stack:
        return None, _cycle_issue(use, ctx.stack, candidate)
    if ctx.depth + 1 > MAX_USE_DEPTH:
        return None, _nested_issue(use)
    key = _memo_key(candidate, use.params)
    if key in ctx.memo:
        return ctx.memo[key], None
    global _fragment_compiles
    try:
        with open(candidate, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, _unresolved(use.relpath, "missing", use)
    _fragment_compiles += 1
    res = compile_source(
        text, name=use.relpath, fragment=True,
        base_dir=os.path.dirname(candidate), profile=profile,  # type: ignore[arg-type]
        params=use.params, compose_ctx=ctx.descend(candidate),
    )
    plan = res.plan
    width = length = 0.0
    if plan is not None and plan.rooms:
        width = max(r.x + r.width for r in plan.rooms)
        length = max(r.y + r.length for r in plan.rooms)
    has_errors = plan is None or any(d.severity is Severity.ERROR for d in res.diagnostics)
    component = PartComponent(
        candidate, use.relpath, plan if plan is not None else Barndominium(name=use.relpath),
        list(res.diagnostics), width, length, has_errors,
    )
    ctx.memo[key] = component
    return component, None


def _cycle_issue(use: UseSpec, stack: tuple[str, ...], candidate: str) -> Issue:
    """A clean ``USE_CYCLE`` naming the cycle path (files, not full paths) — never a
    hang or a recursion crash."""
    names = [os.path.basename(p) for p in stack]
    # The cycle is the tail of the stack from the first appearance of the
    # candidate, closed back to it.
    start = stack.index(candidate)
    chain = " → ".join(names[start:] + [os.path.basename(candidate)])
    return Issue(
        Severity.ERROR, "USE_CYCLE",
        f"part cycle: {chain}. A part can't `use` itself or an ancestor.",
        line=use.line, col=use.col, end_col=use.end_col,
        hint="Break the loop — a part library is a tree, not a ring.",
    )


def _nested_issue(use: UseSpec) -> Issue:
    """A ``USE_NESTED`` error for a ``use`` nested deeper than depth 2 (Phase 20)."""
    return Issue(
        Severity.ERROR, "USE_NESTED",
        f'use "{use.relpath}" nests deeper than 2 — a part used by a part can\'t '
        "itself `use` another part.",
        line=use.line, col=use.col, end_col=use.end_col,
        hint="Composition is depth-2: host → part → part. Flatten the deepest "
        "level, or `use` it one level up.",
    )


# --- the transform (§5 remap table) -------------------------------------------
#
# A ``use`` may rotate (ccw, 90° steps) then mirror the part in its own local
# frame before placing it. Geometry rotates/mirrors about the part's local bbox;
# then ``at`` translates so the TRANSFORMED bbox's SW corner lands where the
# author said. Attribute remapping (wall directions, wall offsets, fixture
# rotations) is the fiddly part — it is derived *from the transformed geometry*
# wherever an offset is involved (the robust route: re-measure from the new
# wall's start corner), and from the §5 table for the discrete wall directions.

#: Rotate a wall direction 90° counter-clockwise: S→E, E→N, N→W, W→S (§5).
_ROT90_WALL: dict[Direction, Direction] = {
    Direction.SOUTH: Direction.EAST,
    Direction.EAST: Direction.NORTH,
    Direction.NORTH: Direction.WEST,
    Direction.WEST: Direction.SOUTH,
}
#: Mirror ``y`` (a vertical mirror line): E↔W, N/S fixed (§5).
_MIRROR_Y_WALL: dict[Direction, Direction] = {
    Direction.EAST: Direction.WEST, Direction.WEST: Direction.EAST,
    Direction.NORTH: Direction.NORTH, Direction.SOUTH: Direction.SOUTH,
}
#: Mirror ``x`` (a horizontal mirror line): N↔S, E/W fixed (§5).
_MIRROR_X_WALL: dict[Direction, Direction] = {
    Direction.NORTH: Direction.SOUTH, Direction.SOUTH: Direction.NORTH,
    Direction.EAST: Direction.EAST, Direction.WEST: Direction.WEST,
}


class _Xform:
    """The linear part of a ``rotate``-then-``mirror`` transform, plus the wall and
    fixture-rotation remaps that ride with it. Point translation to ``at`` is
    handled per-frame (part-world vs room-local) by the caller."""

    def __init__(self, rotate: int, mirror: str | None) -> None:
        self.rotate = rotate % 360
        self.mirror = mirror
        # 2x2 matrix (a, b, c, d): x' = a*x + b*y, y' = c*x + d*y.
        rot = {
            0: (1, 0, 0, 1), 90: (0, -1, 1, 0),
            180: (-1, 0, 0, -1), 270: (0, 1, -1, 0),
        }[self.rotate]
        mir = {None: (1, 0, 0, 1), "y": (-1, 0, 0, 1), "x": (1, 0, 0, -1)}[mirror]
        # mirror applied AFTER rotate (rotate-then-mirror): M = mir @ rot.
        a1, b1, c1, d1 = rot
        a2, b2, c2, d2 = mir
        self.m = (
            a2 * a1 + b2 * c1, a2 * b1 + b2 * d1,
            c2 * a1 + d2 * c1, c2 * b1 + d2 * d1,
        )

    @property
    def identity(self) -> bool:
        return self.rotate == 0 and self.mirror is None

    def _lin(self, px: float, py: float) -> tuple[float, float]:
        a, b, c, d = self.m
        return (a * px + b * py, c * px + d * py)

    def _origin(self, w: float, l: float) -> tuple[float, float]:
        """The min corner of the transformed ``w×l`` bbox — the shift that
        renormalizes the transformed frame back to a SW corner at (0, 0)."""
        pts = [self._lin(x, y) for x, y in ((0, 0), (w, 0), (0, l), (w, l))]
        return (min(p[0] for p in pts), min(p[1] for p in pts))

    def dims(self, w: float, l: float) -> tuple[float, float]:
        """The transformed bbox size (a 90/270 turn swaps width and length)."""
        return (l, w) if self.rotate in (90, 270) else (w, l)

    def point(self, px: float, py: float, w: float, l: float) -> tuple[float, float]:
        """Transform a point in a ``w×l`` frame, renormalized to that frame's SW
        corner (0, 0). Add the placement/room origin afterwards."""
        ox, oy = self._origin(w, l)
        tx, ty = self._lin(px, py)
        return (tx - ox, ty - oy)

    def rect(self, x: float, y: float, rw: float, rl: float, w: float, l: float
             ) -> tuple[float, float, float, float]:
        """Transform an axis-aligned rect ``(x, y, rw, rl)`` living in a ``w×l``
        frame; returns the renormalized ``(x', y', w', l')`` (SW corner + positive
        size — a 90/270 turn swaps the size)."""
        c1 = self.point(x, y, w, l)
        c2 = self.point(x + rw, y + rl, w, l)
        return (min(c1[0], c2[0]), min(c1[1], c2[1]),
                abs(c2[0] - c1[0]), abs(c2[1] - c1[1]))

    def wall(self, direction: Direction) -> Direction:
        """Remap a wall direction through the §5 table (rotate then mirror)."""
        d = direction
        for _ in range(self.rotate // 90):
            d = _ROT90_WALL[d]
        if self.mirror == "y":
            d = _MIRROR_Y_WALL[d]
        elif self.mirror == "x":
            d = _MIRROR_X_WALL[d]
        return d

    def fixture_rotation(self, rotation: float) -> float:
        """Compose a fixture's own ``rotate`` with the instance transform:
        additive under rotate, reflected under mirror (derived from render.py:
        a mirror flips the plan glyph, negating the turn)."""
        r = rotation + self.rotate
        if self.mirror == "y":
            r = -r
        elif self.mirror == "x":
            r = 180.0 - r
        return r % 360.0


def _wall_span(wall: Direction, offset: float, width: float, rw: float, rl: float
               ) -> tuple[tuple[float, float], tuple[float, float]]:
    """The two room-local endpoints of an opening ``offset`` ft along ``wall``
    (S/W start convention), spanning ``width``."""
    if wall is Direction.SOUTH:
        return (offset, 0.0), (offset + width, 0.0)
    if wall is Direction.NORTH:
        return (offset, rl), (offset + width, rl)
    if wall is Direction.WEST:
        return (0.0, offset), (0.0, offset + width)
    return (rw, offset), (rw, offset + width)  # EAST


def _remap_wall_offset(xf: _Xform, wall: Direction, offset: float, width: float,
                       rw: float, rl: float) -> tuple[Direction, float]:
    """Transform a wall-attached feature: return ``(new_wall, new_offset)`` with the
    offset **re-derived from the transformed geometry** — measured from the new
    wall's start corner (the oracle the property tests check against)."""
    new_wall = xf.wall(wall)
    p1, p2 = _wall_span(wall, offset, width, rw, rl)
    q1 = xf.point(p1[0], p1[1], rw, rl)
    q2 = xf.point(p2[0], p2[1], rw, rl)
    # Along-wall coordinate on the new wall (x for N/S walls, y for E/W walls),
    # measured from that wall's SW start corner (which sits at 0 in the frame).
    if new_wall in (Direction.NORTH, Direction.SOUTH):
        new_offset = min(q1[0], q2[0])
    else:
        new_offset = min(q1[1], q2[1])
    return new_wall, new_offset


# --- the stamper --------------------------------------------------------------


def _pref(alias: str, name: str) -> str:
    return f"{alias}.{name}"


def _room_dims(src: Barndominium) -> dict[str, tuple[float, float]]:
    """Map each part room id to its ``(width, length)`` — the frame every
    room-local element (fixture, light, wall opening) is transformed within."""
    return {r.id: (r.width, r.length) for r in src.rooms}


def stamp_instance(plan: Barndominium, component: PartComponent, use: UseSpec) -> Instance:
    """Stamp a transformed, id-prefixed copy of ``component`` into ``plan``.

    Every element is rotated (ccw) then mirrored about the part's local bbox (7b),
    translated so the transformed bbox's SW corner lands at ``use.x, use.y``,
    lifted to ``use.level``, and has every id **and every internal reference**
    prefixed ``<alias>.``. Wall directions and wall offsets remap per §5 (offsets
    re-derived from the transformed geometry); fixture rotations compose. Appends
    the copies to ``plan`` and returns the :class:`Instance` describing them.
    """
    alias = use.alias
    dx, dy = float(use.x), float(use.y)
    lvl = int(use.level)
    src = component.plan
    xf = _Xform(int(use.rotate), use.mirror)
    pw, pl = component.width, component.length
    dims = _room_dims(src)
    objects: list = []
    room_ids: list[str] = []

    def world(px: float, py: float) -> tuple[float, float]:
        """A part-world point → host coords (transform about part bbox + place)."""
        tx, ty = xf.point(px, py, pw, pl)
        return (tx + dx, ty + dy)

    # Rooms: transform the rectangle about the part bbox, then place at `at`.
    src_rooms = {r.id: r for r in src.rooms}
    new_rooms: dict[str, Room] = {}
    for r in src.rooms:
        nx, ny, nw, nl = xf.rect(r.x, r.y, r.width, r.length, pw, pl)
        nr = dataclasses.replace(
            r, id=_pref(alias, r.id), x=nx + dx, y=ny + dy,
            width=nw, length=nl, level=r.level + lvl, placement=None,
        )
        plan.rooms.append(nr)
        objects.append(nr)
        room_ids.append(nr.id)
        new_rooms[r.id] = nr
    for d in src.interior_doors:
        # A door between part rooms is placed from room geometry at render/validate
        # time; only its `offset` (from the shared wall's low end) is stored, so it
        # is re-derived from the transformed shared edge (id-prefixing aside).
        new_offset = _xform_door_offset(d, src_rooms, new_rooms, world)
        nd = dataclasses.replace(
            d, room_a=_pref(alias, d.room_a), room_b=_pref(alias, d.room_b),
            offset=new_offset,
            swing_into=_pref(alias, d.swing_into) if d.swing_into is not None else None,
            line=None, col=None, end_col=None,
        )
        plan.interior_doors.append(nd)
        objects.append(nd)
    for xd in src.exterior_doors:
        rw, rl = dims.get(xd.room, (0.0, 0.0))
        nwall, noff = _remap_wall_offset(xf, xd.wall, xd.offset, xd.width, rw, rl)
        nxd = dataclasses.replace(xd, room=_pref(alias, xd.room), wall=nwall,
                                  offset=noff, line=None, col=None, end_col=None)
        plan.exterior_doors.append(nxd)
        objects.append(nxd)
    for w in src.windows:
        rw, rl = dims.get(w.room, (0.0, 0.0))
        nwall, noff = _remap_wall_offset(xf, w.wall, w.offset, w.width, rw, rl)
        nwin = dataclasses.replace(w, room=_pref(alias, w.room), wall=nwall,
                                   offset=noff, line=None, col=None, end_col=None)
        plan.windows.append(nwin)
        objects.append(nwin)
    for f in src.fixtures:
        nf = _xform_fixture(xf, f, alias, dims)
        plan.fixtures.append(nf)
        objects.append(nf)
    for o in src.outlets:
        rw, rl = dims.get(o.room, (0.0, 0.0))
        nwall, noff = _remap_wall_offset(xf, o.wall, o.offset, 0.0, rw, rl)
        no = dataclasses.replace(o, room=_pref(alias, o.room), wall=nwall,
                                 offset=noff, line=None, col=None, end_col=None)
        plan.outlets.append(no)
        objects.append(no)
    for sw in src.switches:
        rw, rl = dims.get(sw.room, (0.0, 0.0))
        nwall, noff = _remap_wall_offset(xf, sw.wall, sw.offset, 0.0, rw, rl)
        nsw = dataclasses.replace(sw, room=_pref(alias, sw.room), wall=nwall,
                                  offset=noff, line=None, col=None, end_col=None)
        plan.switches.append(nsw)
        objects.append(nsw)
    for lt in src.lights:
        rw, rl = dims.get(lt.room, (0.0, 0.0))
        lx, ly = xf.point(lt.x, lt.y, rw, rl)
        nlt = dataclasses.replace(lt, room=_pref(alias, lt.room), x=lx, y=ly,
                                  line=None, col=None, end_col=None)
        plan.lights.append(nlt)
        objects.append(nlt)
    for al in src.alarms:
        ax, ay = al.x, al.y
        if ax is not None and ay is not None:
            rw, rl = dims.get(al.room, (0.0, 0.0))
            ax, ay = xf.point(ax, ay, rw, rl)
        nal = dataclasses.replace(al, room=_pref(alias, al.room), x=ax, y=ay,
                                  line=None, col=None, end_col=None)
        plan.alarms.append(nal)
        objects.append(nal)
    for nm in src.note_marks:
        wx, wy = world(nm.x, nm.y)
        nnm = dataclasses.replace(nm, x=wx, y=wy, level=nm.level + lvl,
                                  line=None, col=None, end_col=None)
        plan.note_marks.append(nnm)
        objects.append(nnm)
    for p in src.porches:
        nx, ny, nw, nl = xf.rect(p.x, p.y, p.width, p.length, pw, pl)
        npr = dataclasses.replace(p, id=_pref(alias, p.id), x=nx + dx, y=ny + dy,
                                  width=nw, length=nl)
        plan.porches.append(npr)
        objects.append(npr)
    for st in src.stairs:
        # A multi-level part (Phase 20) may carry a `stair` connecting its own
        # `level 1` rooms to level 0. The footprint transforms like a room; the
        # from/to levels lift by the instance level so validation (connectivity,
        # egress, loft) sees the FINAL levels on the composed plan.
        stx, sty, stw, stl = xf.rect(st.x, st.y, st.width, st.length, pw, pl)
        nst = dataclasses.replace(
            st, id=_pref(alias, st.id), x=stx + dx, y=sty + dy, width=stw, length=stl,
            from_level=st.from_level + lvl, to_level=st.to_level + lvl,
            line=None, col=None, end_col=None,
        )
        plan.stairs.append(nst)
        objects.append(nst)
    for ws in src.wall_specs:
        nws = dataclasses.replace(ws, room_a=_pref(alias, ws.room_a),
                                  room_b=_pref(alias, ws.room_b), line=None, col=None, end_col=None)
        plan.wall_specs.append(nws)
        objects.append(nws)
    for s in src.suites:
        ns = dataclasses.replace(s, id=_pref(alias, s.id),
                                 members=tuple(_pref(alias, m) for m in s.members),
                                 line=None, col=None, end_col=None)
        plan.suites.append(ns)
        objects.append(ns)
    for z in src.zones:
        nz = dataclasses.replace(z, id=_pref(alias, z.id),
                                 members=tuple(_pref(alias, m) for m in z.members),
                                 line=None, col=None, end_col=None)
        plan.zones.append(nz)
        objects.append(nz)

    tw, tl = xf.dims(pw, pl)
    inst = Instance(
        alias=alias, relpath=use.relpath, part_path=component.path,
        x=dx, y=dy, level=lvl, mirror=use.mirror, rotate=int(use.rotate),
        bbox=(dx, dy, dx + tw, dy + tl),
        room_ids=room_ids, objects=objects,
        line=use.line, col=use.col, end_col=use.end_col,
    )
    return inst


def _xform_door_offset(
    door: InteriorDoor, src_rooms: dict[str, Room], new_rooms: dict[str, Room],
    world: Callable[[float, float], tuple[float, float]],
) -> float | None:
    """Re-derive an interior door's ``offset`` from the transformed shared wall.

    A ``None`` offset (centred) stays ``None`` — centring is transform-invariant.
    Otherwise the door's span endpoints are transformed and re-measured from the
    new shared edge's low end (the same S/W convention). If either room or the
    shared edge can't be resolved, the stored offset is kept unchanged.

    This maps the *authored* span, not the built one from
    :func:`~barndsl.geometry.door_span`, so a part door that runs off its wall
    still reports ``DOOR_OOB`` once stamped.
    """
    offset = door.offset
    if offset is None:
        return None
    sa = src_rooms.get(door.room_a)
    sb = src_rooms.get(door.room_b)
    na = new_rooms.get(door.room_a)
    nb = new_rooms.get(door.room_b)
    if sa is None or sb is None or na is None or nb is None:
        return offset
    edge = shared_edge(sa, sb)
    nedge = shared_edge(na, nb)
    if edge is None or nedge is None:
        return offset
    width = door.width
    if edge.orientation == "v":
        p1, p2 = (edge.pos, edge.lo + offset), (edge.pos, edge.lo + offset + width)
    else:
        p1, p2 = (edge.lo + offset, edge.pos), (edge.lo + offset + width, edge.pos)
    q1 = world(p1[0], p1[1])
    q2 = world(p2[0], p2[1])
    along = (q1[1], q2[1]) if nedge.orientation == "v" else (q1[0], q2[0])
    return min(along) - nedge.lo


def _xform_fixture(xf: _Xform, f: PlacedFixture, alias: str, dims: dict) -> PlacedFixture:
    """Transform an authored :class:`~barndsl.elements.PlacedFixture`.

    An ``at``-placed fixture's room-local anchor is the SW corner of its footprint,
    so the footprint rect is transformed (using the catalog width/depth, honouring
    the fixture's own ``rotate`` axis-swap) and its new SW corner taken; the
    fixture ``rotate`` composes with the instance transform and the wall backing
    remaps. Auto-placed fixtures (no ``at``) keep ``None`` x/y — they re-seed from
    the transformed room — with only wall/rotation remapped.
    """
    from .fixtures import FIXTURES, quarter_turns

    if f.along is not None:
        # An `along` counter run: remap the run's wall and its span. A full-wall run
        # (no from/to) stays a full-wall run on the transformed wall; a partial run's
        # start offset is re-derived from the transformed geometry (§5), exactly as a
        # door/window offset is, so the run lands on the remapped wall with the
        # remapped span. The along sugar is preserved (the part file keeps it).
        new_along = xf.wall(f.along)
        if f.run_from is None or f.run_to is None:
            return dataclasses.replace(
                f, room=_pref(alias, f.room), along=new_along,
                line=None, col=None, end_col=None,
            )
        rw, rl = dims.get(f.room, (0.0, 0.0))
        span = f.run_to - f.run_from
        _nw, new_from = _remap_wall_offset(xf, f.along, f.run_from, span, rw, rl)
        return dataclasses.replace(
            f, room=_pref(alias, f.room), along=new_along,
            run_from=new_from, run_to=new_from + span,
            line=None, col=None, end_col=None,
        )

    new_wall = xf.wall(f.wall) if f.wall is not None else None
    new_rot = xf.fixture_rotation(f.rotation)
    nx, ny = f.x, f.y
    new_off = f.offset
    if f.x is not None and f.y is not None:
        rw, rl = dims.get(f.room, (0.0, 0.0))
        spec = FIXTURES.get(f.kind)
        fw = float(f.width) if f.width else (spec.width if spec else 1.0)
        fd = spec.depth if spec else 1.0
        if quarter_turns(f.rotation) % 2 == 1:
            fw, fd = fd, fw
        bx, by, _bw, _bl = xf.rect(f.x, f.y, fw, fd, rw, rl)
        nx, ny = bx, by
    elif f.wall is not None and f.offset is not None:
        # A wall-pinned piece remaps exactly like a door/window: the offset is
        # re-derived from the transformed span (its along-wall extent is the
        # rotation-adjusted catalog width).
        rw, rl = dims.get(f.room, (0.0, 0.0))
        spec = FIXTURES.get(f.kind)
        fw = float(f.width) if f.width else (spec.width if spec else 1.0)
        if quarter_turns(f.rotation) % 2 == 1:
            fw = spec.depth if spec else 1.0
        _nw, new_off = _remap_wall_offset(xf, f.wall, f.offset, fw, rw, rl)
    return dataclasses.replace(
        f, room=_pref(alias, f.room), x=nx, y=ny, wall=new_wall,
        offset=new_off, rotation=new_rot, line=None, col=None, end_col=None,
    )


# --- the entry point ----------------------------------------------------------


def _prefix_part_internal(
    iss: Issue, component: PartComponent, use: UseSpec, prefix_room: bool
) -> Issue:
    """A copy of a part-internal diagnostic, chained with the part file and
    re-anchored (in the *current* buffer) to the ``use`` line that pulls the part
    in.

    Nesting (Phase 20) composes cleanly:

    * **Message** always prepends ``in part <relpath>:<line> — …``, so a finding
      that already came from a deeper part accumulates a readable chain
      (``in part p1.barn:5 — in part p2.barn:3 — <original>``).
    * **file/part** are *preserved* if already set — the **innermost** part (where
      the finding actually lives) keeps the attribution; only a finding straight
      from this part (``file is None``) is tagged with this part.
    * **room** is re-prefixed with the alias when ``prefix_room`` (i.e. this part
      is itself being composed into a parent), so a part's stored diagnostics
      carry that-part-frame ids and the parent's one-level dedup lines up.
    """
    part_line = iss.line
    where = f"{component.relpath}:{part_line}" if part_line else component.relpath
    new_room = _pref(use.alias, iss.room) if (prefix_room and iss.room) else iss.room
    return dataclasses.replace(
        iss,
        message=f"in part {where} — {iss.message}",
        file=iss.file if iss.file is not None else component.path,
        part=iss.part if iss.part is not None else component.relpath,
        room=new_room,
        line=use.line,
        col=use.col,
        end_col=use.end_col,
    )


def _param_undeclared(use: UseSpec, key: str, declared: dict[str, float]) -> Issue:
    """A ``PARAM_UNDECLARED`` for a ``with`` key the part doesn't declare, anchored
    to the ``use`` line (Phase 20)."""
    from .compiler import did_you_mean

    known = ", ".join(declared) if declared else "(none)"
    return Issue(
        Severity.ERROR, "PARAM_UNDECLARED",
        f"{did_you_mean(key, tuple(declared))}"
        f"part \"{use.relpath}\" declares no param '{key}'.",
        line=use.line, col=use.col, end_col=use.end_col,
        hint=f"The part's params are: {known}. Add `param {key} = <number>` to the "
        "part, or drop it from `with`.",
    )


def compose_uses(
    plan: Barndominium,
    base_dir: str | None,
    diagnostics: list[Issue],
    profile: object | None = None,
    *,
    ctx: object | None = None,
    self_path: str | None = None,
) -> Composition:
    """Resolve and stamp every ``use`` on ``plan`` (in place), appending
    resolution errors and (deduped) part-internal diagnostics to ``diagnostics``.

    Called by :func:`barndsl.compiler.compile_source` **before** validation, so the
    composed plan is validated as one building. Returns the :class:`Composition`
    the caller uses to reclassify the composed plan's stamped-room diagnostics.

    ``ctx`` (Phase 20) carries the nested-composition recursion state (sandbox
    root, depth, cycle stack, shared memo + instance budget) through a nested
    part's compile; anything else builds a fresh top-level context from
    ``base_dir``, with ``self_path`` (the file being compiled) on the cycle stack.
    A part being composed (``depth ≥ 1``) folds its parts' findings with the alias
    re-prefixed onto the room, so the parent's one-level dedup composes across
    depths.
    """
    if not isinstance(ctx, _ComposeCtx):
        ctx = _ComposeCtx.top_level(base_dir, self_path)
    prefix_room = ctx.depth >= 1
    comp = Composition()
    aliases_seen: set[str] = set()
    reported_parts: set[tuple] = set()

    for use in plan.uses:
        if use.alias in aliases_seen:
            diagnostics.append(Issue(
                Severity.ERROR, "USE_ALIAS_DUP",
                f"alias '{use.alias}' is already used by another `use` statement.",
                line=use.line, col=use.col, end_col=use.end_col,
                hint="Give each instance a unique alias (m, m2, bath_1, ...).",
            ))
            continue
        aliases_seen.add(use.alias)

        component, err = load_part(use, base_dir, ctx, profile)
        if err is not None:
            diagnostics.append(err)
            continue
        assert component is not None

        # Use-site params the part doesn't declare (PARAM_UNDECLARED) — anchored to
        # the use line, one per unknown key. The unknown key never resolves inside
        # the part, so the stamp is still valid; this is a teaching error.
        for key in use.params:
            if key not in component.plan.params:
                diagnostics.append(_param_undeclared(use, key, component.plan.params))

        # Part-internal diagnostics — reported once per (part, param-values),
        # anchored to the (first) use line, tagged with the part path. Keyed by
        # param values too, so two instances with different params each surface
        # their own param-dependent findings.
        report_key = _memo_key(component.path, use.params)
        keys = comp.part_keys.setdefault(component.path, set())
        for iss in component.diagnostics:
            keys.add((iss.code, iss.room or ""))
        if report_key not in reported_parts:
            reported_parts.add(report_key)
            for iss in component.diagnostics:
                diagnostics.append(_prefix_part_internal(iss, component, use, prefix_room))

        if component.has_errors:
            diagnostics.append(Issue(
                Severity.ERROR, "USE_PART_INVALID",
                f"part \"{use.relpath}\" doesn't compile cleanly on its own; "
                f"instance '{use.alias}' can't be stamped.",
                line=use.line, col=use.col, end_col=use.end_col,
                hint="Fix the part-internal error(s) reported above, then re-use it.",
            ))
            continue

        if ctx.budget[0] <= 0:
            diagnostics.append(_unresolved(use.relpath, "cap", use))
            continue
        ctx.budget[0] -= 1

        inst = stamp_instance(plan, component, use)
        plan.instances.append(inst)
        for rid in inst.room_ids:
            plan.stamped_rooms.add(rid)
            local = rid[len(use.alias) + 1:]
            comp.stamped_map[rid] = (local, inst)

    return comp


# --- part-file browsing (the playground Parts panel + the LSP part completions) --
#
# `scan_parts` lives here, beside the `use` loader, because both answer the same
# question — "what parts are available under this directory?" — and both key off
# the plan-less-`.barn`-is-a-part rule. The playground and `barndsl.lsp` both
# import it (the playground re-exports it for backward compatibility).

#: Cap on the number of part files a browse lists — a library, not a filesystem
#: crawl. A directory with more parts than this is trimmed (alphabetical).
MAX_LISTED_PARTS = 32

#: How many bytes of a candidate part to read when sniffing (a part file is small
#: — the loader caps whole parts at 256 KiB; the sniff only needs the statements).
MAX_PART_SNIFF_BYTES = 64 * 1024


def scan_parts(base_dir: str | None) -> list[dict]:
    """List plan-less ``.barn`` part files under ``base_dir`` (and ``base_dir/parts``).

    Cheap on purpose — each file is *sniffed*, not compiled: its statements are
    read line by line until the first keyword, and a file whose first statement is
    ``plan`` (a whole building, not a part) is skipped. ``rooms`` counts the
    ``room`` statement lines. Returns ``[{relpath, name, rooms}]`` sorted by
    relpath, capped at :data:`MAX_LISTED_PARTS`. ``None`` base_dir → ``[]``.
    """
    if not base_dir or not os.path.isdir(base_dir):
        return []
    seen: set[str] = set()
    out: list[dict] = []
    dirs = [(base_dir, "")]
    parts_sub = os.path.join(base_dir, "parts")
    if os.path.isdir(parts_sub):
        dirs.append((parts_sub, "parts/"))
    for folder, prefix in dirs:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".barn"):
                continue
            full = os.path.join(folder, name)
            if not os.path.isfile(full):
                continue
            relpath = prefix + name
            if relpath in seen:
                continue
            info = _sniff_part(full)
            if info is None:  # a full plan (has a `plan` header) — not a part
                continue
            seen.add(relpath)
            out.append({"relpath": relpath, "name": info[0], "rooms": info[1]})
            if len(out) >= MAX_LISTED_PARTS:
                return out
    return out


def scan_part_params(base_dir: str | None, relpath: str) -> list[str]:
    """The declared ``param`` names of the part at ``relpath`` (Phase 20), for the
    ``use ... with `` completion. Cheap and sandboxed: resolve under ``base_dir``
    (no escape) and *sniff* the ``param`` lines — never a compile, never a read
    outside the served folder. ``[]`` on any resolution/read failure."""
    if not base_dir or not relpath:
        return []
    root = os.path.realpath(base_dir)
    candidate, reason = _resolve(relpath, base_dir, root)
    if reason is not None or candidate is None:
        return []
    try:
        with open(candidate, encoding="utf-8") as fh:
            text = fh.read(MAX_PART_SNIFF_BYTES)
    except OSError:
        return []
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split(None, 1)[0].lower()
        if head == "plan":
            return []  # a whole building, not a part
        if head == "param":
            rest = line[len("param"):].strip()
            name = rest.split("=", 1)[0].strip().split()[0] if "=" in rest else ""
            if name and name not in names:
                names.append(name)
    return names


def _sniff_part(path: str) -> tuple[str, int] | None:
    """Sniff a ``.barn`` file without compiling it: ``(name, room_count)`` for a
    plan-less part, or ``None`` if it carries a ``plan`` header (a whole building)
    or can't be read. ``name`` is the file stem; ``room_count`` counts ``room``
    statement lines (0 means the file has no rooms — still listed, teaching that a
    part needs one)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(MAX_PART_SNIFF_BYTES)
    except OSError:
        return None
    rooms = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split(None, 1)[0].lower()
        if head == "plan":
            return None  # a whole-building file, not a part
        if head == "room":
            rooms += 1
    return (os.path.splitext(os.path.basename(path))[0], rooms)
