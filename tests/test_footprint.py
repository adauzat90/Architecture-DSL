"""Tests for rectilinear footprints — L/T/U buildings via ``wing`` blocks."""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl
from barndsl.elements import RoomType as T
from barndsl.geometry import (
    footprint_area,
    footprint_boundary,
    point_in_footprint,
    rect_in_footprint,
)
from barndsl.validation import exterior_walls, validate

# An L-shaped plan: 40×40 main block + a 22×20 east wing (primary suite).
L_SRC = """\
plan "L Test"
envelope 40 x 40
wing 22 x 20 at 40,0
ceiling 10
room living:  living   at 0,0   size 24 x 22
room kitchen: kitchen  at 24,0  size 16 x 22
room hall:    hallway  at 0,22  size 40 x 6
room bed1:    bedroom  at 0,28   size 20 x 12
room bed2:    bedroom  at 20,28  size 20 x 12
room master:  bedroom  at 40,0  size 22 x 20
door living - kitchen width 6
door living - hall width 3
door hall - bed1 width 2.67
door hall - bed2 width 2.67
door kitchen - master width 2.67
entry living south width 3 offset 8
window bed1 north width 6 offset 6
window bed2 north width 6 offset 6
window master east width 8 offset 6
window living south width 10 offset 6
window kitchen south width 9 offset 3
"""


# --- pure geometry ----------------------------------------------------------


def test_footprint_area_is_the_union_not_the_bbox():
    L = [(0, 0, 40, 40), (40, 0, 22, 20)]
    assert footprint_area(L) == 40 * 40 + 22 * 20  # 2040, not 62*40=2480


def test_rect_in_footprint_detects_the_notch():
    L = [(0, 0, 40, 40), (40, 0, 20, 20)]
    assert rect_in_footprint(L, 0, 0, 30, 20)  # inside main
    assert rect_in_footprint(L, 30, 0, 20, 20)  # straddles the seam, still covered
    assert not rect_in_footprint(L, 40, 20, 20, 20)  # the empty NE notch
    assert not rect_in_footprint(L, -5, 0, 10, 10)  # negative coordinate
    assert not point_in_footprint(L, 50, 30)  # a point in the notch


def test_footprint_boundary_is_closed():
    # Every outline vertex should have matched horizontal/vertical edges.
    L = [(0, 0, 40, 40), (40, 0, 20, 20)]
    edges = footprint_boundary(L)
    assert edges
    # total horizontal edge length == total vertical edge length-pairs balance:
    from collections import Counter

    deg = Counter()
    for a, b in edges:
        deg[a] += 1
        deg[b] += 1
    assert all(v % 2 == 0 for v in deg.values())  # closed polygon: even degree


# --- IR / builder -----------------------------------------------------------


def test_wing_builder_sets_union_and_bounds():
    plan = barndominium("B").envelope(40, 40).ceiling(10).wing(22, 20, x=40, y=0)
    assert plan.envelope_width == 40  # primary block, not bbox
    assert plan.bounds() == (0, 0, 62, 40)
    assert plan.footprint_area == 2040
    assert plan.footprint_sections() == [(0, 0, 40, 40), (40, 0, 22, 20)]


def test_t_and_u_footprint_areas():
    t = barndominium("T").envelope(20, 40).ceiling(9).wing(60, 20, x=0, y=40)
    # stem 20×40 + crossbar 60×20 across the top — union = 800+1200
    assert t.footprint_area == 800 + 1200
    u = (
        barndominium("U").envelope(60, 15).ceiling(9)
        .wing(15, 40, x=0, y=15)
        .wing(15, 40, x=45, y=15)
    )
    assert u.footprint_area == 900 + 600 + 600


# --- parse / emit -----------------------------------------------------------


def test_wing_parses_and_round_trips():
    r = compile_source(L_SRC)
    assert not r.errors, r.report()
    assert [w.as_tuple() for w in r.plan.wings] == [(40, 0, 22, 20)]  # (x, y, w, l)
    emitted = emit_dsl(r.plan)
    assert "wing 22 x 20 at 40,0" in emitted
    # idempotent round-trip
    assert emit_dsl(compile_source(emitted).plan) == emitted


# --- validation -------------------------------------------------------------


def test_l_shaped_plan_compiles_clean():
    r = compile_source(L_SRC)
    assert not r.errors, r.report()
    # the suite in the wing reaches past the primary envelope and is still valid
    master = next(x for x in r.plan.rooms if x.id == "master")
    assert master.x2 == 62 > r.plan.envelope_width


def test_room_in_the_notch_is_out_of_bounds():
    src = (
        'plan "Notch"\nenvelope 40 x 40\nwing 20 x 20 at 40,0\nceiling 10\n'
        "room a: living at 0,0 size 30 x 20\n"
        "room b: bedroom at 40,20 size 20 x 20\n"  # the empty NE notch
        "entry a south width 3 offset 5\n"
    )
    r = compile_source(src)
    assert any(d.code == "OUT_OF_BOUNDS" for d in r.errors), r.report()


def test_disconnected_wing_is_rejected():
    src = (
        'plan "Split"\nenvelope 30 x 30\nwing 20 x 20 at 50,0\nceiling 10\n'
        "room a: living at 0,0 size 30 x 30\nentry a south width 3 offset 5\n"
    )
    r = compile_source(src)
    assert any(d.code == "FOOTPRINT_SPLIT" for d in r.errors), r.report()


def test_corner_touch_does_not_connect():
    # Wing touches the main block only at the corner (30,30) — not a shared wall.
    src = (
        'plan "Corner"\nenvelope 30 x 30\nwing 20 x 20 at 30,30\nceiling 10\n'
        "room a: living at 0,0 size 30 x 30\nentry a south width 3 offset 5\n"
    )
    r = compile_source(src)
    assert any(d.code == "FOOTPRINT_SPLIT" for d in r.errors), r.report()


def test_nonpositive_wing_is_rejected():
    plan = barndominium("Z").envelope(30, 30).ceiling(9).wing(0, 20, x=30, y=0)
    plan.add_room("a", T.LIVING, x=0, y=0, width=30, length=30)
    report = validate(plan)
    assert any(d.code == "WING_SIZE" for d in report.errors)


def test_exterior_walls_exclude_interior_seams():
    plan = compile_source(L_SRC).plan
    walls = {r.id: {w.value for w in exterior_walls(plan, r)} for r in plan.rooms}
    # master is in the east wing: its west wall is the seam to the main block.
    assert "west" not in walls["master"]
    assert {"east", "north", "south"} <= walls["master"]
    # the hall's east wall faces the L-notch (outside) — it IS exterior.
    assert "east" in walls["hall"]


# --- the plain rectangle path is unchanged ----------------------------------


def test_rectangular_plan_has_no_wings_and_uses_bbox_area():
    plan = barndominium("R").envelope(60, 40).ceiling(9)
    assert plan.wings == []
    assert plan.footprint_sections() == [(0, 0, 60, 40)]
    assert plan.footprint_area == 2400
    assert plan.bounds() == (0, 0, 60, 40)
