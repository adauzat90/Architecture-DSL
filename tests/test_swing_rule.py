"""One door-swing rule for the plan, the DXF, the validator and the 3D model.

A hinged leaf opens onto one side of its wall, and every consumer must agree
which (:func:`barndsl.drawing.swing_side`, :func:`~barndsl.drawing.inward_side`):

* an interior door that names an ``into`` room swings into it;
* one that doesn't swings toward ``+x``/``+y``, unless the leaf doesn't fit in
  the room on that side and does in the other;
* an exterior door swings into its own room.

Before this rule the drawing, the validator and the glTF each picked the
default side their own way. The drawing and the validator disagreed on 7 of the
10 whole-plan examples, and the 3D model disagreed with the drawing on all 10
(TD-16).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from barndsl import compile_source
from barndsl.drawing import door_symbols, swing_side
from barndsl.elements import Direction, Room, RoomType
from barndsl.geometry import shared_edge
from barndsl.gltf import build_scene

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(p for p in (REPO / "examples").glob("**/*.barn") if "parts" not in p.parts)


def _pair(a_depth: float, b_depth: float, horizontal: bool):
    # a and b share one wall; b is on its +x (vertical wall) or +y side.
    if horizontal:
        a = Room("a", RoomType.LIVING, 0, 0, 10, a_depth)
        b = Room("b", RoomType.BEDROOM, 0, a_depth, 10, b_depth)
    else:
        a = Room("a", RoomType.LIVING, 0, 0, a_depth, 10)
        b = Room("b", RoomType.BEDROOM, a_depth, 0, b_depth, 10)
    return a, b, shared_edge(a, b)


@pytest.mark.parametrize("horizontal", [False, True], ids=["vertical-wall", "horizontal-wall"])
@pytest.mark.parametrize(
    ("into", "a_depth", "b_depth", "leaf", "side"),
    [
        (None, 10, 10, 3.0, 1.0),  # default: toward +x/+y
        (None, 10, 2, 3.0, -1.0),  # the +x/+y room is too shallow: the other side
        (None, 3, 2, 3.0, -1.0),  # the other room only just fits it
        (None, 2, 2, 3.0, 1.0),  # fits neither side: keep +x/+y
        (None, 10, 3, 3.0, 1.0),  # exactly as deep as the leaf still fits
        ("a", 2, 10, 3.0, -1.0),  # an `into` room always wins
        ("b", 10, 2, 3.0, 1.0),
        ("zz", 10, 2, 3.0, -1.0),  # an `into` naming neither room is ignored
    ],
)
def test_interior_swing_side(horizontal, into, a_depth, b_depth, leaf, side):
    a, b, edge = _pair(a_depth, b_depth, horizontal)
    assert edge.orientation == ("h" if horizontal else "v")
    assert swing_side(into, a, b, edge, leaf) == side


def _plan(path: Path):
    return compile_source(path.read_text(encoding="utf-8"), base_dir=str(path.parent)).plan


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.relative_to(REPO).as_posix())
def test_the_3d_model_swings_every_leaf_the_way_the_plan_draws_it(path):
    plan = _plan(path)
    drawn = {}
    for sym in door_symbols(plan):
        for leaf in sym.leaves:
            key = (round(leaf.hinge[0], 3), round(leaf.hinge[1], 3), round(leaf.width, 3))
            drawn[key] = (leaf.tip[0] - leaf.hinge[0], leaf.tip[1] - leaf.hinge[1])
    records = [n.door for n in build_scene(plan).nodes if n.door and n.door["mode"] == "swing"]
    assert records
    for rec in records:
        key = (round(rec["hinge"][0], 3), round(rec["hinge"][1], 3), round(rec["width"], 3))
        assert key in drawn, rec  # every 3D leaf is one the plan draws, hinged alike
        tx, ty = drawn[key]
        assert rec["out"][0] * tx + rec["out"][1] * ty > 0, (rec["id"], rec["out"], drawn[key])


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.relative_to(REPO).as_posix())
def test_every_exterior_leaf_opens_into_its_room(path):
    # Including the entries beside a porch (hall_spine, homestead), which used to
    # be drawn swinging out over it.
    plan = _plan(path)
    for xd in plan.exterior_doors:
        room = plan.room(xd.room)
        if xd.kind == "overhead" or room is None:
            continue
        ox = room.x + xd.offset if xd.wall in (Direction.NORTH, Direction.SOUTH) else (
            room.x if xd.wall is Direction.WEST else room.x2)
        oy = room.y + xd.offset if xd.wall in (Direction.WEST, Direction.EAST) else (
            room.y if xd.wall is Direction.SOUTH else room.y2)
        (sym,) = [s for s in door_symbols(plan, room.level)
                  if s.kind == "swing" and s.jambs[0] == pytest.approx((ox, oy))]
        for leaf in sym.leaves:
            tx, ty = leaf.tip
            assert room.x - 1e-6 <= tx <= room.x2 + 1e-6, (xd, leaf)
            assert room.y - 1e-6 <= ty <= room.y2 + 1e-6, (xd, leaf)


SHALLOW = """\
plan "Shallow"
envelope 24 x 10
ceiling 9
room hall: hallway at 0,0  size 10 x 10
room clo:  closet  at 10,0 size 2 x 10
room bed:  bedroom at 12,0 size 12 x 10
door hall - clo width 2.5 offset 1
door clo - bed width 2 offset 6
entry hall south width 2.5 offset 7.5
window bed east width 4 offset 3
"""


def _leaf_out(plan, hinge):
    """The glTF swing record's ``out`` for the leaf hinged at ``hinge``."""
    (rec,) = [n.door for n in build_scene(plan).nodes
              if n.door and n.door["mode"] == "swing" and tuple(n.door["hinge"]) == hinge]
    return tuple(rec["out"])


