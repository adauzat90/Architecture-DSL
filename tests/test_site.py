"""Tests for the `site` / `setback` statements and the SETBACK check (§2.4).

A `site <W> x <L>` declares the lot; `setback [front <n>] [side <n>] [rear <n>]`
the required yard clearances. The buildable rectangle is the lot minus its
setbacks — front/rear consume the plan's north-south depth, `side` clears both
the east and west edges — and the building footprint (envelope + wings + porches)
must fit inside it (a dimensions-only check). Everything round-trips through emit
and the Revit exchange.
"""

from __future__ import annotations

from barndsl import (
    barndominium,
    compile_source,
    emit_dsl,
    exchange_to_plan,
    to_revit_model,
)
from barndsl.diagnostics import REGISTRY, explain


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# A compact valid plan; `site`/`setback` lines slot in via {extra}.
_SRC = """\
plan "Lot"
envelope 40 x 30
ceiling 9
{extra}
room living: living at 0,0 size 40 x 30
entry living south width 3 offset 10
window living west width 10 offset 8
"""


def _compile(extra: str):
    return compile_source(_SRC.format(extra=extra))


# --- parsing -----------------------------------------------------------------


def test_site_and_setback_parse_onto_the_plan():
    r = _compile("site 120 x 200\nsetback front 25 side 10 rear 20")
    ss = r.plan.site_spec
    assert (ss.width, ss.length) == (120.0, 200.0)
    assert (ss.front, ss.side, ss.rear) == (25.0, 10.0, 20.0)


def test_setback_accepts_any_subset():
    r = _compile("site 120 x 200\nsetback front 25")
    ss = r.plan.site_spec
    assert ss.front == 25.0 and ss.side is None and ss.rear is None
    assert r.ok, r.report()  # 40x30 footprint fits 120x150 buildable


def test_setback_order_independent():
    r = _compile("site 100 x 100\nsetback rear 5 front 5 side 5")
    ss = r.plan.site_spec
    assert (ss.front, ss.side, ss.rear) == (5.0, 5.0, 5.0)


def test_empty_setback_is_a_syntax_error():
    r = _compile("site 100 x 100\nsetback")
    assert r.plan is not None and not r.ok
    assert "SYNTAX" in _codes(r, "error")


def test_unknown_setback_edge_is_a_bad_option():
    # `left`/`right` aren't setback edges — the model has a single `side`.
    r = _compile("site 100 x 100\nsetback left 5")
    assert "BAD_OPTION" in _codes(r, "error")


def test_site_without_dimensions_needs_two_numbers():
    r = _compile("site 120")
    assert not r.ok
    assert "SYNTAX" in _codes(r, "error") or "BAD_NUMBER" in _codes(r, "error")


# --- the SETBACK fit check ---------------------------------------------------


def test_setback_silent_when_the_footprint_fits():
    # 40x30 building on a 120x200 lot with generous setbacks — plenty of room.
    r = _compile("site 120 x 200\nsetback front 25 side 10 rear 20")
    assert "SETBACK" not in _codes(r, "error"), r.report()


def test_setback_flags_a_footprint_too_wide():
    # buildable width = 50 - 2*8 = 34 < 40 ft footprint.
    r = _compile("site 50 x 200\nsetback side 8")
    setback = [d for d in r.errors if d.code == "SETBACK"]
    assert setback and "east-west" in setback[0].message


def test_setback_flags_a_footprint_too_deep():
    # buildable length = 60 - 25 - 20 = 15 < 30 ft footprint depth.
    r = _compile("site 200 x 60\nsetback front 25 rear 20")
    setback = [d for d in r.errors if d.code == "SETBACK"]
    assert setback and "north-south" in setback[0].message


def test_side_setback_applies_to_both_edges():
    # A single `side 6` reserves 6 ft on BOTH east and west: buildable = 50-12=38.
    fits = _compile("site 50 x 200\nsetback side 5")   # 50-10 = 40 == footprint
    assert "SETBACK" not in _codes(fits, "error"), fits.report()
    tight = _compile("site 50 x 200\nsetback side 6")  # 50-12 = 38 < 40
    assert "SETBACK" in _codes(tight, "error")


