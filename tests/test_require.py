"""Tests for the `require` statement and the REQUIRE_UNMET / REQUIRE_REF checks.

`require` extends the `program` pattern (declared intent, mechanically checked)
to *spatial* intent: a required adjacency, separation, exterior wall, or minimum
nominal area, checked against the compiled plan on every compile. Unmet
requirements warn (`REQUIRE_UNMET`) with a concrete fix; a requirement naming an
unknown room id is an error (`REQUIRE_REF`), like every dangling reference.
"""

from __future__ import annotations

import pytest

from barndsl import barndominium, compile_source, emit_dsl, validate


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


def _unmet(result):
    return [d for d in result.warnings if d.code == "REQUIRE_UNMET"]


def _plan(requires: str) -> str:
    # The test_program skeleton: a valid 2-bed/1-bath plan off a hall spine.
    # Resolved rectangles: living 0,0 16x24; hall 16,0 28x4; bed1 16,4 10x20;
    # bath 26,4 6x20; bed2 32,4 12x20 (envelope 44 x 24). The `require` lines
    # are injected after `ceiling`; only REQUIRE_* codes are asserted on.
    return """\
plan "Two bed one bath"
envelope 44 x 24
ceiling 9
{requires}
room living: living   at 0,0   size 16 x 24
room hall:   hallway  at 16,0  size 28 x 4
room bed1:   bedroom  north-of hall  size 10 x 20
room bath:   bathroom east-of bed1   size 6 x 20
room bed2:   bedroom  east-of bath   size 12 x 20
door living - hall width 3
door hall - bed1 width 2.67
door hall - bath width 2.67
door hall - bed2 width 2.67
entry living south width 3 offset 6
window living west width 10 offset 6
window bed1 north width 4 offset 4
window bed2 north width 4 offset 2
window bath north width 3 offset 1
""".format(requires=requires)


# --- parsing + round-trip ------------------------------------------------------


def test_all_four_forms_parse_onto_the_plan():
    r = compile_source(
        _plan(
            "require adjacent living hall\n"
            "require separate living bed2\n"
            "require exterior bed1 north\n"
            "require area living >= 300"
        )
    )
    assert r.ok, r.report()
    kinds = [(q.kind, q.a, q.b) for q in r.plan.requirements]
    assert kinds == [
        ("adjacent", "living", "hall"),
        ("separate", "living", "bed2"),
        ("exterior", "bed1", None),
        ("area", "living", None),
    ]
    assert r.plan.requirements[2].wall.value == "north"
    assert r.plan.requirements[3].min_area == 300.0


def test_exterior_wall_is_optional():
    r = compile_source(_plan("require exterior living"))
    assert r.ok, r.report()
    assert r.plan.requirements[0].wall is None


def test_requirements_round_trip_through_emit():
    src = _plan(
        "require adjacent living hall\n"
        "require separate living bed2\n"
        "require exterior bed1 north\n"
        "require exterior living\n"
        "require area living >= 300"
    )
    r = compile_source(src)
    text = emit_dsl(r.plan)
    # Serialized canonically, next to `program`'s slot ahead of the rooms.
    assert "require adjacent living hall" in text
    assert "require separate living bed2" in text
    assert "require exterior bed1 north" in text
    assert "require exterior living\n" in text
    assert "require area living >= 300" in text
    # compile(emit(compile(src))) is a fixed point.
    again = compile_source(text)
    assert again.plan is not None and not again.errors, again.report()
    assert emit_dsl(again.plan) == text


def test_unknown_requirement_kind_is_a_parse_error():
    r = compile_source(_plan("require touching living hall"))
    assert r.plan is None
    assert "BAD_OPTION" in _codes(r, "error")


def test_negative_area_is_a_parse_error():
    r = compile_source(_plan("require area living >= -5"))
    assert r.plan is None
    assert "BAD_NUMBER" in _codes(r, "error")


def test_area_without_ge_is_a_syntax_error():
    r = compile_source(_plan("require area living 300"))
    assert r.plan is None
    assert "SYNTAX" in _codes(r, "error")


# --- satisfied requirements are silent ----------------------------------------


def test_satisfied_requirements_produce_no_diagnostics():
    r = compile_source(
        _plan(
            "require adjacent living hall\n"
            "require adjacent bed1 bath\n"
            "require separate living bed2\n"
            "require exterior living west\n"
            "require exterior bed1 north\n"
            "require exterior bed1\n"
            "require area living >= 300"
        )
    )
    assert "REQUIRE_UNMET" not in _codes(r, "warning"), r.report()
    assert "REQUIRE_REF" not in _codes(r, "error")


def test_duplicate_requirements_are_harmless():
    r = compile_source(
        _plan("require adjacent living hall\nrequire adjacent living hall")
    )
    assert not _unmet(r)


# --- each unmet form warns with a concrete hint --------------------------------


def test_unmet_adjacent_warns_and_suggests_a_relative_anchor():
    # living (0..16) and bed2 (32..44) never touch.
    r = compile_source(_plan("require adjacent living bed2"))
    assert r.ok  # a warning, never blocking
    (d,) = _unmet(r)
    assert "'living' and 'bed2'" in d.message
    assert "don't share a wall" in d.message
    assert "east-of living" in d.hint  # a relative placement, the short way over
    assert "room bed2: bedroom" in d.hint


