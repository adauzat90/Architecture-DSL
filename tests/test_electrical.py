"""Tests for the electrical layer (Phase 4): the `outlet` / `switch` / `light`
statements, the per-room code checks (OUTLET_SPACING / OUTLET_GFCI /
ROOM_NO_LIGHT), the render layer, the surgical edits, and the packet sheet.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl
from barndsl.diagnostics import REGISTRY, explain
from barndsl.edits import apply_edit, edit_from_json
from barndsl.packet import build_packet
from barndsl.render import RenderConfig, render_svg


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# A living room big enough that two nearby outlets leave a wide gap, plus a wet
# kitchen; `{extra}` slots electrical lines in.
_SRC = """\
plan "Elec"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 28 x 30
room kit: kitchen east-of living size 12 x 30
{extra}
entry living south width 3 offset 10
window living west width 10 offset 8
window kit east width 6 offset 8
"""


def _compile(extra: str):
    return compile_source(_SRC.format(extra=extra))


# --- DEVICE_ROOM -------------------------------------------------------------


def test_device_in_unknown_room_is_a_device_room_error():
    r = _compile(
        "outlet in livng wall S offset 3\n"
        "switch in livng wall E offset 1\n"
        "light in livng at 12,8\n"
        "alarm smoke in livng"
    )
    hits = [d for d in r.errors if d.code == "DEVICE_ROOM"]
    assert [d.line for d in hits] == [6, 7, 8, 9]
    assert "livng" in hits[0].message and hits[0].col is not None


def test_device_in_known_room_has_no_device_room_error():
    r = _compile(
        "outlet in living wall S offset 3\n"
        "switch in living wall E offset 1\n"
        "light in living at 12,8\n"
        "alarm smoke in living"
    )
    assert "DEVICE_ROOM" not in _codes(r, "error")


# --- parsing -----------------------------------------------------------------


def test_outlet_switch_light_parse_onto_the_plan():
    r = _compile(
        "outlet in living wall S offset 3\n"
        "outlet in kit wall N offset 2 gfci\n"
        "switch in living wall E offset 1\n"
        "light in living at 12,8 kind pendant"
    )
    assert r.plan is not None
    assert len(r.plan.outlets) == 2 and len(r.plan.switches) == 1
    assert len(r.plan.lights) == 1
    o = r.plan.outlets[1]
    assert o.room == "kit" and o.wall.value == "north" and o.offset == 2.0 and o.gfci
    assert r.plan.lights[0].kind == "pendant"


def test_light_defaults_to_ceiling_kind():
    r = _compile("light in living at 12,8")
    assert r.plan.lights[0].kind == "ceiling"


def test_full_wall_names_and_letters_both_accepted():
    r = _compile("outlet in living wall south offset 3")
    assert r.plan.outlets[0].wall.value == "south"


def test_outlet_needs_a_wall():
    r = _compile("outlet in living offset 3")
    assert not r.ok and "BAD_WALL" in _codes(r, "error")


def test_unknown_light_kind_is_a_bad_option():
    r = _compile("light in living at 12,8 kind chandelier")
    assert "BAD_OPTION" in _codes(r, "error")


def test_gfci_is_only_an_outlet_option():
    r = _compile("switch in living wall E offset 1 gfci")
    assert "BAD_OPTION" in _codes(r, "error")


# --- receptacle spacing (IRC E3901.2 → OUTLET_SPACING) -----------------------


def test_spacing_warns_when_a_wall_point_is_far_from_an_outlet():
    # One outlet in a 28x30 room leaves most of the perimeter out of reach.
    r = _compile("outlet in living wall S offset 3\nlight in living at 14,15")
    assert "OUTLET_SPACING" in _codes(r, "warning"), r.report()
    msg = next(d for d in r.warnings if d.code == "OUTLET_SPACING").message
    assert "Living" in msg and "6 ft" in msg


def test_spacing_silent_when_outlets_are_close_enough():
    # A 10x10 room (perimeter 40 ft): one outlet centred on each wall keeps every
    # wall point within 5 ft of a receptacle.
    src = """\
