"""Phase 17 — the compiler must scale near-linearly, not O(n²).

A big generated plan used to compile in quadratic time: 5x the rooms cost ~25x
the wall-clock (the developer persona's 10 000-line plan took ~115 s). The fix is
one shared spatial index (:mod:`barndsl.spatial`) that turns the all-pairs room
loops — overlap, shared-edge discovery, geometric neighbours — into
candidate-pair lookups.

These tests pin the new curve two ways that don't flake on a loaded CI box.
The primary guard is a *deterministic operation count*: the index's candidate
pairs (the real driver of the old blow-up) grow ~linearly with room count, so an
accidental return to O(n²) is caught without timing anything. A generous
wall-clock ceiling backs it up, and a determinism test compiles the same big
plan twice and demands identical diagnostics.
"""

from __future__ import annotations

import time

from barndsl import compile_source
from barndsl.elements import Room, RoomType
from barndsl.geometry import shared_edge
from barndsl.spatial import RoomIndex


def _grid_rooms(cols: int, rows: int, side: float = 12.0) -> list[Room]:
    """A ``cols x rows`` grid of edge-to-edge rooms (every interior room shares a
    wall with four neighbours) — the shape that used to trip the O(n²) loops."""
    return [
        Room(
            id=f"r{r}_{c}",
            type=RoomType.LIVING,
            x=c * side,
            y=r * side,
            width=side,
            length=side,
        )
        for r in range(rows)
        for c in range(cols)
    ]


def _grid_source(cols: int, rows: int, side: int = 12) -> str:
    lines = ['plan "Grid"', f"envelope {cols * side} x {rows * side}"]
    for r in range(rows):
        for c in range(cols):
            lines.append(f"room r{r}_{c}: living at {c * side},{r * side} size {side} x {side}")
    lines.append("entry r0_0 south width 3 offset 4")
    return "\n".join(lines)


def test_candidate_pairs_scale_linearly_not_quadratically():
    # Doubling the rooms should ~double the candidate pairs, never quadruple
    # them. The margin (2.5x for a 2x room count) sits comfortably above the
    # linear ~2.0x and far below the ~4.0x an O(n²) index would produce.
    small = RoomIndex(_grid_rooms(20, 20))  # 400 rooms
    big = RoomIndex(_grid_rooms(40, 20))  # 800 rooms (2x)
    n_small = len(small.candidate_pairs())
    n_big = len(big.candidate_pairs())
    assert n_small > 0
    assert n_big / n_small <= 2.5


def test_candidate_pairs_are_a_superset_of_true_adjacencies():
    # The index is a candidate generator: it may over-offer, but it must never
    # miss a real shared wall, or a diagnostic/wall band would silently vanish.
    rooms = _grid_rooms(6, 5)
    index = RoomIndex(rooms)
    candidates = set(index.candidate_pairs())
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            if shared_edge(rooms[i], rooms[j]) is not None:
                assert (i, j) in candidates


def test_candidate_pairs_preserve_ascending_order():
    # The old nested loop visited pairs in ascending (i, j) order; diagnostics,
    # wall bands and summed lengths all depend on that order surviving.
    index = RoomIndex(_grid_rooms(5, 4))
    pairs = index.candidate_pairs()
    assert pairs == sorted(pairs)
    assert all(i < j for i, j in pairs)


def test_large_plan_compiles_well_under_a_generous_ceiling():
    # ~2000 edge-to-edge rooms, all but one unreachable — the exact shape whose
    # per-room neighbour scan was quadratic. It runs in a fraction of a second;
    # the 5 s ceiling tolerates a heavily loaded CI box without flaking. The old
    # path took ~5 s here and ~115 s at 10k rooms.
    src = _grid_source(45, 45)  # 2025 rooms
    start = time.perf_counter()
    result = compile_source(src)
    elapsed = time.perf_counter() - start
    assert result.plan is not None
    assert elapsed < 5.0


def test_compile_is_deterministic_across_two_runs():
    # The same plan compiled twice in one process yields byte-identical
    # diagnostics (order, codes, messages, locations, hints).
    src = _grid_source(20, 20)
    first = compile_source(src).diagnostics
    second = compile_source(src).diagnostics

    def fingerprint(diags):
        return [
            (d.severity.value, d.code, d.line, d.col, d.end_col, d.message, d.hint, d.room)
            for d in diags
        ]

    assert fingerprint(first) == fingerprint(second)
