"""Tests for the Revit-shaped exchange (`barndsl.revit`).

These exercise the pure-Python lowering — no Revit needed. The invariants the
pyRevit consumer depends on: walls dedupe and the exterior set traces the
footprint perimeter; every opening lands on a real wall at the right line;
interior doors host on interior walls and exterior openings on exterior walls;
room seeds fall inside their rectangles; levels and structure carry through; and
the JSON document round-trips.
"""

from __future__ import annotations

import json
import os

import pytest

from barndsl import compile_source, emit_dsl, to_revit_json, to_revit_model
from barndsl.geometry import TOL
from barndsl.revit import EXCHANGE_SCHEMA

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _compile_example(rel: str):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        src = fh.read()
    result = compile_source(src)
    assert result.plan is not None, f"{rel} failed to compile to a plan"
    return result.plan


def _wall_len(w) -> float:
    return abs(w.end[0] - w.start[0]) + abs(w.end[1] - w.start[1])


def _opening_on_wall(o, wall) -> bool:
    lo, hi = wall.span
    if wall.orientation == "v":
        return abs(o.location[0] - wall.const_coord) < 1e-4 and lo - TOL <= o.location[1] <= hi + TOL
    return abs(o.location[1] - wall.const_coord) < 1e-4 and lo - TOL <= o.location[0] <= hi + TOL


# --- basic structure ---------------------------------------------------------


def test_exchange_header_and_units():
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    doc = model.to_dict()
    assert doc["schema"] == EXCHANGE_SCHEMA
    assert doc["units"] == "feet"
    assert doc["plan"]["name"]
    assert doc["plan"]["ceiling_height"] == 12


def test_levels_match_plan_levels():
    plan = _compile_example("gallery/two_story.barn")
    model = to_revit_model(plan)
    assert [l.index for l in model.levels] == plan.levels()
    # Names are 1-based; elevations stack by floor-to-floor (ceiling + the
    # inter-floor assembly), not the bare ceiling height.
    assert model.levels[0].name == "Level 1"
    assert model.levels[0].elevation == 0.0
    assert model.levels[1].elevation == pytest.approx(plan.floor_to_floor)
    assert plan.floor_to_floor > plan.ceiling_height


# --- walls -------------------------------------------------------------------


def test_exterior_walls_trace_the_perimeter():
    """For a fully-tiled rectangular envelope the exterior walls sum to 2(W+L)."""
    plan = _compile_example("cedar_ridge.barn")
    model = to_revit_model(plan)
    ext = [w for w in model.walls if w.exterior]
    perimeter = 2 * (plan.envelope_width + plan.envelope_length)
    assert sum(_wall_len(w) for w in ext) == pytest.approx(perimeter)


def test_walls_are_deduplicated_and_nonzero():
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    assert model.walls, "expected walls"
    seen = set()
    for w in model.walls:
        assert _wall_len(w) > TOL, "zero-length wall"
        key = (round(w.start[0], 6), round(w.start[1], 6), round(w.end[0], 6), round(w.end[1], 6), w.level)
        assert key not in seen, "duplicate (coincident) wall emitted"
        seen.add(key)


def test_walls_partition_between_two_rooms_is_interior():
    # cottage: living and kitchen are stacked, so their shared edge is one
    # interior wall (not two), and it is not flagged exterior.
    model = to_revit_model(_compile_example("gallery/cottage.barn"))
    interior = [w for w in model.walls if not w.exterior]
    assert interior, "expected interior partition walls"
    assert all(not w.exterior for w in interior)


def test_walls_are_per_level():
    model = to_revit_model(_compile_example("gallery/two_story.barn"))
    levels = {w.level for w in model.walls}
    assert levels == {0, 1}
    # The lone loft on level 1 is a rectangle → four walls. Two sit on the
    # building envelope (exterior); the other two face the open footprint below
    # (the great room it overlooks), so they classify as interior, not shell.
    lvl1 = [w for w in model.walls if w.level == 1]
    assert len(lvl1) == 4
    assert sum(1 for w in lvl1 if w.exterior) == 2


# --- openings ----------------------------------------------------------------


def test_every_opening_hosts_on_a_real_wall_at_its_line():
    for rel in ("cedar_ridge.barn", "gallery/cottage.barn", "gallery/two_story.barn"):
        model = to_revit_model(_compile_example(rel))
        by_id = {w.id: w for w in model.walls}
        assert model.openings, f"{rel}: expected openings"
        for o in model.openings:
            assert o.host_wall is not None, f"{rel}: {o.id} ({o.kind}) found no host wall"
            wall = by_id[o.host_wall]
            assert wall.level == o.level
            assert _opening_on_wall(o, wall), f"{rel}: {o.id} not on host wall {o.host_wall}"


def test_interior_doors_host_on_interior_walls():
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    by_id = {w.id: w for w in model.walls}
    interior_openings = [o for o in model.openings if not o.exterior]
    assert interior_openings
    for o in interior_openings:
        assert not by_id[o.host_wall].exterior, f"{o.id} hosted on an exterior wall"


def test_exterior_openings_host_on_exterior_walls():
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    by_id = {w.id: w for w in model.walls}
    exterior_openings = [o for o in model.openings if o.exterior]
    assert exterior_openings
    for o in exterior_openings:
        assert by_id[o.host_wall].exterior, f"{o.id} hosted on an interior wall"


def test_window_sill_and_height_carry_through():
    # window great_room west sill 3 head 6.67 (defaults) → height ~3.67, sill 3.
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    windows = [o for o in model.openings if o.category == "window"]
    assert windows
    for w in windows:
        assert w.sill == pytest.approx(3.0)
        assert w.height == pytest.approx(6.67 - 3.0, abs=1e-2)


