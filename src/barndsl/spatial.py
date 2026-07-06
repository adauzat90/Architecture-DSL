"""A uniform-grid spatial index over room rectangles — the O(n²) killer.

Validation, wall-body extraction and the cost takeoff all ask the same pairwise
question: *which rooms touch or overlap which?* Answered by the naive nested
loop that is ``for i, a: for b in rooms[i+1:]``, it is Θ(n²) — fine at thirty
rooms, 115 s at ten thousand (see ROADMAP Phase 17). Two rooms can only share a
wall or overlap when their (axis-aligned) bounding boxes come within a hair of
each other, so a coarse grid that buckets rooms by cell turns "all pairs" into
"pairs that share a cell", which is Θ(n) for the evenly-spread plans real
buildings (and generated fixtures) are.

The index is a **candidate generator**, never an oracle: it returns a *superset*
of the truly adjacent/overlapping pairs, and the caller still runs the exact
:func:`~barndsl.geometry.shared_edge` / :meth:`~barndsl.elements.Room.overlaps`
test on each. Crucially it hands those candidates back in the *same order* the
old nested loop visited them — ascending ``(i, j)`` over the room list — so every
diagnostic, wall band and summed length comes out byte-for-byte unchanged.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .elements import Barndominium, Room

#: Cell-registration slop (feet). Rooms sharing a wall touch to within the
#: geometry tolerance (``geometry.TOL`` = 1e-6); expanding each rect by this
#: (much larger) margin before bucketing guarantees two touching rects always
#: land in a common cell, whatever a floor-boundary coincidence would otherwise
#: do. Far below any real dimension, so it never merges genuinely separate rooms.
_EXPAND = 1e-3

#: Floor on the grid cell size (feet), so a degenerate plan of hairline rooms
#: can't drive the cell count — and the per-room cell span — toward infinity.
_MIN_CELL = 1.0


def _finite_rect(r: "Room") -> bool:
    """True when a room has finite position and size — the only rooms that can
    actually touch or overlap another (a nan/inf coordinate defeats every
    comparison, so such a room is simply left out of the index, exactly as the
    old loops' ``overlaps``/``shared_edge`` calls returned nothing for it)."""
    return (
        math.isfinite(r.x)
        and math.isfinite(r.y)
        and math.isfinite(r.width)
        and math.isfinite(r.length)
    )


class RoomIndex:
    """A uniform grid over a room list, keyed by ``(level, cell_x, cell_y)``.

    Build once, then ask for :meth:`candidate_pairs` (replacing an all-pairs
    loop) or :meth:`candidates_near` (replacing a per-room full scan). Indices in
    the results refer to positions in the ``rooms`` sequence handed to the
    constructor.
    """

    def __init__(self, rooms: Sequence["Room"]) -> None:
        self._rooms = rooms
        dims: list[float] = []
        for r in rooms:
            if not _finite_rect(r):
                continue
            if r.width > 0:
                dims.append(r.width)
            if r.length > 0:
                dims.append(r.length)
        # Cell ~ a typical room, so each room spans O(1) cells and each cell
        # holds O(1) rooms. The median is robust to a stray giant/tiny room.
        self._cell = max(_MIN_CELL, float(median(dims))) if dims else _MIN_CELL
        buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        # First-match id → index, so a per-id lookup is O(1) instead of the linear
        # ``plan.room()`` scan. ``setdefault`` keeps the *first* room of a duplicate
        # id, matching ``plan.room``'s first-wins semantics for DUP_ID plans.
        first_by_id: dict[str, int] = {}
        for i, r in enumerate(rooms):
            first_by_id.setdefault(r.id, i)
            if not _finite_rect(r):
                continue
            for key in self._cells(r):
                buckets[key].append(i)
        self._first_by_id = first_by_id
        # Within each bucket the indices are ascending: rooms are inserted in
        # list order, so ``(members[a], members[b])`` with a < b is already
        # ``(lo, hi)`` — the canonical pair order the old loops emitted.
        self._buckets = buckets

    def _cells(self, r: "Room"):
        cell = self._cell
        level = getattr(r, "level", 0)
        x0 = math.floor((r.x - _EXPAND) / cell)
        x1 = math.floor((r.x + r.width + _EXPAND) / cell)
        y0 = math.floor((r.y - _EXPAND) / cell)
        y1 = math.floor((r.y + r.length + _EXPAND) / cell)
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                yield (level, cx, cy)

    def candidate_pairs(self) -> list[tuple[int, int]]:
        """Every ``(i, j)`` with ``i < j`` whose rooms *might* touch or overlap,
        in ascending order — a superset of the truly adjacent/overlapping pairs,
        visited in the exact order ``for i, a: for b in rooms[i+1:]`` would."""
        pairs: set[tuple[int, int]] = set()
        for members in self._buckets.values():
            n = len(members)
            if n < 2:
                continue
            for a in range(n):
                ia = members[a]
                for b in range(a + 1, n):
                    pairs.add((ia, members[b]))
        return sorted(pairs)

    def first_index(self, room_id: str) -> int | None:
        """Index of the first room with ``room_id`` (``plan.room``'s first-wins
        lookup), or ``None`` — O(1)."""
        return self._first_by_id.get(room_id)

    def candidates_near(self, room: "Room") -> list[int]:
        """Sorted indices of rooms whose cells intersect ``room``'s — a superset
        of the rooms that share a wall with it, in room-list order. ``room``'s
        own index (if it is in the list) may be present; callers filter it."""
        if not _finite_rect(room):
            return []
        cand: set[int] = set()
        buckets = self._buckets
        for key in self._cells(room):
            members = buckets.get(key)
            if members:
                cand.update(members)
        return sorted(cand)


def room_index(plan: "Barndominium") -> RoomIndex:
    """The plan's shared room index over ``plan.rooms``, built lazily and reused.

    The cache key is ``(id(rooms_list), len(rooms_list))`` — O(1), so a per-room
    caller (``geometric_neighbors`` inside ``_validate_access``) can ask for the
    index thousands of times without paying to rebuild or re-fingerprint it, the
    difference between O(n) and O(n²) at ten thousand rooms.

    That key deliberately does **not** fingerprint room geometry, which is safe
    here: the one place a compile mutates a room's size in place is the Phase 10
    dimension clamp in :func:`validation._check_dimensions`, and it runs *before*
    any consumer first asks for the index — so the index is built from
    already-clamped geometry, and nothing downstream moves or resizes a room.
    Rebuilding a *different* plan (or a recompiled one) hits a fresh rooms list
    (new id, or new length), so it rebuilds; and the cache lives on the plan, so
    id reuse across plans can't cross-contaminate.
    """
    rooms = plan.rooms
    key = (id(rooms), len(rooms))
    cache = getattr(plan, "_room_index_cache", None)
    if cache is not None and cache[0] == key:
        return cache[1]
    index = RoomIndex(rooms)
    # A private, behaviour-free memo — set via setattr so it stays off the
    # dataclass's fields (no effect on ==, repr, emit or serialisation).
    setattr(plan, "_room_index_cache", (key, index))
    return index