plan "Snug"
envelope 10 x 10
ceiling 9
room living: living at 0,0 size 10 x 10
outlet in living wall S offset 5
outlet in living wall E offset 5
outlet in living wall N offset 5
outlet in living wall W offset 5
light in living at 5,5
entry living south width 3 offset 3
window living west width 4 offset 3
"""
    r = compile_source(src)
    assert "OUTLET_SPACING" not in _codes(r, "warning"), r.report()


def test_spacing_only_checks_rooms_that_declared_an_outlet():
    # No outlets anywhere → the check never fires (drawing electrical is opt-in).
    r = _compile("")
    assert "OUTLET_SPACING" not in _codes(r, "warning")


# --- GFCI (IRC E3902 → OUTLET_GFCI) ------------------------------------------


def test_wet_room_outlet_without_gfci_warns():
    r = _compile("outlet in kit wall N offset 2")
    assert "OUTLET_GFCI" in _codes(r, "warning")
    d = next(d for d in r.warnings if d.code == "OUTLET_GFCI")
    assert d.room == "kit"


def test_wet_room_outlet_with_gfci_is_clean():
    r = _compile("outlet in kit wall N offset 2 gfci")
    assert "OUTLET_GFCI" not in _codes(r, "warning")


def test_dry_room_outlet_never_needs_gfci():
    r = _compile("outlet in living wall S offset 3")
    assert "OUTLET_GFCI" not in _codes(r, "warning")


# --- lighting outlet (IRC E3903 → ROOM_NO_LIGHT) -----------------------------


def test_habitable_room_with_power_but_no_light_is_an_info():
    r = _compile("switch in living wall E offset 1")
    assert "ROOM_NO_LIGHT" in _codes(r, "info")
    assert next(d for d in r.infos if d.code == "ROOM_NO_LIGHT").room == "living"


def test_room_with_a_light_does_not_trip_no_light():
    r = _compile("switch in living wall E offset 1\nlight in living at 14,15")
    assert "ROOM_NO_LIGHT" not in _codes(r, "info")


# --- ELECTRICAL_PLAN gating --------------------------------------------------


def test_electrical_directive_alone_keeps_the_checklist_reminder():
    r = _compile("electrical")
    assert "ELECTRICAL_PLAN" in _codes(r, "info")


def test_drawing_outlets_supersedes_the_checklist():
    r = _compile("electrical\noutlet in living wall S offset 3\nlight in living at 14,15")
    assert "ELECTRICAL_PLAN" not in _codes(r, "info")


# --- emit round-trip ---------------------------------------------------------


def test_electrical_round_trips_through_emit():
    r = _compile(
        "outlet in living wall S offset 3\n"
        "outlet in kit wall N offset 2 gfci\n"
        "switch in living wall E offset 1\n"
        "light in living at 12,8 kind pendant"
    )
    src2 = emit_dsl(r.plan)
    assert "outlet in living wall S offset 3" in src2
    assert "outlet in kit wall N offset 2 gfci" in src2
    assert "switch in living wall E offset 1" in src2
    assert "light in living at 12,8 kind pendant" in src2
    again = compile_source(src2, name=r.plan.name)
    assert emit_dsl(again.plan) == src2  # a fixed point


def test_no_electrical_emits_nothing():
    r = _compile("")
    src = emit_dsl(r.plan)
    assert "outlet" not in src and "switch" not in src and "light" not in src


# --- builder API -------------------------------------------------------------


def test_builder_add_outlet_switch_light():
    plan = (
        barndominium("B")
        .envelope(40, 30)
        .ceiling(9)
        .add_room("living", "living", x=0, y=0, width=40, length=30)
        .add_outlet("living", "south", offset=3, gfci=False)
        .add_switch("living", "east", offset=1)
        .add_light("living", x=20, y=15, kind="recessed")
    )
    assert plan.outlets[0].offset == 3 and plan.lights[0].kind == "recessed"


def test_builder_rejects_a_bad_light_kind():
    import pytest

    with pytest.raises(ValueError):
        barndominium("B").add_light("living", x=1, y=1, kind="strobe")


# --- render layer ------------------------------------------------------------


def test_electrical_layer_off_by_default_on_by_flag():
    r = _compile(
        "outlet in living wall S offset 3 gfci\n"
        "switch in living wall E offset 1\n"
        "light in living at 14,15"
    )
    plain = render_svg(r.plan)
    assert 'data-layer="electrical"' not in plain
    lit = render_svg(r.plan, RenderConfig(show_electrical=True))
    assert 'data-layer="electrical"' in lit
    assert "GFCI" in lit  # the gfci tag
    assert ">S<" in lit  # the switch glyph


# --- surgical edits ----------------------------------------------------------


def test_add_and_delete_electrical_edits_round_trip():
    src = _SRC.format(extra="")
    for ed in (
        {"kind": "add_outlet", "room": "living", "wall": "S", "offset": 3},
        {"kind": "add_switch", "room": "living", "wall": "E", "offset": 1},
        {"kind": "add_light", "room": "living", "x": 14, "y": 15, "lkind": "pendant"},
    ):
        res = apply_edit(src, edit_from_json(ed))
        assert res.ok, res.error
        src = res.source
    r = compile_source(src)
    assert len(r.plan.outlets) == 1 and len(r.plan.switches) == 1
    assert len(r.plan.lights) == 1
    # Delete the outlet (index 0).
    res = apply_edit(src, edit_from_json({"kind": "delete_outlet", "index": 0}))
    assert res.ok, res.error
    assert "outlet" not in res.source


def test_add_outlet_edit_needs_a_valid_wall():
    src = _SRC.format(extra="")
    res = apply_edit(src, edit_from_json({"kind": "add_outlet", "room": "living",
                                          "wall": "X", "offset": 3}))
    assert not res.ok and res.error.kind == "bad_value"


# --- packet ------------------------------------------------------------------


def test_packet_gains_an_electrical_sheet_only_when_declared():
    without = build_packet(_compile(""))
    assert "Electrical Plan" not in without
    with_elec = build_packet(_compile(
        "outlet in living wall S offset 3 gfci\nlight in living at 14,15"
    ))
    assert "Electrical Plan" in with_elec
    assert "Device count" in with_elec
    assert 'data-layer="electrical"' in with_elec


# --- registry ----------------------------------------------------------------


def test_electrical_codes_are_registered():
    for code in ("OUTLET_SPACING", "OUTLET_GFCI", "ROOM_NO_LIGHT"):
        assert code in REGISTRY
        assert "Unknown" not in explain(code)