def _codes(src: str) -> set[str]:
    return {d.code for d in compile_source(src).diagnostics}


def test_a_leaf_too_wide_for_the_closet_opens_into_the_hall_everywhere():
    plan = compile_source(SHALLOW).plan
    # Drawn: hinged at (10, 1), opening west into the hall — and so in 3D.
    (leaf,) = next(s for s in door_symbols(plan) if s.jambs[0] == (10.0, 1.0)).leaves
    assert leaf.tip == (7.5, 1.0)
    assert _leaf_out(plan, (10.0, 1.0)) == (-1.0, 0.0)

    codes = _codes(SHALLOW)
    # Validated there too: it now fouls the entry swinging in beside it…
    assert "DOOR_SWING_CLASH" in codes
    # …and no longer "fills the closet", because it doesn't swing into it.
    assert "CLOSET_DOOR_SWING" not in codes

    # Told to swing into the closet, it does fill it again.
    forced = _codes(SHALLOW.replace("door hall - clo width 2.5 offset 1",
                                    "door hall - clo width 2.5 offset 1 into clo"))
    assert "CLOSET_DOOR_SWING" in forced


PAIR = SHALLOW.replace("size 2 x 10", "size 3 x 10").replace("at 12,0 size 12", "at 13,0 size 11").replace(
    "door hall - clo width 2.5 offset 1", "door hall - clo double width 5 offset 1")


def test_a_pair_is_fitted_leaf_by_leaf():
    # Each half of a 5 ft pair is 2.5 ft, which fits the 3 ft closet, so the pair
    # keeps its default (east) side even though 5 ft would not fit.
    plan = compile_source(PAIR).plan
    near, far = next(s for s in door_symbols(plan) if s.jambs[0] == (10.0, 1.0)).leaves
    assert (near.tip, far.tip) == ((12.5, 1.0), (12.5, 6.0))
    assert _leaf_out(plan, (10.0, 1.0)) == _leaf_out(plan, (10.0, 6.0)) == (1.0, 0.0)
    # The validator checks it on that side too: clear of the hall entry, which
    # it would foul if it opened into the hall.
    assert "DOOR_SWING_CLASH" not in _codes(PAIR)
    assert "DOOR_SWING_CLASH" in _codes(PAIR.replace("double width 5 offset 1",
                                                     "double width 5 offset 1 into hall"))


NORTH = """\
plan "North"
envelope 20 x 10
ceiling 9
room a: living  at 0,0  size 12 x 10
room b: kitchen at 12,0 size 8 x 10
door a - b width 3 offset 6.5 into a
entry a north width 3 offset 8.5
entry a south width 3 offset 2
"""


def test_an_exterior_door_is_checked_swinging_into_its_room():
    # The north entry swings south into the living room, where it meets the
    # kitchen door swinging west into the same corner. Checked swinging out,
    # the two could never meet.
    plan = compile_source(NORTH).plan
    (leaf,) = next(s for s in door_symbols(plan) if s.jambs[0] == (8.5, 10.0)).leaves
    assert leaf.tip == (8.5, 7.0)
    assert "DOOR_SWING_CLASH" in _codes(NORTH)


LEVELS = """\
plan "Levels"
envelope 10 x 10
ceiling 9
room a: living  at 0,0 size 10 x 10
room c: office  at 0,0 size 10 x 4 level 1
room d: bedroom at 0,4 size 10 x 6 level 1
door c - d width 3 offset 2 into c
entry a south width 3 offset 2
"""


def test_doors_on_different_levels_never_clash():
    # The upstairs door's swing lies right above the entry's, a storey apart.
    plan = compile_source(LEVELS).plan
    entry, upstairs = sorted((s.leaves[0] for s in door_symbols(plan)), key=lambda lf: lf.hinge[1])
    assert entry.hinge == (2.0, 0.0) and upstairs.hinge == (2.0, 4.0)
    assert "DOOR_SWING_CLASH" not in _codes(LEVELS)
