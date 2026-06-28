"""Tests for the circulation-quality checks — PRIVATE_PASSTHROUGH and ENTRY_PRIVATE.

These flag layouts that *reach* every room (so NO_ACCESS is satisfied) but route
people through private rooms to do it — e.g. a mudroom whose only way into the
house is through a bathroom. They are design-quality checks on the door graph's
*shape*, and (unlike the soft INFO nudges) a forced pass-through a bathroom is a
real defect, so it's a WARNING.
"""

from __future__ import annotations

from barndsl import compile_source


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- PRIVATE_PASSTHROUGH -----------------------------------------------------


def test_room_only_reachable_through_a_bathroom_warns():
    src = """\
plan "Bath gateway"
envelope 40 x 28
ceiling 9
room living:  living   at 0,0   size 22 x 28
room kitchen: kitchen  at 22,0  size 18 x 14
room bath:    bathroom at 22,14 size 10 x 14
room mud:     mudroom  at 32,14 size 8 x 14
door living - kitchen width 6
door kitchen - bath width 2.67
door bath - mud width 2.67
entry living south width 3 offset 10
"""
    r = compile_source(src)
    assert "PRIVATE_PASSTHROUGH" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "PRIVATE_PASSTHROUGH").message
    assert "mud" in msg and "bathroom" in msg


def test_room_only_reachable_through_a_bedroom_warns():
    src = """\
plan "Bed gateway"
envelope 36 x 28
ceiling 9
room living: living  at 0,0   size 16 x 28
room hall:   hallway at 16,0  size 4 x 28
room bed:    bedroom at 20,0  size 16 x 16
room office: office  at 20,16 size 16 x 12
door living - hall width 3
door hall - bed width 2.67
door bed - office width 2.67
entry living south width 3 offset 6
window bed north width 6 offset 4
window office east width 5 offset 4
"""
    r = compile_source(src)
    assert "PRIVATE_PASSTHROUGH" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "PRIVATE_PASSTHROUGH").message
    assert "office" in msg and "bedroom" in msg


def test_ensuite_bath_behind_a_bedroom_is_not_flagged():
    # The canonical suite: master → ensuite. Removing the bedroom isolates the
    # bath, but that's exactly where it belongs — no warning.
    src = """\
plan "Suite"
envelope 36 x 24
ceiling 9
room living:   living   at 0,0   size 16 x 24
room hall:     hallway  at 16,0  size 4 x 24
room master:   bedroom  at 20,0  size 16 x 16
room ensuite:  bathroom at 20,16 size 16 x 8
door living - hall width 3
door hall - master width 2.67
door master - ensuite width 2.67
entry living south width 3 offset 6
window master north width 6 offset 4
"""
    r = compile_source(src)
    assert "PRIVATE_PASSTHROUGH" not in _codes(r, "warning")


def test_walk_in_closet_behind_a_bedroom_is_not_flagged():
    src = """\
plan "Walk-in"
envelope 36 x 24
ceiling 9
room living:  living  at 0,0   size 16 x 24
room hall:    hallway at 16,0  size 4 x 24
room master:  bedroom at 20,0  size 16 x 16
room closet:  closet  at 20,16 size 16 x 8
door living - hall width 3
door hall - master width 2.67
door master - closet width 2.5
entry living south width 3 offset 6
window master north width 6 offset 4
"""
    r = compile_source(src)
    assert "PRIVATE_PASSTHROUGH" not in _codes(r, "warning")


def test_normal_hall_plan_has_no_passthrough_warning():
    src = """\
plan "Tidy"
envelope 40 x 30
ceiling 9
room living:  living   at 0,0   size 22 x 18
room kitchen: kitchen  at 22,0  size 18 x 18
room hall:    hallway  at 0,18  size 40 x 4
room bed1:    bedroom  at 0,22  size 20 x 8
room bed2:    bedroom  at 20,22 size 20 x 8
door living - kitchen width 6
door living - hall width 3
door hall - bed1 width 2.67
door hall - bed2 width 2.67
entry living south width 3 offset 10
window bed1 north width 6 offset 4
window bed2 north width 6 offset 4
"""
    r = compile_source(src)
    assert "PRIVATE_PASSTHROUGH" not in _codes(r, "warning")


# --- ENTRY_PRIVATE -----------------------------------------------------------


def test_entry_into_a_bathroom_warns():
    src = """\
plan "Front bath"
envelope 30 x 20
ceiling 9
room living: living   at 0,0  size 20 x 20
room bath:   bathroom at 20,0 size 10 x 20
door living - bath width 2.67
entry bath south width 3 offset 4
"""
    r = compile_source(src)
    assert "ENTRY_PRIVATE" in _codes(r, "warning")


def test_entry_into_a_bedroom_is_an_info():
    src = """\
plan "Patio door"
envelope 34 x 20
ceiling 9
room living: living  at 0,0  size 18 x 20
room hall:   hallway at 18,0 size 4 x 20
room bed:    bedroom at 22,0 size 12 x 20
door living - hall width 3
door hall - bed width 2.67
entry living south width 3 offset 6
entry bed east width 3 offset 8
window bed east width 5 offset 2
"""
    r = compile_source(src)
    assert "ENTRY_PRIVATE" in _codes(r, "info")
    assert "ENTRY_PRIVATE" not in _codes(r, "warning")
