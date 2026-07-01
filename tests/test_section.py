"""Section fidelity — the vertical model the plan lowers to.

Covers the pieces a floor plan usually leaves out: floor-to-floor (distinct from
the clear ceiling height), the sloped gable roof the exchange carries, and the
gable-end walls whose tops rise to the ridge. All pure, no Revit.
"""

import math
import os

import pytest

from barndsl import compile_source, emit_dsl, exchange_to_plan, to_revit_model
from barndsl.constants import DEFAULT_ROOF_PITCH, FLOOR_ASSEMBLY_DEPTH

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _plan(dsl):
    result = compile_source(dsl)
    assert result.plan is not None
    return result.plan


def _example(rel):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        return _plan(fh.read())


# --- floor-to-floor ----------------------------------------------------------


def test_floor_to_floor_adds_the_assembly_depth():
    plan = _plan("plan \"x\"\nenvelope 30 x 20\nceiling 9\n")
    assert plan.floor_depth == pytest.approx(FLOOR_ASSEMBLY_DEPTH)
    assert plan.floor_to_floor == pytest.approx(9 + FLOOR_ASSEMBLY_DEPTH)
    # Ground floor at grade; each level stacks by floor-to-floor, not ceiling.
    assert plan.level_elevation(0) == 0.0
    assert plan.level_elevation(2) == pytest.approx(2 * (9 + FLOOR_ASSEMBLY_DEPTH))


def test_floor_directive_parses_and_round_trips():
    plan = _plan("plan \"x\"\nenvelope 30 x 20\nceiling 10\nfloor 1.5\n")
    assert plan.floor_depth == pytest.approx(1.5)
    assert plan.floor_to_floor == pytest.approx(11.5)
    # The directive survives emit → recompile.
    src = emit_dsl(plan)
    assert "floor 1.5" in src
    assert _plan(src).floor_depth == pytest.approx(1.5)


def test_default_floor_depth_is_not_emitted():
    # A plan that never set the floor depth stays clean — no `floor` line.
    plan = _plan("plan \"x\"\nenvelope 30 x 20\nceiling 9\n")
    assert "floor " not in emit_dsl(plan)


def test_upper_level_stacks_by_floor_to_floor():
    plan = _example("gallery/two_story.barn")
    model = to_revit_model(plan)
    elevs = {lvl.index: lvl.elevation for lvl in model.levels}
    assert elevs[0] == 0.0
    assert elevs[1] == pytest.approx(plan.floor_to_floor)
    assert elevs[1] > plan.ceiling_height  # not stacked on the ceiling plane


def test_exchange_carries_floor_depth_and_round_trips():
    plan = _plan("plan \"x\"\nenvelope 30 x 20\nceiling 9\nfloor 1.25\n")
    data = to_revit_model(plan).to_dict()
    assert data["plan"]["floor_depth"] == pytest.approx(1.25)
    assert data["plan"]["floor_to_floor"] == pytest.approx(10.25)
    assert exchange_to_plan(data).floor_depth == pytest.approx(1.25)


# --- gable roof --------------------------------------------------------------


def test_roof_slope_angle_follows_pitch():
    roof = to_revit_model(_example("cedar_ridge.barn")).roof
    assert roof["slope_angle"] == pytest.approx(math.atan(DEFAULT_ROOF_PITCH))


def test_exactly_the_two_eaves_slope():
    roof = to_revit_model(_example("cedar_ridge.barn")).roof
    slopes = roof["outline_slopes"]
    assert len(slopes) == len(roof["outline"])
    assert sum(1 for s in slopes if s) == 2  # a gable slopes on its two eaves
    # The sloping edges are the ones parallel to the ridge (the long axis).
    for seg, is_slope in zip(roof["outline"], slopes):
        (sx, sy), (ex, ey) = seg
        horizontal = abs(ey - sy) < 1e-9
        assert is_slope == (horizontal if roof["gable_axis"] == "x" else not horizontal)


# --- gable-end walls ---------------------------------------------------------


def _gable_walls(model):
    return [w for w in model.walls if w.profile == "gable"]


def test_gable_walls_rise_to_the_ridge():
    plan = _example("cedar_ridge.barn")
    model = to_revit_model(plan)
    gables = _gable_walls(model)
    assert gables, "expected gable-end walls"
    rise = model.roof["rise"]
    for w in gables:
        assert w.exterior  # only exterior walls gable
        assert w.apex is not None
        # The top rises from the plate (ceiling height) to the ridge apex.
        assert w.apex_height == pytest.approx(plan.ceiling_height + rise)
        assert w.apex_height > w.height


def test_gable_walls_run_across_the_ridge_and_eaves_stay_flat():
    model = to_revit_model(_example("cedar_ridge.barn"))
    want = "v" if model.roof["gable_axis"] == "x" else "h"
    for w in model.walls:
        if w.profile == "gable":
            assert w.orientation == want  # perpendicular to the ridge
        else:
            assert w.apex is None and w.apex_height == 0.0


def test_interior_walls_are_never_gable():
    model = to_revit_model(_example("cedar_ridge.barn"))
    assert all(w.exterior for w in model.walls if w.profile == "gable")
