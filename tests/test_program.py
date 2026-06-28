"""Tests for the `program N bed M bath` directive and the PROGRAM_MISMATCH check.

The directive lets an author declare *intent* — the bedroom/bathroom counts the
plan is meant to deliver — so the validator can catch "compiles clean but isn't
what I asked for" (e.g. a dropped bedroom) instead of letting it slip through a
0-error compile. `baths` counts every bathroom and half-bath, matching the recap.
"""

from __future__ import annotations

import pytest

from barndsl import RoomType as T, barndominium, compile_source, emit_dsl, validate


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


def _plan(program: str) -> str:
    # A valid 2-bed/1-bath skeleton (rooms off a hall spine); the `program` line
    # is injected at the top. No errors, so `r.ok` reflects only the directive.
    return """\
plan "Two bed one bath"
envelope 44 x 24
ceiling 9
{program}
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
""".format(program=program)


# --- parsing -----------------------------------------------------------------


def test_program_parses_onto_the_plan():
    r = compile_source(_plan("program 2 bed 1 bath"))
    assert r.ok, r.report()
    spec = r.plan.program_spec
    assert (spec.beds, spec.baths) == (2, 1)


def test_program_bed_only_leaves_baths_unchecked():
    r = compile_source(_plan("program 2 bed"))
    assert r.plan.program_spec.baths is None
    assert "PROGRAM_MISMATCH" not in _codes(r, "warning")


def test_plural_and_singular_units_both_parse():
    for unit in ("bed", "beds", "bedroom", "bedrooms"):
        r = compile_source(_plan(f"program 2 {unit} 1 bath"))
        assert r.ok, r.report()
    for unit in ("bath", "baths", "bathroom", "bathrooms"):
        r = compile_source(_plan(f"program 2 bed 1 {unit}"))
        assert r.ok, r.report()


def test_non_integer_count_is_a_parse_error():
    r = compile_source(_plan("program 2.5 bed"))
    assert not r.ok
    assert "BAD_COUNT" in _codes(r, "error")


def test_unknown_unit_is_a_parse_error():
    r = compile_source(_plan("program 2 rooms"))
    assert not r.ok


# --- the check ---------------------------------------------------------------


def test_matching_program_is_silent():
    r = compile_source(_plan("program 2 bed 1 bath"))
    assert "PROGRAM_MISMATCH" not in _codes(r, "warning")


def test_too_few_bedrooms_warns():
    r = compile_source(_plan("program 3 bed 1 bath"))
    assert "PROGRAM_MISMATCH" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "PROGRAM_MISMATCH").message
    assert "3 bedroom(s) declared but 2 placed" in msg


def test_bath_mismatch_warns():
    r = compile_source(_plan("program 2 bed 2 bath"))
    msg = next(d for d in r.warnings if d.code == "PROGRAM_MISMATCH").message
    assert "2 bath(s) declared but 1 placed" in msg


def test_mismatch_is_a_warning_not_an_error():
    r = compile_source(_plan("program 5 bed 5 bath"))
    assert r.ok  # still a valid, buildable plan — the directive doesn't block
    assert "PROGRAM_MISMATCH" in _codes(r, "warning")


def test_no_program_means_no_check():
    r = compile_source(_plan("").replace("\n\n", "\n"))
    assert r.plan.program_spec is None
    assert "PROGRAM_MISMATCH" not in _codes(r, "warning")


# --- builder + emit ----------------------------------------------------------


def test_builder_program_method():
    plan = barndominium("B").envelope(width=20, length=20).ceiling(9).program(3, 2)
    assert (plan.program_spec.beds, plan.program_spec.baths) == (3, 2)


def test_builder_program_rejects_negative():
    with pytest.raises(ValueError):
        barndominium("B").program(-1)


def test_program_round_trips_through_emit():
    r = compile_source(_plan("program 2 bed 1 bath"))
    text = emit_dsl(r.plan)
    assert "program 2 bed 1 bath" in text
    r2 = compile_source(text)
    assert (r2.plan.program_spec.beds, r2.plan.program_spec.baths) == (2, 1)


def test_bed_only_program_emits_without_baths():
    plan = barndominium("B").envelope(width=20, length=20).ceiling(9).program(4)
    assert "program 4 bed\n" in emit_dsl(plan)
    assert "bath" not in emit_dsl(plan)
