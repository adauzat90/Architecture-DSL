"""Tests for the Phase 4 site-plan additions: the `building at <x>,<y>`
placement, the position-aware SETBACK check (naming the encroached side), the
`render_site_svg` drawing, and the packet Site Plan sheet.

The `site` / `setback` grammar and the dimension-only fit check are covered by
`test_site.py`; this file exercises what `building at` and the site drawing add.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl, render_site_svg
from barndsl.packet import build_packet


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


_SRC = """\
plan "Lot"
envelope 40 x 30
ceiling 9
site 120 x 90
setback front 25 side 10 rear 20
{extra}
room living: living at 0,0 size 40 x 30
entry living south width 3 offset 10
window living west width 10 offset 8
"""


def _compile(extra: str):
    return compile_source(_SRC.format(extra=extra))


# --- parsing / round-trip ----------------------------------------------------


def test_building_at_parses_onto_the_site_spec():
    r = _compile("building at 40,30")
    ss = r.plan.site_spec
    assert ss.has_building and (ss.building_x, ss.building_y) == (40.0, 30.0)


def test_building_round_trips_through_emit():
    r = _compile("building at 40,30")
    src2 = emit_dsl(r.plan)
    assert "building at 40,30" in src2
    again = compile_source(src2, name=r.plan.name)
    assert emit_dsl(again.plan) == src2  # a fixed point


def test_no_building_emits_nothing():
    r = _compile("")
    assert "building at" not in emit_dsl(r.plan)


def test_building_needs_the_at_keyword():
    r = _compile("building 40 30")
    assert not r.ok


def test_builder_building_placement():
    plan = (
        barndominium("B").envelope(40, 30).ceiling(9)
        .site(120, 90).setback(front=25, side=10, rear=20).building(40, 30)
        .add_room("living", "living", x=0, y=0, width=40, length=30)
    )
    assert plan.site_spec.building_x == 40 and plan.site_spec.building_y == 30


# --- position-aware SETBACK check --------------------------------------------


def test_placed_building_within_all_setbacks_is_clean():
    # 40x30 on a 120x90 lot at (40,30): W=40, E=40, front=30, rear=30 — all clear.
    r = _compile("building at 40,30")
    assert "SETBACK" not in _codes(r, "error"), r.report()


def test_placed_building_naming_the_encroached_side():
    # Shoved west (x=5) → west yard 5 ft < 10 ft side setback.
    r = _compile("building at 5,30")
    setback = [d for d in r.errors if d.code == "SETBACK"]
    assert setback, r.report()
    assert "west side" in setback[0].message
    assert "5 ft" in setback[0].message or "short" in setback[0].message


def test_placed_building_off_the_lot_is_flagged():
    # y = -5 pushes the south edge past the lot line entirely.
    r = _compile("building at 40,-5")
    setback = [d for d in r.errors if d.code == "SETBACK"]
    assert setback and "front (south)" in setback[0].message


def test_placed_setback_error_located_on_the_setback_line():
    r = _compile("building at 5,30")
    d = [x for x in r.errors if x.code == "SETBACK"][0]
    assert d.line == 5  # the `setback` statement line in _SRC


# --- render ------------------------------------------------------------------


def test_render_site_svg_draws_lot_setbacks_and_building():
    r = _compile("building at 40,30")
    svg = render_site_svg(r.plan)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "Site Plan" in svg
    assert "BUILDING" in svg
    assert "setback" in svg  # a labelled dashed setback line
    assert "stroke-dasharray" in svg


def test_render_site_svg_marks_the_street_side():
    r = _compile("building at 40,30\nstreet south")
    assert "STREET" in render_site_svg(r.plan)


def test_render_site_svg_without_a_site_is_a_placeholder():
    r = compile_source(
        'plan "P"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 40 x 30\n"
        "entry living south width 3 offset 10\n"
    )
    svg = render_site_svg(r.plan)
    assert "No site declared" in svg


def test_site_plan_centres_the_building_when_unplaced():
    # No `building at`: the drawing still renders, centring the footprint.
    r = _compile("")
    assert "BUILDING" in render_site_svg(r.plan)


# --- packet ------------------------------------------------------------------


def test_packet_gains_a_site_sheet_only_when_a_lot_is_declared():
    with_site = build_packet(_compile("building at 40,30"))
    assert "Site Plan" in with_site and "Lot &amp; setbacks" in with_site

    no_site = build_packet(compile_source(
        'plan "P"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 40 x 30\n"
        "entry living south width 3 offset 10\n"
        "window living west width 10 offset 8\n"
    ))
    assert "Site Plan" not in no_site