def test_unmet_separate_names_the_shared_wall_length():
    # living and bed1 share the x=16 wall over y 4..24 — a 20 ft run.
    r = compile_source(_plan("require separate living bed1"))
    (d,) = _unmet(r)
    assert "share a 20 ft wall" in d.message
    assert "buffer" in d.hint


def test_unmet_exterior_side_says_which_walls_are_interior():
    # bed1 sits mid-plan: only its north wall reaches the envelope.
    r = compile_source(_plan("require exterior bed1 south"))
    (d,) = _unmet(r)
    assert "south wall is interior" in d.message
    assert "interior walls: south, east, west" in d.message
    assert "exterior wall(s): north" in d.hint


def test_exterior_side_met_when_the_wall_reaches_the_envelope():
    # Same room, north side: bed1's north wall lies on y = envelope_length.
    r = compile_source(_plan("require exterior bed1 north"))
    assert not _unmet(r)


def test_unmet_area_states_actual_vs_required():
    r = compile_source(_plan("require area living >= 500"))  # living is 384 sq ft
    (d,) = _unmet(r)
    assert "384 sq ft" in d.message
    assert ">= 500 sq ft" in d.message
    assert "size 16 x 32" in d.hint  # a concrete resize that meets it


def test_each_unmet_requirement_warns_separately():
    r = compile_source(
        _plan("require adjacent living bed2\nrequire area living >= 500")
    )
    assert len(_unmet(r)) == 2
    # Anchored to their own `require` lines, like `program`.
    assert sorted(d.line for d in _unmet(r)) == [4, 5]


# --- unknown room ids are errors ------------------------------------------------


def test_unknown_room_is_a_require_ref_error():
    r = compile_source(_plan("require adjacent living pantry"))
    assert not r.ok
    (d,) = [d for d in r.errors if d.code == "REQUIRE_REF"]
    assert "unknown room 'pantry'" in d.message
    assert d.room == "pantry"


def test_unknown_room_in_area_form_is_an_error_too():
    r = compile_source(_plan("require area attic >= 100"))
    assert "REQUIRE_REF" in _codes(r, "error")


# --- levels ---------------------------------------------------------------------


def test_separate_across_levels_is_trivially_satisfied():
    # A loft stacked directly over the living room: same footprint band, but
    # rooms on different levels never share a wall, so `separate` holds.
    plan = (
        barndominium("Levels")
        .envelope(width=30, length=20)
        .ceiling(9)
        .add_room("living", "living", x=0, y=0, width=30, length=20)
        .add_room("loft", "loft", x=0, y=0, width=30, length=10, level=1)
        .require("separate", "living", "loft")
    )
    rep = validate(plan)
    assert "REQUIRE_UNMET" not in {d.code for d in rep.warnings}


def test_adjacent_across_levels_is_unmet():
    plan = (
        barndominium("Levels")
        .envelope(width=30, length=20)
        .ceiling(9)
        .add_room("living", "living", x=0, y=0, width=30, length=20)
        .add_room("loft", "loft", x=0, y=0, width=30, length=10, level=1)
        .require("adjacent", "living", "loft")
    )
    (d,) = [d for d in validate(plan).warnings if d.code == "REQUIRE_UNMET"]
    assert "different levels (0 and 1)" in d.message


# --- the Python builder ----------------------------------------------------------


def test_builder_require_mirrors_the_statement():
    plan = (
        barndominium("B")
        .envelope(width=30, length=20)
        .ceiling(9)
        .add_room("a", "living", x=0, y=0, width=15, length=20)
        .add_room("b", "kitchen", x=15, y=0, width=15, length=20)
        .require("adjacent", "a", "b")
        .require("exterior", "a", wall="west")
        .require("area", "a", min_area=200)
    )
    assert [q.kind for q in plan.requirements] == ["adjacent", "exterior", "area"]
    rep = validate(plan)
    assert "REQUIRE_UNMET" not in {d.code for d in rep.warnings}
    # Now an unmet one: a and b abut, so `separate` fails on their shared wall.
    plan.require("separate", "a", "b")
    (d,) = [d for d in validate(plan).warnings if d.code == "REQUIRE_UNMET"]
    assert "share a 20 ft wall" in d.message


def test_builder_require_validates_its_arguments():
    plan = barndominium("B").envelope(width=20, length=20).ceiling(9)
    with pytest.raises(ValueError):
        plan.require("touching", "a", "b")  # unknown kind
    with pytest.raises(ValueError):
        plan.require("adjacent", "a")  # needs two rooms
    with pytest.raises(ValueError):
        plan.require("area", "a")  # needs min_area
    with pytest.raises(ValueError):
        plan.require("area", "a", min_area=-1)  # non-negative
    with pytest.raises(ValueError):
        plan.require("exterior", "a", "b")  # names a single room


def test_builder_requirements_emit_back_to_dsl():
    plan = (
        barndominium("B")
        .envelope(width=30, length=20)
        .ceiling(9)
        .add_room("a", "living", x=0, y=0, width=15, length=20)
        .require("area", "a", min_area=250)
        .require("exterior", "a", wall="south")
    )
    text = emit_dsl(plan)
    assert "require area a >= 250" in text
    assert "require exterior a south" in text


# --- registry ---------------------------------------------------------------------


def test_require_codes_are_registered():
    from barndsl.diagnostics import REGISTRY

    for code in ("REQUIRE_UNMET", "REQUIRE_REF"):
        assert code in REGISTRY
    assert REGISTRY["REQUIRE_UNMET"].severity.value == "warning"
    assert REGISTRY["REQUIRE_REF"].severity.value == "error"
