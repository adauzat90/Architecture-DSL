"""Round-trip tests for the Revit exchange: plan → exchange → plan.

`to_revit_model` lowers a plan to the Revit-shaped exchange; `exchange_to_plan`
reconstructs it. Going both ways should recover the same rooms, the same door/
window connections (by re-deriving each opening's wall and offset from geometry),
and re-emit DSL that still compiles clean.
"""

from __future__ import annotations

import json
import os

import pytest

from barndsl import (
    RevitImportError,
    compile_source,
    emit_dsl,
    exchange_to_dsl,
    exchange_to_plan,
    to_revit_model,
)

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")
GALLERY = ["cottage.barn", "hall_spine.barn", "lshape.barn", "two_story.barn"]


def _plan(rel: str):
    with open(os.path.join(EXAMPLES, "gallery", rel), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    assert plan is not None
    return plan


def _round_trip(plan):
    return exchange_to_plan(to_revit_model(plan).to_dict())


def _rooms_key(plan):
    return sorted(
        (r.id, r.type.value, round(r.x, 4), round(r.y, 4), round(r.width, 4), round(r.length, 4), r.level)
        for r in plan.rooms
    )


def _interior_key(plan):
    return sorted(
        (frozenset((d.room_a, d.room_b)), d.kind, round(d.width, 3)) for d in plan.interior_doors
    )


def _exterior_key(plan):
    return sorted(
        (d.room, d.wall.value, round(d.width, 3), d.egress) for d in plan.exterior_doors
    )


def _window_key(plan):
    return sorted(
        (w.room, w.wall.value, round(w.width, 3), round(w.sill_height, 3)) for w in plan.windows
    )


@pytest.mark.parametrize("rel", GALLERY)
def test_rooms_round_trip(rel):
    plan = _plan(rel)
    assert _rooms_key(_round_trip(plan)) == _rooms_key(plan)


@pytest.mark.parametrize("rel", GALLERY)
def test_interior_doors_round_trip(rel):
    plan = _plan(rel)
    assert _interior_key(_round_trip(plan)) == _interior_key(plan)


@pytest.mark.parametrize("rel", GALLERY)
def test_exterior_doors_round_trip(rel):
    plan = _plan(rel)
    assert _exterior_key(_round_trip(plan)) == _exterior_key(plan)


@pytest.mark.parametrize("rel", GALLERY)
def test_windows_round_trip(rel):
    plan = _plan(rel)
    back = _round_trip(plan)
    assert _window_key(back) == _window_key(plan)
    # Head heights survive too (sill + opening height).
    for w0, w1 in zip(sorted(plan.windows, key=lambda w: (w.room, w.wall.value, w.offset)),
                      sorted(back.windows, key=lambda w: (w.room, w.wall.value, w.offset))):
        assert w1.head_height == pytest.approx(w0.head_height, abs=1e-3)


@pytest.mark.parametrize("rel", GALLERY)
def test_envelope_and_ceiling_round_trip(rel):
    plan = _plan(rel)
    back = _round_trip(plan)
    assert back.envelope_width == pytest.approx(plan.envelope_width)
    assert back.envelope_length == pytest.approx(plan.envelope_length)
    assert back.ceiling_height == pytest.approx(plan.ceiling_height)
    assert len(back.wings) == len(plan.wings)


@pytest.mark.parametrize("rel", GALLERY)
def test_reconstructed_dsl_compiles_clean(rel):
    # The whole point: a round-tripped plan is still a valid plan.
    plan = _plan(rel)
    src = exchange_to_dsl(to_revit_model(plan).to_dict())
    result = compile_source(src, name=plan.name)
    assert result.ok, result.report()


def test_porches_and_stairs_round_trip():
    # cottage has neither; cedar_ridge has a porch; two_story has a stair.
    with open(os.path.join(EXAMPLES, "cedar_ridge.barn"), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    back = _round_trip(plan)
    assert len(back.porches) == len(plan.porches)
    assert back.porches[0].covered == plan.porches[0].covered

    two = _plan("two_story.barn")
    back2 = _round_trip(two)
    assert len(back2.stairs) == len(two.stairs)
    s0, s1 = two.stairs[0], back2.stairs[0]
    assert (s1.from_level, s1.to_level) == (s0.from_level, s0.to_level)


def test_exchange_to_plan_rejects_foreign_schema():
    with pytest.raises(RevitImportError):
        exchange_to_plan({"schema": "ifc/4", "units": "feet"})


def test_round_trip_through_json_string():
    plan = _plan("cottage.barn")
    text = to_revit_model(plan).to_json()
    back = exchange_to_plan(json.loads(text))
    assert _rooms_key(back) == _rooms_key(plan)


def test_reconstructs_reader_shaped_partial_exchange():
    """The Revit reader emits a *partial* exchange — no walls, host_wall null —
    and leans on exchange_to_plan to infer each opening's wall/offset from
    geometry. Lock that contract with a hand-built minimal document."""
    data = {
        "schema": "barndsl.revit/1",
        "units": "feet",
        "plan": {"name": "Read Back", "ceiling_height": 9,
                 "envelope_width": 30, "envelope_length": 24, "wings": []},
        "levels": [{"index": 0, "name": "Level 1", "elevation": 0.0, "height": 9}],
        "walls": [],  # reader doesn't read walls back
        "openings": [
            {"id": "door0", "category": "door", "kind": "swing", "level": 0,
             "location": [18.0, 8.0], "width": 3.0, "height": 6.667, "sill": 0.0,
             "exterior": False, "egress": False, "rooms": ["living", "bed"], "host_wall": None},
            {"id": "win0", "category": "window", "kind": "window", "level": 0,
             "location": [9.0, 0.0], "width": 4.0, "height": 3.5, "sill": 3.0,
             "exterior": True, "egress": False, "rooms": ["living"], "host_wall": None},
        ],
        "rooms": [
            {"id": "living", "name": "Living", "type": "living", "level": 0,
             "point": [9, 12], "area": 432, "x": 0, "y": 0, "width": 18, "length": 24},
            {"id": "bed", "name": "Bed", "type": "bedroom", "level": 0,
             "point": [24, 12], "area": 288, "x": 18, "y": 0, "width": 12, "length": 24},
        ],
        "structure": {"columns": [], "framing": []},
        "areas": [],
    }
    plan = exchange_to_plan(data)
    assert {r.id for r in plan.rooms} == {"living", "bed"}
    # The interior door's wall/offset were inferred from the shared edge.
    assert len(plan.interior_doors) == 1
    d = plan.interior_doors[0]
    assert {d.room_a, d.room_b} == {"living", "bed"}
    assert d.offset == pytest.approx(6.5)  # 8 (centre) - 1.5 (half width) - 0 (edge start)
    # The window's wall was inferred as the south (y==0) edge of living.
    assert len(plan.windows) == 1
    assert plan.windows[0].wall.value == "south"
    assert plan.windows[0].offset == pytest.approx(7.0)  # 9 - 2 (half of 4)


def test_emit_is_stable_under_double_round_trip():
    # plan → exchange → plan → exchange → plan should reach a fixed point.
    plan = _plan("hall_spine.barn")
    once = _round_trip(plan)
    twice = _round_trip(once)
    assert emit_dsl(once) == emit_dsl(twice)


# --- exchange v2: declared-intent round-trip ---------------------------------
#
# The geometry alone can't reconstruct declared *intent* (program, frame,
# roof form/pitch, notes, the accessibility opt-in). These carry as optional
# keys in the `plan` block and restore via the builder methods, so a plan that
# went through Revit and back still knows what it was meant to be.

_INTENT = """\
plan "Intent"
envelope 50 x 40
ceiling 10
roof monitor pitch 0.5
accessible
note "client wants a wraparound porch"
program 2 beds 1 baths
frame bay 10 span 40
room living: living at 0,0 size 30 x 40
room bed1: bedroom at 30,0 size 20 x 20
room bed2: bedroom at 30,20 size 20 x 20
"""


def _intent_plan():
    plan = compile_source(_INTENT).plan
    assert plan is not None
    return plan


def test_roof_style_and_pitch_round_trip():
    back = _round_trip(_intent_plan())
    assert back.roof_style == "monitor"
    assert back.roof_pitch == pytest.approx(0.5)


def test_default_roof_stays_a_fixed_point():
    # A plan with no roof override must NOT freeze the default pitch as an
    # override — roof_pitch stays None, so emit is a fixed point.
    plan = _plan("cottage.barn")
    back = _round_trip(plan)
    assert back.roof_style == "gable"
    assert back.roof_pitch is None
    # No roof intent keys leak into the plan block for a default plan.
    pinfo = to_revit_model(plan).to_dict()["plan"]
    assert "roof_style" not in pinfo and "roof_pitch" not in pinfo


def test_notes_and_accessible_round_trip():
    back = _round_trip(_intent_plan())
    assert back.notes == "client wants a wraparound porch"
    assert back.accessible is True


def test_program_round_trips_with_counts_and_requires():
    src = _INTENT.replace(
        "program 2 beds 1 baths",
        "program 2 bed 1 bath 1 laundry area 900",
    )
    plan = compile_source(src).plan
    assert plan is not None
    back = _round_trip(plan)
    assert back.program_spec is not None
    assert back.program_spec.beds == 2
    assert back.program_spec.baths == 1
    assert back.program_spec.min_area == pytest.approx(900.0)
    # the `requires` map survives (keyed by RoomType)
    from barndsl.elements import RoomType

    assert back.program_spec.required.get(RoomType.LAUNDRY) == 1


def test_frame_spec_round_trips_and_replaces_the_skeleton():
    plan = _intent_plan()
    back = _round_trip(plan)
    assert back.frame_spec is not None
    assert back.frame_spec.bay == pytest.approx(10.0)
    assert back.frame_spec.span == pytest.approx(40.0)
    # Restoring the frame *request* re-places posts/beams (they aren't stored).
    assert len(back.posts) == len(plan.posts) > 0
    assert len(back.beams) == len(plan.beams) > 0


_NO_INTENT = """\
plan "Bare"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 24 x 30
room bed: bedroom at 24,0 size 16 x 30
"""


def test_absent_intent_keys_are_harmless():
    # A plan declaring none of the v2 intent still round-trips cleanly, and the
    # optional keys are simply absent (an older document loads the same way).
    plan = compile_source(_NO_INTENT).plan
    data = to_revit_model(plan).to_dict()
    for key in ("roof_style", "roof_pitch", "notes", "accessible", "program", "frame"):
        assert key not in data["plan"]
    back = exchange_to_plan(data)
    assert back.program_spec is None and back.frame_spec is None
    assert back.notes == "" and back.accessible is False


def test_partial_intent_program_without_frame():
    src = """\
plan "PartInt"
envelope 40 x 30
ceiling 9
program 1 beds
room living: living at 0,0 size 24 x 30
room bed: bedroom at 24,0 size 16 x 30
"""
    plan = compile_source(src).plan
    data = to_revit_model(plan).to_dict()
    assert "program" in data["plan"] and "frame" not in data["plan"]
    back = exchange_to_plan(data)
    assert back.program_spec is not None and back.program_spec.beds == 1
    assert back.frame_spec is None


def test_program_mismatch_guards_a_revit_edit_after_round_trip():
    # The declared program surviving the round-trip is what lets PROGRAM_MISMATCH
    # catch a bedroom deleted in Revit: round-trip to DSL, drop a bedroom from
    # the reconstructed source, recompile — the guard fires.
    plan = _intent_plan()
    src = exchange_to_dsl(to_revit_model(plan).to_dict())
    assert "program 2 bed" in src  # the declared intent survived to source
    edited = src.replace("room bed2: bedroom at 30,20 size 20 x 20\n", "")
    assert edited != src
    result = compile_source(edited, name=plan.name)
    codes = [d.code for d in result.diagnostics]
    assert "PROGRAM_MISMATCH" in codes, codes