def test_setback_error_is_located_on_the_setback_line():
    r = _compile("site 50 x 200\nsetback side 8")
    setback = [d for d in r.errors if d.code == "SETBACK"][0]
    assert setback.line == 5  # the `setback` statement's line in _SRC


# --- porches count against the yard ------------------------------------------


def test_a_projecting_porch_counts_against_the_setback():
    # The building alone (40x30) fits, but a porch projecting 6 ft south past the
    # envelope pushes the footprint depth to 36 > the 34 ft buildable length.
    src = """\
plan "Porchy"
envelope 40 x 30
ceiling 9
site 200 x 40
setback front 3 rear 3
room living: living at 0,0 size 40 x 30
porch p at 0,-6 size 40 x 6 covered
entry living south width 3 offset 10
window living west width 10 offset 8
"""
    r = compile_source(src)
    assert "SETBACK" in _codes(r, "error"), r.report()


# --- setback without site ----------------------------------------------------


def test_setback_without_site_is_an_error():
    r = _compile("setback front 25 side 10")
    assert "SETBACK_NO_SITE" in _codes(r, "error")


def test_site_without_setback_imposes_no_check():
    # A lot with no setbacks never fires SETBACK, even for a huge building.
    r = _compile("site 10 x 10")  # smaller than the 40x30 building, but no setback
    assert "SETBACK" not in _codes(r, "error")
    assert "SETBACK_NO_SITE" not in _codes(r, "error")


# --- builder API -------------------------------------------------------------


def test_builder_site_and_setback():
    plan = (
        barndominium("B")
        .envelope(40, 30)
        .ceiling(9)
        .site(120, 200)
        .setback(front=25, side=10, rear=20)
        .add_room("living", "living", x=0, y=0, width=40, length=30)
        .entrance("living", "south", width=3, offset=10)
    )
    assert plan.site_spec.width == 120 and plan.site_spec.side == 10


# --- emit round-trip ---------------------------------------------------------


def test_site_and_setback_round_trip_through_emit():
    r = _compile("site 120 x 200\nsetback front 25 side 10 rear 20")
    src2 = emit_dsl(r.plan)
    assert "site 120 x 200" in src2
    assert "setback front 25 side 10 rear 20" in src2
    again = compile_source(src2, name=r.plan.name)
    assert emit_dsl(again.plan) == src2  # a fixed point


def test_partial_setback_omits_undeclared_edges_on_emit():
    r = _compile("site 120 x 200\nsetback front 25")
    line = next(l for l in emit_dsl(r.plan).splitlines() if l.startswith("setback"))
    assert line == "setback front 25"


def test_no_site_emits_nothing():
    r = _compile("")
    assert "site" not in emit_dsl(r.plan) and "setback" not in emit_dsl(r.plan)


# --- Revit exchange round-trip -----------------------------------------------


def test_site_reaches_the_exchange_and_round_trips():
    r = _compile("site 120 x 200\nsetback front 25 side 10 rear 20")
    data = to_revit_model(r.plan).to_dict()
    assert data["site"] == {
        "width": 120.0,
        "length": 200.0,
        "setbacks": {"front": 25.0, "side": 10.0, "rear": 20.0},
    }
    rt = exchange_to_plan(data)
    assert rt.site_spec.width == 120.0
    assert (rt.site_spec.front, rt.site_spec.side, rt.site_spec.rear) == (25.0, 10.0, 20.0)


def test_exchange_omits_the_site_key_when_undeclared():
    r = _compile("")
    data = to_revit_model(r.plan).to_dict()
    assert "site" not in data  # byte-identical to pre-site documents


def test_exchange_carries_a_partial_setback():
    r = _compile("site 80 x 90\nsetback front 15")
    data = to_revit_model(r.plan).to_dict()
    assert data["site"] == {"width": 80.0, "length": 90.0, "setbacks": {"front": 15.0}}
    rt = exchange_to_plan(data)
    assert rt.site_spec.front == 15.0 and rt.site_spec.side is None


# --- registry ----------------------------------------------------------------


def test_setback_codes_are_registered():
    for code in ("SETBACK", "SETBACK_NO_SITE"):
        assert code in REGISTRY
        assert "Unknown" not in explain(code)
