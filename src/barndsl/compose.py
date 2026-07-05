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

#: Depth-1 in v1: parts can't ``use``. Instance count and file-size guards keep a
#: pathological plan from turning into a resource problem (each is a teaching
#: diagnostic, never a crash — see §8).
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
    "ROOM_SIZE", "ROOM_TIGHT", "ROOM_PROPORTION", "ROOM_GEOMETRY", "ROOM_CLEAR",
    "BEDROOM_AREA", "BEDROOM_DIM", "CLOSET_SHAPE", "BATH_CLEARANCE",
    "BATH_OVERSIZE", "OPEN_BATH", "BATH_DISTANCE",
    # fixtures & furniture clearances (room-local)
    "FIXTURE_TOILET_CLEARANCE", "FIXTURE_FRONT", "FIXTURE_BACKING",
    "FIXTURE_DOOR", "FIXTURE_OVERLAP", "FIXTURE_OOB", "FIXTURE_ROOM",
    "FIXTURE_ROOM_TYPE", "FIXTURE_EGRESS", "BED_CLEARANCE", "DINING_CLEARANCE",
    "KITCHEN_TRIANGLE", "KITCHEN_FIT", "RANGE_LANDING", "DRYER_VENT",
    # openings that clash with the part's own geometry/fixtures
    "OPENING_CLASH", "DOOR_HITS_FIXTURE", "DOOR_SWING_CLASH",
    # room overlaps within the part
    "OVERLAP",
    # devices (room-local rules)
    "OUTLET_SPACING", "OUTLET_GFCI", "ROOM_NO_LIGHT",
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
    #: the part, without the ``<alias>.`` prefix).
    stamped_map: dict[str, tuple[str, Instance]] = field(default_factory=dict)
    #: Resolved part path -> the set of ``(code, local_id)`` its fragment compile
    #: reported — the keys a composed stamped-room diagnostic is deduped against.
    part_keys: dict[str, set[tuple[str, str]]] = field(default_factory=dict)


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
    """Resolve ``relpath`` against the sandbox ``root``. Returns
    ``(resolved_path, None)`` or ``(None, reason)`` where ``reason`` is one of
    ``no_base_dir`` / ``absolute`` / ``escape`` / ``missing`` / ``too_big``."""
    if base_dir is None or root is None:
        return None, "no_base_dir"
    if not relpath or os.path.isabs(relpath):
        return None, "absolute"
    candidate = os.path.realpath(os.path.join(root, relpath))
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


def load_part(
    use: UseSpec,
    base_dir: str | None,
    root: str | None,
    memo: dict[str, PartComponent],
    profile: object | None,
) -> tuple[PartComponent | None, Issue | None]:
    """Resolve + fragment-compile the part named by ``use`` (memoized).

    Returns ``(component, None)`` on success or ``(None, issue)`` for an
    unresolvable path (a ``USE_UNRESOLVED`` anchored to the ``use`` line). The
    part is compiled at most once per resolved path per top-level compile.
    """
    from .compiler import compile_source

    candidate, reason = _resolve(use.relpath, base_dir, root)
    if reason is not None:
        return None, _unresolved(use.relpath, reason, use)
    assert candidate is not None
    if candidate in memo:
        return memo[candidate], None
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
    memo[candidate] = component
    return component, None


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
    from .fixtures import FIXTURES, _quarter_turns

    new_wall = xf.wall(f.wall) if f.wall is not None else None
    new_rot = xf.fixture_rotation(f.rotation)
    nx, ny = f.x, f.y
    if f.x is not None and f.y is not None:
        rw, rl = dims.get(f.room, (0.0, 0.0))
        spec = FIXTURES.get(f.kind)
        fw = float(f.width) if f.width else (spec.width if spec else 1.0)
        fd = spec.depth if spec else 1.0
        if _quarter_turns(f.rotation) % 2 == 1:
            fw, fd = fd, fw
        bx, by, _bw, _bl = xf.rect(f.x, f.y, fw, fd, rw, rl)
        nx, ny = bx, by
    return dataclasses.replace(
        f, room=_pref(alias, f.room), x=nx, y=ny, wall=new_wall,
        rotation=new_rot, line=None, col=None, end_col=None,
    )


# --- the entry point ----------------------------------------------------------


def _prefix_part_internal(iss: Issue, component: PartComponent, use: UseSpec) -> Issue:
    """A copy of a part-internal diagnostic, tagged with the part file and anchored
    (in the host buffer) to the first ``use`` line that pulls the part in."""
    part_line = iss.line
    where = f"{component.relpath}:{part_line}" if part_line else component.relpath
    return dataclasses.replace(
        iss,
        message=f"in part {where} — {iss.message}",
        file=component.path,
        part=component.relpath,
        line=use.line,
        col=use.col,
        end_col=use.end_col,
    )


def compose_uses(
    plan: Barndominium,
    base_dir: str | None,
    diagnostics: list[Issue],
    profile: object | None = None,
) -> Composition:
    """Resolve and stamp every ``use`` on ``plan`` (in place), appending
    resolution errors and (deduped) part-internal diagnostics to ``diagnostics``.

    Called by :func:`barndsl.compiler.compile_source` **before** validation, so the
    composed plan is validated as one building. Returns the :class:`Composition`
    the caller uses to reclassify the composed plan's stamped-room diagnostics.
    """
    root = os.path.realpath(base_dir) if base_dir is not None else None
    memo: dict[str, PartComponent] = {}
    comp = Composition()
    aliases_seen: set[str] = set()
    reported_parts: set[str] = set()
    total = 0

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

        component, err = load_part(use, base_dir, root, memo, profile)
        if err is not None:
            diagnostics.append(err)
            continue
        assert component is not None

        # Part-internal diagnostics — reported once per part file, anchored to the
        # (first) use line, tagged with the part path.
        if component.path not in reported_parts:
            reported_parts.add(component.path)
            keys: set[tuple[str, str]] = set()
            for iss in component.diagnostics:
                keys.add((iss.code, iss.room or ""))
                diagnostics.append(_prefix_part_internal(iss, component, use))
            comp.part_keys[component.path] = keys

        if component.has_errors:
            diagnostics.append(Issue(
                Severity.ERROR, "USE_PART_INVALID",
                f"part \"{use.relpath}\" doesn't compile cleanly on its own; "
                f"instance '{use.alias}' can't be stamped.",
                line=use.line, col=use.col, end_col=use.end_col,
                hint="Fix the part-internal error(s) reported above, then re-use it.",
            ))
            continue

        if total >= MAX_INSTANCES:
            diagnostics.append(_unresolved(use.relpath, "cap", use))
            continue

        inst = stamp_instance(plan, component, use)
        plan.instances.append(inst)
        for rid in inst.room_ids:
            plan.stamped_rooms.add(rid)
            local = rid[len(use.alias) + 1:]
            comp.stamped_map[rid] = (local, inst)
        total += 1

    return comp
