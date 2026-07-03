"""Tests for the site / setback feature (site & solar, Phase 2).

A `lot` rectangle plus `setback` distances give the plan a buildable envelope;
the footprint is checked against it (`SETBACK`, a WARNING). Both are gated on a
declared `lot`, so an ordinary plan is untouched.

- lot_box / buildable_envelope  — the geometry (incl. auto-centre).
- SETBACK                        — footprint short of a setback / past the lot line.
- emit / builder                 — round-trip and the fluent API + aliases.
"""

from __future__ import annotations

import pytest

from barndsl import barndominium, compile_source, emit_dsl, render_svg
from barndsl import Direction as D, RoomType as T


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# A clean 60x40 plan; the {site} slot takes the lot/setback lines under test.
_BASE = """\
plan "Sited"
envelope 60 x 40
ceiling 12
{site}room living:  living   at 0,0  size 30 x 40
room kitchen: kitchen  at 30,0 size 30 x 40
open living - kitchen width 8
entry living south width 3 offset 14
entry kitchen east width 3 offset 20
window living south width 6 offset 4
window kitchen north width 6 offset 4
"""


def _compile(site: str):
    return compile_source(_BASE.format(site=site))


# --- geometry ----------------------------------------------------------------


def test_lot_box_explicit_and_buildable_envelope():
    plan = _compile("lot 200 x 300 at -70,-130\nsetback front 40 back 25 left 15 right 15\n").plan
    assert plan.lot_box() == (-70.0, -130.0, 130.0, 170.0)
    # inset by setbacks: west+15, south+40, east-15, north-25
    assert plan.buildable_envelope() == (-55.0, -90.0, 115.0, 145.0)


def test_lot_auto_centres_when_no_at():
    # footprint bbox is (0,0,60,40) → centre (30,20); a 100x100 lot centres there.
    plan = _compile("lot 100 x 100\n").plan
    assert plan.lot_box() == (-20.0, -30.0, 80.0, 70.0)


# --- SETBACK check -----------------------------------------------------------


def test_fits_is_silent():
    r = _compile("lot 200 x 300 at -70,-130\nsetback front 40 back 25 left 15 right 15\n")
    assert "SETBACK" not in _codes(r, "warning")


def test_encroachment_flags_each_short_side():
    r = _compile("lot 70 x 50 at -3,-3\nsetback front 40 back 25 left 15 right 15\n")
    assert r.ok  # a WARNING, never a blocker
    setbacks = [i for i in r.warnings if i.code == "SETBACK"]
    assert {s for i in setbacks for s in ("south", "north", "east", "west") if s in i.message} == {
        "south", "north", "east", "west",
    }


def test_building_past_the_lot_line_even_without_setbacks():
    # A 40-wide lot can't hold the 60-wide building; east edge runs past it.
    r = _compile("lot 40 x 40 at 0,0\n")
    msgs = [i.message for i in r.warnings if i.code == "SETBACK"]
    assert any("crosses the east lot line" in m for m in msgs)


def test_no_lot_means_no_setback_check():
    # A setback with no lot is inert (and must not crash).
    r = _compile("setback front 40\n")
    assert "SETBACK" not in _codes(r, "warning")
    assert r.plan.lot is None


def test_unsited_plan_untouched():
    r = _compile("")
    assert "SETBACK" not in _codes(r, "warning")


# --- emit / round-trip -------------------------------------------------------


def test_lot_and_setback_round_trip():
    src = "lot 200 x 300 at -70,-130\nsetback front 40 back 25 left 15 right 15\n"
    plan = _compile(src).plan
    dsl = emit_dsl(plan)
    assert "lot 200 x 300 at -70,-130" in dsl
    # aliases normalise to plan-relative cardinals, in canonical order.
    assert "setback south 40 north 25 east 15 west 15" in dsl
    # and it recompiles to the same lot / setbacks.
    again = compile_source(dsl).plan
    assert again.lot_box() == plan.lot_box()
    assert again.setbacks == plan.setbacks


def test_auto_centred_lot_emits_no_at():
    plan = _compile("lot 100 x 100\n").plan
    line = next(l for l in emit_dsl(plan).splitlines() if l.startswith("lot "))
    assert line == "lot 100 x 100"  # no `at`


# --- builder + parse errors --------------------------------------------------


def test_builder_lot_and_setback_aliases():
    plan = (
        barndominium("B")
        .envelope(width=40, length=30)
        .ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=40, length=30)
        .entrance("a", D.SOUTH, width=3, offset=4)
        .set_lot(120, 100, x=-40, y=-35)
        .setback(front=25, back=20, left=10, right=10)
    )
    assert plan.lot_box() == (-40.0, -35.0, 80.0, 65.0)
    # front→south, back→north, left→west, right→east
    assert plan.setbacks == {"south": 25.0, "north": 20.0, "west": 10.0, "east": 10.0}


def test_unknown_setback_side_is_a_parse_error():
    r = _compile("setback sideways 40\n")
    assert not r.ok
    assert "BAD_OPTION" in _codes(r, "error")


def test_lot_render_includes_parcel():
    svg = render_svg(_compile("lot 200 x 300 at -70,-130\nsetback front 40\n").plan)
    assert "LOT 200×300" in svg


def test_negative_lot_size_rejected():
    with pytest.raises(ValueError):
        barndominium("x").envelope(width=40, length=30).set_lot(-10, 50)
