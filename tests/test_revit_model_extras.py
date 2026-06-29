"""Tests for the building-completion geometry in the exchange:
floor slabs, structural grids, and the gable roof (all pure, no Revit)."""

from __future__ import annotations

import os

import pytest

from barndsl import (
    compile_source,
    emit_dsl,
    roof_plan,
    structural_grids,
    to_revit_model,
)

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _plan(rel):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    assert plan is not None
    return plan


def _framed(rel):
    plan = _plan(rel)
    plan.frame()
    return compile_source(emit_dsl(plan), name=plan.name).plan


# --- slabs -------------------------------------------------------------------


def test_ground_slab_covers_the_footprint():
    plan = _plan("cedar_ridge.barn")
    model = to_revit_model(plan)
    ground = [s for s in model.slabs if s.level == 0]
    assert len(ground) == 1
    s = ground[0]
    assert (s.width, s.length) == pytest.approx((plan.envelope_width, plan.envelope_length))


def test_lshape_ground_slab_is_one_per_section():
    plan = _plan("gallery/lshape.barn")
    model = to_revit_model(plan)
    ground = [s for s in model.slabs if s.level == 0]
    # envelope + each wing → one slab per footprint section (exact for an L).
    assert len(ground) == len(plan.footprint_sections())


def test_upper_level_gets_its_own_slab():
    plan = _plan("gallery/two_story.barn")
    model = to_revit_model(plan)
    upper = [s for s in model.slabs if s.level == 1]
    assert len(upper) == 1
    # The loft is 24x18 in two_story.
    assert (upper[0].width, upper[0].length) == pytest.approx((24, 18))


def test_slabs_serialise():
    model = to_revit_model(_plan("cedar_ridge.barn"))
    doc = model.to_dict()
    assert doc["slabs"] and set(doc["slabs"][0]) == {"level", "x", "y", "width", "length"}


# --- grids -------------------------------------------------------------------


def test_no_grids_without_a_frame():
    assert structural_grids(_plan("cedar_ridge.barn")) == []
    assert to_revit_model(_plan("cedar_ridge.barn")).grids == []


def test_grids_number_the_bents_and_letter_the_eaves():
    plan = _framed("cedar_ridge.barn")  # 60x40, default bay 12 → bents along x
    grids = structural_grids(plan)
    numbered = [g for g in grids if g["label"].isdigit()]
    lettered = [g for g in grids if g["label"].isalpha()]
    # One numbered grid per bent (frame beam).
    bents = [b for b in plan.beams if b.role == "frame"]
    assert len(numbered) == len(bents)
    # At least the two eaves are lettered.
    assert len(lettered) >= 2
    assert numbered[0]["label"] == "1"
    assert lettered[0]["label"] == "A"


def test_numbered_grid_lines_coincide_with_bents():
    plan = _framed("cedar_ridge.barn")
    grids = {g["label"]: g for g in structural_grids(plan)}
    bents = sorted(
        (b for b in plan.beams if b.role == "frame"),
        key=lambda b: (b.x1 + b.x2) / 2.0,
    )
    for i, b in enumerate(bents, start=1):
        g = grids[str(i)]
        assert g["start"] == [b.x1, b.y1] and g["end"] == [b.x2, b.y2]


def test_interior_post_line_becomes_a_lettered_grid():
    # A wide span forces an interior support post line, which should letter-grid.
    src = """\
plan "Wide"
envelope 30 x 60
ceiling 10
room hall: living at 0,0 size 30 x 60
frame bay 12 span 20 post 6
entry hall south width 3 offset 14
window hall north width 6 offset 12
"""
    plan = compile_source(src).plan
    assert any(p.role == "interior" for p in plan.posts), "expected an interior post line"
    lettered = [g for g in structural_grids(plan) if g["label"].isalpha()]
    assert len(lettered) >= 3  # two eaves + the interior line


# --- roof --------------------------------------------------------------------


def test_roof_ridge_runs_the_long_axis():
    plan = _plan("cedar_ridge.barn")  # 60 wide x 40 long → long axis is x
    roof = to_revit_model(plan).roof
    assert roof is not None
    assert roof["gable_axis"] == "x"
    assert roof["ridge"]["start"][1] == pytest.approx(20.0)  # centre of the 40 ft span
    assert len(roof["eaves"]) == 2
    assert len(roof["outline"]) == 4


def test_roof_rise_follows_pitch_over_half_span():
    plan = _plan("cedar_ridge.barn")
    roof = roof_plan(plan, top_level=0, pitch=4.0 / 12.0)
    # short span is 40 → half is 20 → rise = 20 * 4/12.
    assert roof["rise"] == pytest.approx(20.0 * 4.0 / 12.0)


def test_roof_axis_flips_with_a_tall_plan():
    src = """\
plan "Tall"
envelope 30 x 50
ceiling 9
room a: living at 0,0 size 30 x 50
entry a south width 3 offset 14
window a north width 6 offset 12
"""
    plan = compile_source(src).plan
    roof = to_revit_model(plan).roof
    assert roof["gable_axis"] == "y"  # long axis is y (50 > 30)
