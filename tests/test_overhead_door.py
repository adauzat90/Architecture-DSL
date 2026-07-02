"""Tests for `door <id> <wall> overhead` — overhead/sectional garage doors.

An overhead door is vehicle access on a garage/shop bay: it parses with 9 x 7
defaults, is never an egress door or a building entrance (a plan with only an
overhead door still errors NO_ENTRY), nudges to stock sectional sizes, renders
as a gap with a dashed track (no swing arc), and rides through the Revit
exchange as kind "overhead" so the builder can pick a garage-door family.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))  # for revit_fakes
import revit_fakes  # noqa: E402

revit_fakes.install()  # MUST run before importing the builder

_LIB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "revit", "barndsl.extension", "lib"
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import builder, report  # noqa: E402
from revit_fakes import BuiltInCategory as BIC  # noqa: E402
from revit_fakes import FakeDocument, WallFunction  # noqa: E402

from barndsl import (  # noqa: E402
    compile_source,
    emit_dsl,
    exchange_to_plan,
    render_svg,
    to_revit_model,
)
from barndsl.elements import OVERHEAD_DOOR_HEIGHT, OVERHEAD_DOOR_WIDTH  # noqa: E402


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


_SHOP_PLAN = """\
plan "Shop Door"
envelope 50 x 30
ceiling 10
room living: living at 0,0  size 30 x 30
room shop:   shop   at 30,0 size 20 x 30
door living - shop width 3
entry living south width 3 offset 4
window living south width 8 offset 10
door shop south overhead width 9 offset 4
"""


# --- parsing + defaults --------------------------------------------------------


def test_overhead_parses_onto_the_exterior_door():
    r = compile_source(_SHOP_PLAN)
    assert r.ok, r.report()
    (xd,) = [d for d in r.plan.exterior_doors if d.kind == "overhead"]
    assert xd.room == "shop" and xd.wall.value == "south"
    assert xd.width == 9 and xd.height == 7
    assert xd.offset == 4
    assert xd.overhead is True
    assert xd.egress is False  # implied — an overhead door is never egress


def test_overhead_defaults_to_the_nine_by_seven_single():
    src = _SHOP_PLAN.replace(
        "door shop south overhead width 9 offset 4", "door shop south overhead"
    )
    r = compile_source(src)
    assert r.ok, r.report()
    (xd,) = [d for d in r.plan.exterior_doors if d.kind == "overhead"]
    assert xd.width == OVERHEAD_DOOR_WIDTH == 9
    assert xd.height == OVERHEAD_DOOR_HEIGHT == 7


def test_overhead_rejects_no_egress_option():
    # There is no egress knob on an overhead door — no-egress is implied.
    src = _SHOP_PLAN.replace(
        "door shop south overhead width 9 offset 4",
        "door shop south overhead no-egress",
    )
    r = compile_source(src)
    bad = [d for d in r.errors if d.code == "BAD_OPTION"]
    assert bad and "implied" in (bad[0].hint or "")


def test_overhead_round_trips_through_emit():
    plan = compile_source(_SHOP_PLAN).plan
    src = emit_dsl(plan)
    assert "door shop south overhead width 9 height 7 offset 4" in src
    back = compile_source(src)
    assert back.ok, back.report()
    (xd,) = [d for d in back.plan.exterior_doors if d.kind == "overhead"]
    assert (xd.width, xd.height, xd.egress) == (9, 7, False)


# --- validation ----------------------------------------------------------------


def test_only_an_overhead_door_still_errors_no_entry():
    src = """\
plan "Only Overhead"
envelope 30 x 30
ceiling 10
room shop: shop at 0,0 size 30 x 30
door shop south overhead
"""
    r = compile_source(src)
    errs = _codes(r, "error")
    assert "NO_ENTRY" in errs
    # The overhead door still makes the shop reachable — no NO_ACCESS cascade.
    assert "NO_ACCESS" not in errs


def test_a_people_entry_beside_the_overhead_clears_no_entry():
    r = compile_source(_SHOP_PLAN)
    assert "NO_ENTRY" not in _codes(r, "error")
    # But the overhead door doesn't count as the house's back door.
    assert "NO_BACK_DOOR" in _codes(r, "info")


def test_overhead_room_notes_a_bedroom():
    src = """\
plan "Bedroom Bay"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 18 x 24
room bed:    bedroom at 18,0 size 12 x 24
door living - bed width 2.67
entry living south width 3 offset 4
door bed north overhead
"""
    r = compile_source(src)
    assert "OVERHEAD_ROOM" in _codes(r, "info")
    # The same door on the shop plan (a garage-type room) doesn't note.
    assert "OVERHEAD_ROOM" not in _codes(compile_source(_SHOP_PLAN), "info")


def test_overhead_header_fires_over_ten_feet_but_not_at_nine():
    wide = _SHOP_PLAN.replace(
        "door shop south overhead width 9 offset 4",
        "door shop south overhead width 12 offset 4",
    )
    assert "OVERHEAD_HEADER" in _codes(compile_source(wide), "info")
    assert "OVERHEAD_HEADER" not in _codes(compile_source(_SHOP_PLAN), "info")


def test_off_standard_sectional_size_nudges_door_size():
    odd = _SHOP_PLAN.replace(
        "door shop south overhead width 9 offset 4",
        "door shop south overhead width 11 offset 4",
    )
    r = compile_source(odd)
    hits = [d for d in r.infos if d.code == "DOOR_SIZE"]
    assert hits and "sectional" in hits[0].message
    assert "width 10" in (hits[0].hint or "")  # 10 is the nearest stock width
    # Stock 9 x 7 stays quiet.
    assert "DOOR_SIZE" not in _codes(compile_source(_SHOP_PLAN), "info")


def test_post_in_opening_covers_a_sixteen_foot_overhead():
    src = """\