def test_cased_opening_classified_separately_from_doors():
    # cedar_ridge has `open` (cased) links and `door` leaves.
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    cased = [o for o in model.openings if o.category == "cased_opening"]
    doors = [o for o in model.openings if o.category == "door" and not o.exterior]
    assert cased and doors
    assert all(o.kind == "cased" for o in cased)


def test_door_swing_and_hinge_carry_into_the_exchange():
    # `into <room>` / `hinge far` (elements.InteriorDoor.swing_into/hinge) ride
    # the opening as optional fields, so the builder can flip the built instance.
    src = (
        'plan "Swing"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 24 x 30\n"
        "room bed: bedroom at 24,0 size 16 x 30\n"
        "door living - bed width 2.67 into bed hinge far\n"
        "entry living south width 3 offset 10\n"
        "window bed east width 4 offset 4\n"
    )
    model = to_revit_model(compile_source(src).plan)
    doc = model.to_dict()
    interior = [o for o in doc["openings"] if o["category"] == "door" and not o["exterior"]]
    assert len(interior) == 1
    assert interior[0]["swing_into"] == "bed"
    assert interior[0]["hinge"] == "far"
    # Exterior doors and windows never carry a swing side.
    for o in doc["openings"]:
        if o["exterior"] or o["category"] == "window":
            assert o["swing_into"] is None and o["hinge"] is None
    # And the reverse direction restores them (round-trip fixed point).
    from barndsl import exchange_to_plan

    back = exchange_to_plan(doc)
    assert back.interior_doors[0].swing_into == "bed"
    assert back.interior_doors[0].hinge == "far"


def test_interior_door_serves_two_rooms_exterior_one():
    model = to_revit_model(_compile_example("cedar_ridge.barn"))
    for o in model.openings:
        if o.exterior or o.category == "window":
            assert len(o.rooms) == 1
        else:
            assert len(o.rooms) == 2


# --- rooms -------------------------------------------------------------------


def test_room_seed_points_lie_inside_their_rectangles():
    plan = _compile_example("cedar_ridge.barn")
    model = to_revit_model(plan)
    by_id = {r.id: r for r in plan.rooms}
    assert len(model.rooms) == len(plan.rooms)
    for rr in model.rooms:
        src = by_id[rr.id]
        px, py = rr.point
        assert src.x < px < src.x2 and src.y < py < src.y2
        assert rr.area == pytest.approx(src.area)
        assert rr.level == src.level


# --- structure ---------------------------------------------------------------


def test_frame_lowers_to_columns_and_framing():
    plan = _compile_example("cedar_ridge.barn")
    plan.frame()
    # recompile from emitted DSL so the frame passes the normal pipeline
    plan = compile_source(emit_dsl(plan), name=plan.name).plan
    model = to_revit_model(plan)
    assert len(model.columns) == len(plan.posts)
    assert len(model.framing) == len(plan.beams)
    assert {f.role for f in model.framing} <= {"frame", "ridge"}
    assert {c.role for c in model.columns} <= {"post", "interior"}


def test_framing_carries_the_posts_nominal_section():
    # Beams share the frame's nominal post section in this MVP (revit.py notes
    # it), so the builder can duplicate-and-size a framing type like it does for
    # columns. An unframed plan has no framing, so no size to check there.
    plan = _compile_example("cedar_ridge.barn")
    plan.frame()
    plan = compile_source(emit_dsl(plan), name=plan.name).plan
    model = to_revit_model(plan)
    assert model.framing
    post_size = max(p.size for p in plan.posts)
    assert post_size > 0
    for f in model.framing:
        assert f.size == pytest.approx(post_size)
    doc = model.to_dict()
    assert all(f["size"] == pytest.approx(post_size) for f in doc["structure"]["framing"])


def test_no_structure_when_no_frame():
    model = to_revit_model(_compile_example("gallery/cottage.barn"))
    assert model.columns == []
    assert model.framing == []


# --- areas (porches / stairs) ------------------------------------------------


def test_porch_and_stair_become_reference_areas():
    porch_model = to_revit_model(_compile_example("cedar_ridge.barn"))
    porches = [a for a in porch_model.areas if a.kind == "porch"]
    assert porches and porches[0].meta.get("covered") is True

    stair_model = to_revit_model(_compile_example("gallery/two_story.barn"))
    stairs = [a for a in stair_model.areas if a.kind == "stair"]
    assert stairs and stairs[0].meta["to_level"] == 1


# --- serialisation -----------------------------------------------------------


def test_json_round_trips():
    plan = _compile_example("cedar_ridge.barn")
    text = to_revit_json(plan)
    doc = json.loads(text)
    assert doc["schema"] == EXCHANGE_SCHEMA
    # Every opening references a wall id that exists in the document.
    wall_ids = {w["id"] for w in doc["walls"]}
    for o in doc["openings"]:
        assert o["host_wall"] in wall_ids
    # Structure block present and shaped.
    assert set(doc["structure"]) == {"columns", "framing"}


def test_lowering_is_deterministic():
    plan = _compile_example("cedar_ridge.barn")
    assert to_revit_json(plan) == to_revit_json(plan)


def test_empty_plan_lowers_without_error():
    plan = compile_source('plan "Empty"\nenvelope 20 x 20\nceiling 9\n').plan
    model = to_revit_model(plan)
    assert model.walls == []
    assert model.openings == []
    assert model.rooms == []
    json.loads(model.to_json())  # still valid JSON
