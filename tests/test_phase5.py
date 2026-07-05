"""Phase 5 — feet-and-inches literals, safety glazing (IRC R308.4), placeable
smoke/CO alarms (IRC R314/R315), and the fixture round-trip through `emit`.

Covers the four features end to end: the ft-in lexer positives and the
disambiguation negatives; the WINDOW_TEMPERED warning + the window schedule's
Glazing column (one shared predicate); the `alarm` statement across model, parse,
emit, checks, render, edits and packet; and the fixture/alarm emit round-trip.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl
from barndsl import RoomType as T
from barndsl import validate
from barndsl.diagnostics import REGISTRY
from barndsl.edits import apply_edit, edit_from_json
from barndsl.packet import build_packet
from barndsl.render import RenderConfig, render_svg
from barndsl.schedule import window_rows


def _codes(plan_or_result) -> set[str]:
    issues = (
        plan_or_result.diagnostics
        if hasattr(plan_or_result, "diagnostics")
        else validate(plan_or_result).issues
    )
    return {i.code for i in issues}


# ===========================================================================
# 1. Fixture round-trip through emit
# ===========================================================================

_FIXTURE_SRC = """\
plan "Fx"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 24 x 30
room bath: bathroom east-of living size 10 x 12
entry living south width 3 offset 10
fixture sofa in living at 3,4 rotate 90 width 7
fixture desk in living wall N
fixture bed_queen in living
"""


def test_authored_fixtures_round_trip_through_emit():
    r = compile_source(_FIXTURE_SRC)
    assert r.plan is not None
    src2 = emit_dsl(r.plan)
    assert "fixture sofa in living at 3,4 rotate 90 width 7" in src2
    assert "fixture desk in living wall N" in src2
    assert "fixture bed_queen in living" in src2
    again = compile_source(src2, name=r.plan.name)
    before = [(f.kind, f.room, f.x, f.y, f.wall, f.rotation, f.width) for f in r.plan.fixtures]
    after = [(f.kind, f.room, f.x, f.y, f.wall, f.rotation, f.width) for f in again.plan.fixtures]
    assert before == after
    assert emit_dsl(again.plan) == src2  # a fixed point


def test_auto_seeds_are_absent_from_emit():
    # The bath seeds a toilet / lavatory / tub with no `fixture` line; emit carries
    # only the author's fixtures, never the derived seeds.
    r = compile_source(_FIXTURE_SRC)
    src2 = emit_dsl(r.plan)
    fixture_lines = [ln for ln in src2.splitlines() if ln.startswith("fixture ")]
    assert all("in living" in ln for ln in fixture_lines)  # no bath seeds emitted
    assert "toilet" not in src2 and "lavatory" not in src2


def test_no_fixtures_emits_no_fixture_lines():
    r = compile_source("plan \"x\"\nenvelope 20 x 16\nceiling 9\n"
                       "room living: living at 0,0 size 20 x 16\n"
                       "entry living south width 3 offset 8\n")
    assert "fixture" not in emit_dsl(r.plan)


# ===========================================================================
# 2. Feet-and-inches literals
# ===========================================================================

def _len(expr: str) -> float:
    """Compile a plan whose north window offset is `expr`; return the parsed value."""
    src = (f'plan "x"\nenvelope 60 x 40\nceiling 9\n'
           f'room a: living at 0,0 size 40 x 30\n'
           f'entry a south width 3 offset 2\n'
           f'window a north width 2 offset {expr}\n')
    r = compile_source(src)
    assert r.plan is not None and not r.errors, [i.code for i in r.errors]
    return r.plan.windows[0].offset


def test_ft_in_positive_forms():
    assert _len("12-6") == 12.5
    assert _len("12′6″") == 12.5
    assert _len("12′") == 12.0
    assert _len("12'6") == 12.5
    assert _len("12'") == 12.0
    assert _len("6″") == 0.5
    assert _len("12") == 12.0
    assert _len("10.5") == 10.5


def test_ft_in_canonicalizes_to_decimal_feet_in_emit():
    r = compile_source('plan "x"\nenvelope 60 x 40\nceiling 10-6\n'
                       'room a: living at 0,0 size 20-6 x 15\n'
                       'entry a south width 3 offset 2\n')
    src = emit_dsl(r.plan)
    assert "ceiling 10.5" in src
    assert "size 20.5 x 15" in src
    assert "-" not in src.split("ceiling")[1].split("\n")[0]  # decimal, not ft-in


def test_ft_in_door_dash_separator_still_parses():
    # `door a - b` uses `-` as a separator (three tokens), NOT a ft-in literal.
    r = compile_source('plan "x"\nenvelope 40 x 30\nceiling 9\n'
                       'room a: living at 0,0 size 20 x 15\n'
                       'room b: kitchen east-of a size 15 x 15\n'
                       'door a - b width 8\n'
                       'entry a south width 3 offset 2\n')
    assert not r.errors
    assert len(r.plan.interior_doors) == 1


def test_ft_in_spaced_dash_is_not_a_length():
    # `12 - 6` is three tokens, so it is NOT a length — the parser rejects it.
    r = compile_source('plan "x"\nenvelope 40 x 30\nceiling 9\n'
                       'room a: living at 0,0 size 20 x 15\n'
                       'window a north width 12 - 6 offset 5\n'
                       'entry a south width 3 offset 2\n')
    assert any(i.severity.name == "ERROR" for i in r.diagnostics)


def test_ft_in_negative_position_still_works():
    r = compile_source('plan "x"\nenvelope 40 x 30\nceiling 9\n'
                       'room a: living at -6,0 size 20 x 15\n'
                       'entry a south width 3 offset 2\n')
    assert r.plan.rooms[0].x == -6.0


def test_ft_in_negative_feet_with_inches_is_rejected():
    r = compile_source('plan "x"\nenvelope 60 x 40\nceiling 9\n'
                       'room a: living at 0,0 size -12-6 x 15\n'
                       'entry a south width 3 offset 2\n')
    assert any(i.code == "BAD_NUMBER" for i in r.diagnostics)


def test_ft_in_inches_over_twelve_is_rejected():
    r = compile_source('plan "x"\nenvelope 60 x 40\nceiling 9\n'
                       'room a: living at 0,0 size 12-15 x 15\n'
                       'entry a south width 3 offset 2\n')
    assert any(i.code == "BAD_NUMBER" for i in r.diagnostics)


# ===========================================================================
# 3. Safety glazing (IRC R308.4)
# ===========================================================================

def _glazing(result, room: str) -> str:
    for row in window_rows(result.plan):
        if row["room"] == room:
            return row["glazing"]
    raise AssertionError(f"no window in {room}")


def test_window_tempered_near_door_sidelite():
    # A window immediately beside the entry door on the same wall — R308.4.1.
    r = compile_source('plan "x"\nenvelope 40 x 30\nceiling 9\n'
                       'room a: living at 0,0 size 24 x 20\n'
                       'entry a south width 3 offset 12\n'
                       'window a south width 4 offset 8\n')
    assert "WINDOW_TEMPERED" in _codes(r)
    assert _glazing(r, "a") == "tempered (required)"


def test_window_not_tempered_far_from_door():
    r = compile_source('plan "x"\nenvelope 40 x 30\nceiling 9\n'
                       'room a: living at 0,0 size 24 x 20\n'
                       'entry a south width 3 offset 2\n'
                       'window a south width 4 offset 16\n')
    assert "WINDOW_TEMPERED" not in _codes(r)
    assert _glazing(r, "a") == "—"


def test_window_tempered_in_wet_room_near_tub():
    r = compile_source('plan "x"\nenvelope 30 x 20\nceiling 9\n'
                       'room a: living at 0,0 size 20 x 12\n'
                       'room bath: bathroom east-of a size 8 x 8\n'
                       'entry a south width 3 offset 8\n'
                       'window bath east width 2 offset 3\n')
    assert "WINDOW_TEMPERED" in _codes(r)
    assert _glazing(r, "bath") == "tempered (required)"


def test_high_sill_bath_window_is_exempt():
    # A high privacy window (sill >= 60 in) is not a hazard — R308.4.5 exemption.
    r = compile_source('plan "x"\nenvelope 30 x 20\nceiling 9\n'
                       'room a: living at 0,0 size 20 x 12\n'
                       'room bath: bathroom east-of a size 8 x 8\n'
                       'entry a south width 3 offset 8\n'
                       'window bath east width 2 offset 3 sill 5\n')
    assert "WINDOW_TEMPERED" not in _codes(r)
    assert _glazing(r, "bath") == "—"


def test_window_tempered_near_stairs():
    r = compile_source('plan "x"\nenvelope 30 x 24\nceiling 9\n'
                       'room a: living at 0,0 size 24 x 20\n'
                       'stair s at 0,2 size 4 x 12 from 0 to 1\n'
                       'room loft: loft at 0,0 size 24 x 20 level 1\n'
                       'entry a south width 3 offset 12\n'
                       'window a west width 3 offset 4 sill 2\n')
    codes = [(i.code, i.room) for i in r.diagnostics if i.code == "WINDOW_TEMPERED"]
    assert ("WINDOW_TEMPERED", "a") in codes


def test_window_tempered_registered():
    assert REGISTRY["WINDOW_TEMPERED"].severity.name == "WARNING"


# ===========================================================================
# 4. Placeable smoke/CO alarms (IRC R314/R315)
# ===========================================================================

_ALARM_SRC = """\
plan "Al"
envelope 44 x 24
ceiling 9
room bed: bedroom at 0,0 size 14 x 12
room hall: hallway east-of bed size 6 x 12
room living: living east-of hall size 20 x 12
door bed - hall width 3
door hall - living width 3
entry living south width 3 offset 8
window bed south width 4 offset 3
{extra}
"""


def _alarm(extra: str):
    return compile_source(_ALARM_SRC.format(extra=extra))


def test_alarm_parses_all_kinds_and_position():
    r = _alarm("alarm smoke in bed\nalarm co in hall\n"
               "alarm smoke_co in living at 5,5\n")
    assert [(a.kind, a.room, a.x, a.y) for a in r.plan.alarms] == [
        ("smoke", "bed", None, None),
        ("co", "hall", None, None),
        ("smoke_co", "living", 5.0, 5.0),
    ]


def test_combo_spelling_is_smoke_co_only():
    ok = _alarm("alarm smoke_co in bed\n")
    assert not ok.errors
    bad = _alarm("alarm combo in bed\n")
    assert any(i.code == "BAD_OPTION" for i in bad.diagnostics)


def test_alarms_round_trip_through_emit():
    r = _alarm("alarm smoke in bed\nalarm smoke_co in hall at 3,4\n")
    src2 = emit_dsl(r.plan)
    assert "alarm smoke in bed" in src2
    assert "alarm smoke_co in hall at 3,4" in src2
    again = compile_source(src2, name=r.plan.name)
    assert [(a.kind, a.room, a.x, a.y) for a in again.plan.alarms] == \
           [(a.kind, a.room, a.x, a.y) for a in r.plan.alarms]
    assert emit_dsl(again.plan) == src2


def test_no_alarms_with_bedrooms_gets_reminder():
    r = _alarm("")
    assert "ALARM_CO" in _codes(r)


def test_alarm_bedroom_when_bedroom_lacks_smoke():
    r = _alarm("alarm smoke in hall\n")  # hall covered, bed not
    assert "ALARM_BEDROOM" in _codes(r)


def test_alarm_hall_when_no_adjacent_alarm():
    # bed carries a smoke alarm but nothing adjacent to it does.
    r = _alarm("alarm smoke in bed\n")
    assert "ALARM_HALL" in _codes(r)


def test_fully_alarmed_single_level_is_clean():
    r = _alarm("alarm smoke in bed\nalarm smoke in hall\n")
    assert not {c for c in _codes(r) if c.startswith("ALARM")}


def test_alarm_level_flags_a_bare_storey():
    src = ('plan "x"\nenvelope 24 x 24\nceiling 9\n'
           'room bed: bedroom at 0,0 size 14 x 12\n'
           'room hall: hallway east-of bed size 6 x 12\n'
           'room loft: loft at 0,0 size 20 x 12 level 1\n'
           'door bed - hall width 3\n'
           'stair s at 20,0 size 4 x 12 from 0 to 1\n'
           'entry hall south width 3 offset 2\n'
           'alarm smoke in bed\nalarm smoke in hall\n')  # nothing on level 1
    r = compile_source(src)
    assert "ALARM_LEVEL" in _codes(r)


def test_alarm_co_info_for_garage_without_co():
    src = ('plan "x"\nenvelope 60 x 24\nceiling 9\n'
           'room bed: bedroom at 0,0 size 14 x 12\n'
           'room hall: hallway east-of bed size 6 x 12\n'
           'room garage: garage at 30,0 size 30 x 24\n'
           'door bed - hall width 3\ndoor hall - garage width 3\n'
           'entry hall south width 3 offset 2\n'
           'alarm smoke in bed\nalarm smoke in hall\n')
    r = compile_source(src)
    assert "ALARM_CO" in _codes(r)
    # A CO alarm outside the bedrooms clears it.
    r2 = compile_source(src + "alarm co in hall\n")
    assert "ALARM_CO" not in _codes(r2)


def test_alarm_codes_registered():
    for code, sev in (("ALARM_BEDROOM", "WARNING"), ("ALARM_HALL", "WARNING"),
                      ("ALARM_LEVEL", "WARNING"), ("ALARM_CO", "INFO")):
        assert REGISTRY[code].severity.name == sev


def test_alarm_renders_sd_and_co_symbols():
    r = _alarm("alarm smoke in bed\nalarm co in hall\nalarm smoke_co in living\n")
    svg = render_svg(r.plan, RenderConfig(show_electrical=True))
    assert ">SD<" in svg
    assert ">CO<" in svg
    assert "SD/CO" in svg


def test_add_and_delete_alarm_edits_round_trip():
    base = _ALARM_SRC.format(extra="")
    add = apply_edit(base, edit_from_json({"kind": "add_alarm", "room": "bed",
                                           "akind": "smoke"}))
    assert add.ok and "alarm smoke in bed" in add.source
    r2 = compile_source(add.source)
    assert len(r2.plan.alarms) == 1
    delete = apply_edit(add.source, edit_from_json(
        {"kind": "delete_alarm", "index": 0}))
    assert delete.ok and "alarm smoke in bed" not in delete.source


def test_builder_add_alarm_rejects_bad_kind():
    plan = barndominium("x").envelope(20, 16).ceiling(9).add_room(
        "bed", T.BEDROOM, x=0, y=0, width=14, length=12)
    try:
        plan.add_alarm("bed", "combo")
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError for an unknown alarm kind")


def test_packet_counts_alarms():
    r = _alarm("alarm smoke in bed\nalarm co in hall\nalarm smoke_co in living\n")
    html = build_packet(r)
    assert "Smoke alarms" in html and "CO alarms" in html


# ===========================================================================
# 5. Playground affordances + offline guarantee
# ===========================================================================

def test_payload_carries_alarms():
    from barndsl.playground import compile_payload

    payload = compile_payload(_ALARM_SRC.format(extra="alarm smoke in bed\n"))
    alarms = payload["electrical"]["alarms"]
    assert alarms and alarms[0]["kind"] == "smoke" and alarms[0]["room"] == "bed"


def test_playground_markup_and_offline():
    from barndsl.playground import render_app

    html = render_app("plan \"x\"\n")
    assert "smoke alarm" in html and "smoke+CO alarm" in html
    assert "add_alarm" in html
    assert "http://" not in html and "https://" not in html