plan "Framed Shop"
envelope 48 x 30
ceiling 12
room shop: shop at 0,0 size 48 x 30
door shop south overhead width 16 offset 4
entry shop east width 3 offset 4
frame bay 12
"""
    r = compile_source(src)
    # The bay-12 grid drops a post at x=12 on the south wall — inside the 4..20
    # ft opening the double door spans.
    assert "POST_IN_OPENING" in _codes(r, "warning")


def test_garage_separation_checks_are_unaffected():
    r = compile_source(_SHOP_PLAN)
    infos = _codes(r, "info")
    assert "GARAGE_SEPARATION" in infos and "GARAGE_DOOR" in infos


def test_new_codes_are_registered():
    from barndsl.diagnostics import REGISTRY

    for code in ("OVERHEAD_ROOM", "OVERHEAD_HEADER"):
        assert code in REGISTRY
        assert REGISTRY[code].severity.value == "info"


# --- exchange ------------------------------------------------------------------


def test_exchange_carries_kind_height_and_egress_false():
    plan = compile_source(_SHOP_PLAN).plan
    doc = to_revit_model(plan).to_dict()
    (o,) = [o for o in doc["openings"] if o["kind"] == "overhead"]
    assert o["category"] == "door"
    assert o["width"] == 9 and o["height"] == 7
    assert o["exterior"] is True and o["egress"] is False
    assert o["rooms"] == ["shop"]
    # And it reconstructs as an overhead door, not an entry.
    back = exchange_to_plan(doc)
    (xd,) = [d for d in back.exterior_doors if d.kind == "overhead"]
    assert (xd.room, xd.width, xd.height, xd.egress) == ("shop", 9, 7, False)


# --- builder -------------------------------------------------------------------


def _ready_doc(**kw):
    doc = FakeDocument()
    doc.add_level(0.0, "Level 1")
    doc.add_wall_type("Ext", function=WallFunction.Exterior)
    doc.add_wall_type("Int", function=WallFunction.Interior)
    doc.add_floor_type("Generic 12")
    doc.add_roof_type("Gable Metal")
    doc.add_family(BIC.OST_Doors, "Single-Flush")
    doc.add_family(BIC.OST_Windows, "Fixed")
    if kw.get("garage_doors"):
        doc.add_family(BIC.OST_Doors, "Sectional-Garage")
    return doc


def _exchange(dsl: str) -> dict:
    plan = compile_source(dsl).plan
    assert plan is not None
    return to_revit_model(plan).to_dict()


def test_builder_prefers_a_garage_door_family_for_overhead():
    doc = _ready_doc(garage_doors=True)
    rep = builder.build(doc, _exchange(_SHOP_PLAN))
    assert rep.resources["garage_door_family"] == "Sectional-Garage"
    placed = [r for r in rep.records if r.kind == "door" and r.status == "created"]
    assert sum(r.message == "overhead (garage) door" for r in placed) == 1
    # The overhead door was sized from the garage family (a "barndsl WxH"
    # duplicate under Sectional-Garage), not the standard leaf family.
    garage_types = [
        s.Name for s in doc.symbols[BIC.OST_Doors] if s.Family.Name == "Sectional-Garage"
    ]
    assert any(n.startswith("barndsl 9.00x7.00") for n in garage_types)


def test_builder_overhead_falls_back_to_the_standard_door_with_a_note():
    doc = _ready_doc()  # only Single-Flush loaded
    rep = builder.build(doc, _exchange(_SHOP_PLAN))
    assert rep.resources["garage_door_family"] == "(standard door)"
    assert any("no garage-door family loaded" in n for n in rep.notes)
    # Interior door + entry + overhead all still land.
    assert rep.count(status="created", kind="door") == 3


def test_garage_door_family_override_round_trips_the_config():
    opts = report.BuildOptions.from_dict({"garage_door_family": "My Sectional"})
    assert opts.garage_door_family == "My Sectional"
    assert opts.to_dict()["garage_door_family"] == "My Sectional"


def test_named_garage_door_override_wins_over_the_name_hint():
    doc = _ready_doc(garage_doors=True)
    doc.add_family(BIC.OST_Doors, "Custom Shop Door")
    opts = report.BuildOptions(garage_door_family="Custom Shop Door")
    rep = builder.build(doc, _exchange(_SHOP_PLAN), opts)
    assert rep.resources["garage_door_family"] == "Custom Shop Door"


# --- render --------------------------------------------------------------------


def test_render_draws_a_dashed_track_and_no_swing_arc():
    plan = compile_source(_SHOP_PLAN).plan
    svg = render_svg(plan)
    # The overhead door's segmented-panel track: a dashed line inside the room.
    assert 'stroke-dasharray="5 3"' in svg
    # Swing arcs come only from the swing doors (1 interior + 1 entry) — the
    # overhead door adds none.
    arcs = re.findall(r"A [\d.]+ [\d.]+ 0 0 [01] ", svg)
    assert len(arcs) == 2
