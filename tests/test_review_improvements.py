"""Tests for the diagnostic-review improvements:

* ``EGRESS_SIZE`` — a bedroom escape opening below the IRC R310 minimums.
* ``STAIR_RUN``   — a stair footprint too short for the storey it climbs.
* ``OPENING_CLASH`` — two openings overlapping on the same wall.
* the diagnostic code registry + ``explain`` + JSON output.
* ``ValidationReport.summary`` reporting the info count.
"""

from __future__ import annotations

import re
from pathlib import Path

from barndsl import barndominium, compile_source, validate
from barndsl.diagnostics import REGISTRY, explain


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- EGRESS_SIZE -------------------------------------------------------------

_EGRESS_BASE = """\
plan "Egress"
envelope 34 x 24
ceiling 9
room living: living   at 0,0  size 18 x 24
room bed:    bedroom  at 18,0 size 16 x 24
room bath:   bathroom at 0,0  size 0.001 x 0.001
door living - bed width 2.67
entry living south width 3 offset 4
window living west width 10 offset 8
{bed_window}
"""


def _egress(bed_window: str):
    # Drop the placeholder bath (keeps NO_BATH out of the way isn't critical here).
    src = _EGRESS_BASE.format(bed_window=bed_window).replace(
        "room bath:   bathroom at 0,0  size 0.001 x 0.001\n", ""
    )
    return compile_source(src)


def test_egress_size_warns_on_a_too_narrow_window():
    # 18 in wide (1.5 ft) is below the 20 in clear-width minimum.
    r = _egress("window bed east width 1.5 offset 4")
    assert "EGRESS_SIZE" in _codes(r, "warning")
    # The bedroom still *has* an opening, so it's not a hard BEDROOM_EGRESS error.
    assert "BEDROOM_EGRESS" not in _codes(r, "error")


def test_adequate_egress_window_does_not_warn():
    r = _egress("window bed east width 4 offset 4")
    assert "EGRESS_SIZE" not in _codes(r, "warning")


def test_missing_egress_is_still_a_hard_error_not_egress_size():
    r = _egress("window bed west width 4 offset 4")  # west abuts living — interior
    assert "BEDROOM_EGRESS" in _codes(r, "error")
    assert "EGRESS_SIZE" not in _codes(r, "warning")


def test_a_full_height_exterior_door_satisfies_egress_size():
    # A patio door is a valid escape opening and easily clears the width minimum.
    r = _egress("entry bed east width 3 offset 4")
    assert "EGRESS_SIZE" not in _codes(r, "warning")


def test_egress_size_via_high_sill_through_the_builder():
    # A transom-style window mounted too high (sill > 44 in) can't be an egress.
    plan = (
        barndominium("Transom")
        .envelope(width=30, length=24)
        .ceiling(9)
        .add_room("living", "living", x=0, y=0, width=14, length=24)
        .add_room("bed", "bedroom", x=14, y=0, width=16, length=24)
        .connect("living", "bed", width=2.67)
        .entrance("living", "south", width=3, offset=4)
        .add_window("living", "west", width=10, offset=8)
        .add_window("bed", "east", width=4, offset=4, sill_height=5.0, head_height=7.0)
    )
    report = validate(plan)
    assert "EGRESS_SIZE" in {i.code for i in report.warnings}


# --- STAIR_RUN ---------------------------------------------------------------

_STAIR_SRC = """\
plan "Two story"
envelope 40 x 30
ceiling {ceiling}
room living: living at 0,0 size 40 x 30
room loft:   loft   at 0,0 size 28 x 20 level 1
entry living south width 3 offset 10
window living west width 12 offset 8
window loft west width 10 offset 4
stair s at 24,2 size {stair} from 0 to 1
"""


def test_stair_run_warns_when_footprint_too_short():
    r = compile_source(_STAIR_SRC.format(ceiling=10, stair="4 x 6"))
    assert "STAIR_RUN" in _codes(r, "warning")


def test_stair_run_ok_for_a_full_length_flight():
    # A 9 ft storey needs ~10.8 ft of run; a 12 ft footprint clears it.
    r = compile_source(_STAIR_SRC.format(ceiling=9, stair="4 x 12"))
    assert "STAIR_RUN" not in _codes(r, "warning")


def test_stair_run_ok_for_a_switchback_footprint():
    # Wide enough (>= 6 ft) for two flights side by side: half the run suffices.
    r = compile_source(_STAIR_SRC.format(ceiling=10, stair="8 x 8"))
    assert "STAIR_RUN" not in _codes(r, "warning")


# --- OPENING_CLASH -----------------------------------------------------------

_CLASH_SRC = """\
plan "Clash"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 30 x 24
entry living south width 3 offset 8
{second}
"""


def test_opening_clash_on_overlapping_window_and_entry():
    r = compile_source(_CLASH_SRC.format(second="window living south width 10 offset 6"))
    assert "OPENING_CLASH" in _codes(r, "error")


def test_no_clash_when_openings_sit_apart():
    r = compile_source(_CLASH_SRC.format(second="window living south width 8 offset 14"))
    assert "OPENING_CLASH" not in _codes(r, "error")


def test_openings_on_different_walls_dont_clash():
    r = compile_source(_CLASH_SRC.format(second="window living east width 8 offset 6"))
    assert "OPENING_CLASH" not in _codes(r, "error")


def test_edge_touching_openings_do_not_clash():
    # entry occupies [8,11]; a window starting exactly at 11 just abuts it.
    r = compile_source(_CLASH_SRC.format(second="window living south width 6 offset 11"))
    assert "OPENING_CLASH" not in _codes(r, "error")


# --- summary info count ------------------------------------------------------


def test_summary_reports_info_count():
    plan = (
        barndominium("Counts")
        .envelope(width=30, length=24)
        .ceiling(9)
        .add_room("kitchen", "kitchen", x=0, y=0, width=30, length=24)
        .entrance("kitchen", "south", width=3, offset=4)
    )
    summary = validate(plan).summary()
    assert "info(s)" in summary
    assert re.search(r"\d+ error\(s\), \d+ warning\(s\), \d+ info\(s\)", summary)


# --- registry / explain / JSON ----------------------------------------------


def test_registry_covers_every_code_emitted_in_the_source():
    """Guard: no check ships without a registry entry (and an explanation)."""
    src_dir = Path(__file__).resolve().parent.parent / "src" / "barndsl"
    emitted: set[str] = set()
    for fname in ("validation.py", "compiler.py", "agent.py", "revitlog.py"):
        text = (src_dir / fname).read_text()
        # The code is the string literal right after a Severity.* or in a _ParseError.
        emitted.update(re.findall(r'Severity\.\w+,\s*"([A-Z_]{3,})"', text))
        emitted.update(re.findall(r'_ParseError\(\s*"([A-Z_]{3,})"', text))
    missing = sorted(emitted - set(REGISTRY))
    assert not missing, f"codes emitted but not in the registry: {missing}"


def test_explain_known_and_unknown_codes():
    assert "R310" in explain("BEDROOM_EGRESS")
    assert explain("egress_size").startswith("EGRESS_SIZE")  # case-insensitive
    assert "Unknown diagnostic code" in explain("NOPE")


def test_new_codes_are_registered():
    for code in ("EGRESS_SIZE", "STAIR_RUN", "OPENING_CLASH"):
        assert code in REGISTRY


# --- ROOM_TIGHT --------------------------------------------------------------

_TIGHT_SRC = """\
plan "Tight"
envelope 30 x 20
ceiling 9
room living: living    at 0,0   size 16 x 20
room kitchen: kitchen  at 16,0  size {kw} x 8
room bath:   bathroom  at 22,0  size 5 x 6
room powder: half_bath at 22,6  size 4 x 4
door living - kitchen width 2.67
door living - bath width 2.67
door living - powder width 2.67
entry living south width 3 offset 4
window living west width 12 offset 4
"""


def test_room_tight_flags_small_kitchen_and_full_bath():
    r = compile_source(_TIGHT_SRC.format(kw=6))  # 48 sq ft kitchen, 30 sq ft bath
    tight = {d.room for d in r.infos if d.code == "ROOM_TIGHT"}
    assert "kitchen" in tight
    assert "bath" in tight


def test_room_tight_flags_a_cramped_half_bath():
    # A 4x4 (16 sq ft) powder room is below the ~30 sq ft / 5 ft-short-side floor.
    r = compile_source(_TIGHT_SRC.format(kw=6))
    tight = {d.room for d in r.infos if d.code == "ROOM_TIGHT"}
    assert "powder" in tight


def test_room_tight_silent_for_a_workable_half_bath():
    # 5x6 = 30 sq ft, short side 5 ft — a fine powder room.
    src = _TIGHT_SRC.format(kw=10).replace(
        "room powder: half_bath at 22,6  size 4 x 4",
        "room powder: half_bath at 16,8  size 6 x 5",
    )
    tight = {d.room for d in compile_source(src).infos if d.code == "ROOM_TIGHT"}
    assert "powder" not in tight


def test_room_tight_silent_for_a_workable_kitchen():
    r = compile_source(_TIGHT_SRC.format(kw=10))  # 80 sq ft kitchen
    tight = {d.room for d in r.infos if d.code == "ROOM_TIGHT"}
    assert "kitchen" not in tight


# --- BATH_VENT ---------------------------------------------------------------


def test_bath_vent_flags_a_windowless_bath():
    r = compile_source(_TIGHT_SRC.format(kw=10))  # bath has no window
    assert "BATH_VENT" in _codes(r, "info")


def test_bath_vent_silent_with_an_exterior_window():
    # Give the bath a window on its south (exterior) wall.
    src = _TIGHT_SRC.format(kw=10) + "window bath south width 3 offset 1\n"
    vented = {d.room for d in compile_source(src).infos if d.code == "BATH_VENT"}
    assert "bath" not in vented


# --- HALL_DEADEND ------------------------------------------------------------

_HALL_SRC = """\
plan "Hall"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 20 x 24
room hall:   hallway at 20,0 size 4 x 24
room bed:    bedroom at 24,0 size 6 x 24
{doors}
entry {entry}
window living west width 12 offset 4
window bed east width 4 offset 4
"""


def _hall(doors: str, entry: str = "living south width 3 offset 4"):
    return compile_source(_HALL_SRC.format(doors=doors, entry=entry))


def test_hall_serving_two_rooms_is_not_a_deadend():
    # Doors at either end span the hall — no stub past the last doorway.
    r = _hall("door living - hall width 3 offset 0.5\ndoor hall - bed width 2.67 offset 20.83")
    assert "HALL_DEADEND" not in _codes(r, "info")


def test_hall_deadend_flags_doors_bunched_in_the_middle():
    # Both doors dead-centre of a 24 ft hall leave ~10 ft stubs at both ends.
    r = _hall("door living - hall width 3\ndoor hall - bed width 2.67")
    assert "HALL_DEADEND" in _codes(r, "info")


def test_hall_serving_one_room_is_a_deadend():
    # living reaches bed directly; the hall only touches bed → degree 1.
    r = _hall("door living - bed width 6\ndoor hall - bed width 2.67")
    assert "HALL_DEADEND" in _codes(r, "info")


def test_foyer_hall_with_an_entry_is_exempt():
    # The hall carries the exterior entry — a vestibule, not dead circulation.
    r = _hall("door hall - bed width 2.67", entry="hall south width 3 offset 1")
    deadends = {d.room for d in r.infos if d.code == "HALL_DEADEND"}
    assert "hall" not in deadends


def test_new_advisory_codes_are_registered():
    for code in ("ROOM_TIGHT", "BATH_VENT", "HALL_DEADEND"):
        assert code in REGISTRY


def test_compile_result_to_dict_is_machine_readable():
    r = compile_source(_CLASH_SRC.format(second="window living south width 10 offset 6"))
    data = r.to_dict()
    assert data["ok"] is False
    assert data["counts"]["error"] >= 1
    codes = {d["code"] for d in data["diagnostics"]}
    assert "OPENING_CLASH" in codes
    # Every diagnostic exposes the stable field set.
    for d in data["diagnostics"]:
        assert set(d) == {
            "code", "severity", "line", "col", "end_col", "room", "message", "hint"
        }


# --- window sill/head grammar + WINDOW_SILL ---------------------------------

_SILL_SRC = """\
plan "Sill"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 30 x 24
entry living south width 3 offset 4
window living west width 6 offset 6 {opts}
"""


def test_window_sill_and_head_parse_onto_the_plan():
    from barndsl import emit_dsl

    r = compile_source(_SILL_SRC.format(opts="sill 4 head 7"))
    w = r.plan.windows[0]
    assert (w.sill_height, w.head_height) == (4.0, 7.0)
    # Non-default sill/head round-trip through emit.
    line = next(l for l in emit_dsl(r.plan).splitlines() if l.startswith("window"))
    assert line.endswith("sill 4 head 7")
    assert compile_source(emit_dsl(r.plan)).plan is not None


def test_default_window_omits_sill_and_head_on_emit():
    from barndsl import emit_dsl

    r = compile_source(_SILL_SRC.format(opts=""))
    line = next(l for l in emit_dsl(r.plan).splitlines() if l.startswith("window"))
    assert "sill" not in line and "head" not in line


def test_window_sill_warns_when_head_not_above_sill():
    r = compile_source(_SILL_SRC.format(opts="sill 6 head 4"))
    assert "WINDOW_SILL" in _codes(r, "warning")


def test_window_sill_is_registered():
    assert "WINDOW_SILL" in REGISTRY


def test_a_high_sill_window_fails_bedroom_egress_size_via_dsl():
    # Now that sill/head are in the grammar, a transom egress can be expressed.
    src = """\
plan "Transom"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 14 x 24
room bed:    bedroom at 14,0 size 16 x 24
door living - bed width 2.67
entry living south width 3 offset 4
window living west width 10 offset 8
window bed east width 4 offset 4 sill 5 head 7
"""
    assert "EGRESS_SIZE" in _codes(compile_source(src), "warning")


# --- auto-layout windows a perimeter bath (clears BATH_VENT) -----------------


def test_auto_layout_windows_a_perimeter_bath():
    import os

    from barndsl import emit_dsl
    from barndsl.layout2 import parse_brief2, solve_layout2

    path = os.path.join(os.path.dirname(__file__), "..", "examples", "birch_run.brief")
    with open(path) as fh:
        out = solve_layout2(parse_brief2(fh.read()))
    r = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert "BATH_VENT" not in {d.code for d in r.infos}, r.report()
    baths = [
        room.id
        for room in out.plan.rooms
        if room.type.value in ("bathroom", "half_bath")
    ]
    assert baths and any(out.plan.windows_for(b) for b in baths)


# --- NO_CLOSET tightened: requires a door-connected closet -------------------

_CLOSET_SRC = """\
plan "Closet"
envelope 40 x 32
ceiling 10
room living: living  at 0,0  size 40 x 15
room hall:   hallway at 0,15 size 40 x 3
room bed1:   bedroom at 0,18  size 13 x 14
room closet1: closet at 13,18 size 4 x 14
room bed2:   bedroom at 17,18 size 13 x 14
room bath:   bathroom at 30,18 size 10 x 14
door living - hall width 4
door hall - bed1 width 3
door hall - bed2 width 3
door hall - bath width 2.67
door bed1 - closet1 width 2.5
{extra}
entry living south width 3 offset 4
window living south width 12 offset 4
window bed1 north width 5 offset 4
window bed2 north width 5 offset 4
window bath north width 3 offset 2
"""


def _no_closet_rooms(src):
    return {
        d.room for d in compile_source(src).infos if d.code == "NO_CLOSET"
    }


def test_no_closet_flags_an_abutting_closet_with_no_door():
    # bed2 abuts closet1 (bed1's) but has no door into any closet.
    rooms = _no_closet_rooms(_CLOSET_SRC.format(extra=""))
    assert "bed2" in rooms
    assert "bed1" not in rooms  # bed1 has its own door-connected closet


def test_no_closet_message_distinguishes_no_door_from_no_closet():
    r = compile_source(_CLOSET_SRC.format(extra=""))
    msg = next(d.message for d in r.infos if d.code == "NO_CLOSET" and d.room == "bed2")
    assert "no door into it" in msg


def test_no_closet_silent_with_a_door_connected_closet():
    # Give bed2 its own closet carved from the bath, with a door.
    src = _CLOSET_SRC.replace(
        "room bath:   bathroom at 30,18 size 10 x 14",
        "room closet2: closet at 30,18 size 4 x 14\n"
        "room bath:   bathroom at 34,18 size 6 x 14",
    ).format(extra="door bed2 - closet2 width 2.5")
    assert "bed2" not in {d.room for d in compile_source(src).infos if d.code == "NO_CLOSET"}


# --- MASTER_ENSUITE ----------------------------------------------------------

_SUITE_SRC = """\
plan "Suite"
envelope 50 x 32
ceiling 10
room living:  living   at 0,0   size 50 x 15
room hall:    hallway  at 0,15  size 50 x 3
room bed1:    bedroom  at 0,18   size 14 x 14
room closet1: closet   at 14,18  size 4 x 14
room master:  bedroom  at 18,18  size 18 x 14
room mcloset: closet   at 36,18  size 4 x 14
room bath1:   bathroom at 40,18  size 5 x 14
room bath2:   bathroom at 45,18  size 5 x 14
door living - hall width 4
door hall - bed1 width 3
door hall - master width 3
door bed1 - closet1 width 2.5
door master - mcloset width 2.5
{baths}
entry living south width 3 offset 4
window living south width 12 offset 4
window bed1 north width 5 offset 4
window master north width 6 offset 6
window bath1 north width 3 offset 1
window bath2 north width 3 offset 1
"""

# Both baths off the hall — master only shares them.
_SHARED_BATHS = "door hall - bath1 width 2.67\ndoor hall - bath2 width 2.67"
# bath2 opens only off the master — a true ensuite.
_ENSUITE = "door hall - bath1 width 2.67\ndoor master - bath2 width 2.67"


def test_master_ensuite_flags_two_baths_with_no_private_bath():
    r = compile_source(_SUITE_SRC.format(baths=_SHARED_BATHS))
    ensuite = {d.room for d in r.infos if d.code == "MASTER_ENSUITE"}
    assert "master" in ensuite


def test_master_ensuite_silent_when_master_has_a_private_bath():
    r = compile_source(_SUITE_SRC.format(baths=_ENSUITE))
    assert "MASTER_ENSUITE" not in {d.code for d in r.infos}


def test_master_ensuite_does_not_apply_with_a_single_bath():
    # Drop bath2 entirely → only one full bath, rule doesn't trigger.
    src = (
        _SUITE_SRC.replace("room bath2:   bathroom at 45,18  size 5 x 14\n", "")
        .replace("window bath2 north width 3 offset 1\n", "")
        .replace("room bath1:   bathroom at 40,18  size 5 x 14",
                 "room bath1:   bathroom at 40,18  size 10 x 14")
        .format(baths="door hall - bath1 width 2.67")
    )
    assert "MASTER_ENSUITE" not in {d.code for d in compile_source(src).infos}


def test_closet_and_ensuite_codes_registered():
    assert "MASTER_ENSUITE" in REGISTRY
    assert "NO_CLOSET" in REGISTRY


# --- DOOR_SIZE: standard manufactured door widths ---------------------------

_DOORSIZE_SRC = """\
plan "Door sizes"
envelope 36 x 24
ceiling 9
room living: living  at 0,0  size 20 x 24
room bed:    bedroom at 20,0 size 16 x 24
door living - bed width {w}
entry living south width {ew} offset 4
window living west width 10 offset 8
window bed east width 4 offset 6
"""


def _doorsize(w="2.67", ew="3"):
    return {
        d.code for d in compile_source(_DOORSIZE_SRC.format(w=w, ew=ew)).infos
    }


def test_door_size_flags_a_non_standard_swing_width():
    assert "DOOR_SIZE" in _doorsize(w="2.9")  # 34.8 in — between 32 and 36


def test_standard_swing_widths_are_silent():
    for w in ("2.5", "2.67", "3"):  # 30, 32, 36 in
        assert "DOOR_SIZE" not in _doorsize(w=w), w


def test_door_size_tolerates_a_near_standard_width():
    assert "DOOR_SIZE" not in _doorsize(w="2.7")  # 32.4 in, within 0.5 of 32


def test_open_cased_passage_is_not_size_checked():
    src = _DOORSIZE_SRC.format(w="2.67", ew="3").replace(
        "door living - bed width 2.67", "open living - bed width 5"
    )
    assert "DOOR_SIZE" not in {d.code for d in compile_source(src).infos}


def test_door_size_flags_a_non_standard_exterior_width():
    assert "DOOR_SIZE" in _doorsize(ew="3.2")  # 38.4 in entry


def test_garage_overhead_door_is_exempt_from_door_size():
    src = """\
plan "Garage"
envelope 50 x 24
ceiling 9
room living: living at 0,0 size 30 x 24
room garage: garage at 30,0 size 20 x 24
door living - garage width 2.67
entry living south width 3 offset 8
entry garage south width 9 offset 4 no-egress
window living west width 10 offset 8
"""
    sizes = [d for d in compile_source(src).infos if d.code == "DOOR_SIZE"]
    assert not sizes, [d.message for d in sizes]


def test_door_size_is_registered():
    assert "DOOR_SIZE" in REGISTRY


# --- interior door positioning (offset on the shared wall) -------------------

_POS_SRC = """\
plan "Positioned"
envelope 40 x 20
ceiling 9
room living: living  at 0,0  size 24 x 20
room bed:    bedroom at 24,0 size 16 x 20
door living - bed width 2.67 {offset}
entry living south width 3 offset 10
window living west width 10 offset 6
window bed east width 4 offset 6
"""


def test_door_offset_parses_and_round_trips():
    from barndsl import emit_dsl

    r = compile_source(_POS_SRC.format(offset="offset 2"))
    assert r.plan.interior_doors[0].offset == 2.0
    line = next(
        l for l in emit_dsl(r.plan).splitlines() if l.startswith("door")
    )
    assert line.endswith("offset 2")


def test_centred_door_omits_offset_on_emit():
    from barndsl import emit_dsl

    r = compile_source(_POS_SRC.format(offset=""))
    assert r.plan.interior_doors[0].offset is None
    line = next(l for l in emit_dsl(r.plan).splitlines() if l.startswith("door"))
    assert "offset" not in line


def test_door_oob_when_offset_runs_off_the_shared_wall():
    r = compile_source(_POS_SRC.format(offset="offset 19"))  # 19 + 2.67 > 20
    assert "DOOR_OOB" in _codes(r, "error")


def test_positioned_door_within_the_wall_is_clean():
    r = compile_source(_POS_SRC.format(offset="offset 5"))
    assert "DOOR_OOB" not in _codes(r, "error")


def test_two_connections_between_the_same_pair_clash():
    src = _POS_SRC.format(offset="").replace(
        "door living - bed width 2.67",
        "door living - bed width 4\nopen living - bed width 4",
    )
    assert "OPENING_CLASH" in _codes(compile_source(src), "error")


def test_offset_doors_apart_do_not_clash():
    # Two narrow openings on a 20-ft wall, placed well apart.
    src = _POS_SRC.format(offset="").replace(
        "door living - bed width 2.67",
        "door living - bed width 2.67 offset 1\nopen living - bed width 3 offset 14",
    )
    assert "OPENING_CLASH" not in _codes(compile_source(src), "error")


def test_door_oob_registered():
    assert "DOOR_OOB" in REGISTRY


def test_builder_connect_accepts_offset():
    plan = (
        barndominium("B")
        .envelope(width=30, length=20)
        .ceiling(9)
        .add_room("a", "living", x=0, y=0, width=15, length=20)
        .add_room("b", "bedroom", x=15, y=0, width=15, length=20)
        .connect("a", "b", width=2.67, offset=3)
    )
    assert plan.interior_doors[0].offset == 3.0


# --- door swing / hinge ------------------------------------------------------

_SWING_SRC = """\
plan "Swing"
envelope 40 x 20
ceiling 9
room living: living  at 0,0  size 24 x 20
room bed:    bedroom at 24,0 size 16 x 20
door living - bed width 3 {opt}
entry living south width 3 offset 10
window living west width 10 offset 6
window bed east width 4 offset 6
"""


def test_swing_into_and_hinge_parse_and_round_trip():
    from barndsl import emit_dsl

    r = compile_source(_SWING_SRC.format(opt="into bed hinge far"))
    d = r.plan.interior_doors[0]
    assert (d.swing_into, d.hinge) == ("bed", "far")
    line = next(l for l in emit_dsl(r.plan).splitlines() if l.startswith("door"))
    assert line.endswith("into bed hinge far")


def test_swing_into_a_room_it_does_not_connect_is_an_error():
    r = compile_source(_SWING_SRC.format(opt="into kitchen"))
    assert "DOOR_SWING" in _codes(r, "error")


def test_swing_clearance_warns_for_a_shallow_room():
    src = """\
plan "Clearance"
envelope 30 x 20
ceiling 9
room bed:    bedroom at 0,0  size 24 x 20
room closet: closet  at 24,0 size 2 x 20
door bed - closet width 3 into closet
entry bed south width 3 offset 10
window bed west width 10 offset 6
"""
    assert "DOOR_SWING" in _codes(compile_source(src), "warning")


def test_swinging_the_other_way_clears_the_warning():
    src = """\
plan "Clearance ok"
envelope 30 x 20
ceiling 9
room bed:    bedroom at 0,0  size 24 x 20
room closet: closet  at 24,0 size 2 x 20
door bed - closet width 3 into bed
entry bed south width 3 offset 10
window bed west width 10 offset 6
"""
    assert "DOOR_SWING" not in _codes(compile_source(src), "warning")


def test_door_swing_is_registered_and_varies():
    from barndsl.diagnostics import REGISTRY as REG

    assert "DOOR_SWING" in REG and REG["DOOR_SWING"].varies


def test_builder_rejects_a_bad_hinge():
    import pytest

    with pytest.raises(ValueError):
        (
            barndominium("B")
            .envelope(width=20, length=20)
            .ceiling(9)
            .add_room("a", "living", x=0, y=0, width=10, length=20)
            .add_room("b", "bedroom", x=10, y=0, width=10, length=20)
            .connect("a", "b", hinge="sideways")
        )


# --- unified door statement (kinds + exterior form) -------------------------

_UNI_SRC = """\
plan "Unified"
envelope 44 x 20
ceiling 9
room living:  living   at 0,0  size 20 x 20
room kitchen: kitchen  at 20,0 size 12 x 20
room bath:    bathroom at 32,0 size 12 x 20
{conn}
window living west width 10 offset 6
window bath east width 4 offset 6
"""

_UNI_CONN = """\
door living - kitchen cased width 8
door kitchen - bath pocket width 2.67
door living south exterior width 3 offset 8"""


def test_unified_door_kinds_parse():
    r = compile_source(_UNI_SRC.format(conn=_UNI_CONN))
    assert r.ok, r.report()
    kinds = {(d.room_a, d.room_b): d.kind for d in r.plan.interior_doors}
    assert kinds[("living", "kitchen")] == "cased"
    assert kinds[("kitchen", "bath")] == "pocket"
    # cased has no leaf; pocket does
    leaves = {(d.room_a, d.room_b): d.leaf for d in r.plan.interior_doors}
    assert leaves[("living", "kitchen")] is False
    assert leaves[("kitchen", "bath")] is True


def test_unified_door_exterior_form_makes_an_entry():
    r = compile_source(_UNI_SRC.format(conn=_UNI_CONN))
    assert [(e.room, e.wall.value) for e in r.plan.exterior_doors] == [("living", "south")]
    assert "NO_ENTRY" not in _codes(r, "error")


def test_unified_doors_emit_terse_shorthands_and_round_trip():
    from barndsl import emit_dsl

    r = compile_source(_UNI_SRC.format(conn=_UNI_CONN))
    lines = [
        l for l in emit_dsl(r.plan).splitlines()
        if l.startswith(("door", "open", "entry"))
    ]
    assert "open living - kitchen width 8" in lines           # cased -> open
    assert "door kitchen - bath pocket width 2.67" in lines    # pocket keeps `door`
    assert "entry living south width 3 offset 8" in lines      # exterior -> entry
    assert emit_dsl(compile_source(emit_dsl(r.plan)).plan) == emit_dsl(r.plan)


def test_exterior_door_requires_the_exterior_keyword():
    bad = _UNI_SRC.format(
        conn=_UNI_CONN.replace(
            "door living south exterior width 3 offset 8",
            "door living south width 3 offset 8",
        )
    )
    assert not compile_source(bad).ok


def test_legacy_open_and_entry_still_parse():
    legacy = _UNI_SRC.format(
        conn="open living - kitchen width 8\n"
        "door kitchen - bath width 2.67\n"
        "entry living south width 3 offset 8"
    )
    assert compile_source(legacy).ok


def test_sliding_door_has_no_swing_clearance_check():
    # A sliding door into a shallow closet does NOT warn (no swing to clear).
    src = """\
plan "Slide"
envelope 30 x 20
ceiling 9
room bed:    bedroom at 0,0  size 24 x 20
room closet: closet  at 24,0 size 2 x 20
door bed - closet sliding width 3 into closet
entry bed south width 3 offset 10
window bed west width 10 offset 6
"""
    assert "DOOR_SWING" not in _codes(compile_source(src), "warning")


def test_builder_kind_and_bad_kind():
    import pytest

    plan = (
        barndominium("B").envelope(width=24, length=20).ceiling(9)
        .add_room("a", "living", x=0, y=0, width=12, length=20)
        .add_room("b", "bedroom", x=12, y=0, width=12, length=20)
        .connect("a", "b", kind="pocket")
    )
    assert plan.interior_doors[0].kind == "pocket"
    with pytest.raises(ValueError):
        plan.connect("a", "b", kind="revolving")


# --- eval-driven fixes -------------------------------------------------------

_ENSUITE_TIE = """\
plan "Tie"
envelope 50 x 32
ceiling 10
room living:  living   at 0,0   size 50 x 15
room hall:    hallway  at 0,15  size 50 x 3
room bed1:    bedroom  at 0,18   size 14 x 14
room closet1: closet   at 14,18  size 4 x 14
room master:  bedroom  at 18,18  size 14 x 14
room mcloset: closet   at 32,18  size 4 x 14
room mbath:   bathroom at 36,18  size 6 x 14
room bath1:   bathroom at 42,18  size 8 x 14
door living - hall width 4
door hall - bed1 width 3
door hall - master width 3
door hall - bath1 width 2.67
door bed1 - closet1 width 2.5
door master - mcloset width 2.5
{mbath_door}
entry living south width 3 offset 4
window living south width 12 offset 4
window bed1 north width 5 offset 4
window master north width 5 offset 4
window mbath north width 3 offset 1
window bath1 north width 3 offset 2
"""


def test_master_ensuite_not_fired_when_a_tied_bedroom_has_the_ensuite():
    # master and bed1 have equal area; master has a private ensuite (mbath).
    r = compile_source(_ENSUITE_TIE.format(mbath_door="door master - mbath width 2.67"))
    assert "MASTER_ENSUITE" not in _codes(r, "info")


def test_master_ensuite_still_fires_when_no_bedroom_has_one():
    # Both baths open off the hall — no private ensuite anywhere.
    r = compile_source(_ENSUITE_TIE.format(mbath_door="door hall - mbath width 2.67"))
    assert "MASTER_ENSUITE" in _codes(r, "info")


def test_misplaced_align_gives_a_targeted_hint():
    src = """\
plan "Misplaced"
envelope 30 x 20
ceiling 9
room living: living at 0,0 size 16 x 20
room bed: bedroom east-of living size 14 x 20 align far
entry living south width 3 offset 4
"""
    r = compile_source(src)
    extra = next((d for d in r.errors if d.code == "EXTRA_TOKENS"), None)
    assert extra is not None and "align" in extra.hint and "before `size`" in extra.hint


# --- DOOR_BLOCKS_HALL + OVERLAP placement hint (round-2 soft observations) ---

_BLOCKS_SRC = """\
plan "Blocks"
envelope 40 x 22
ceiling 9
room living: living  at 0,0  size 40 x 15
room hall:   hallway at 0,15 size 40 x 3
room bed:    bedroom at 0,18  size 20 x 4
room bed2:   bedroom at 20,18 size 20 x 4
door living - hall width 3 {swing}
door hall - bed width 2.67 into bed
door hall - bed2 width 2.67 into bed2
entry living south width 3 offset 10
window living south width 12 offset 4
window bed north width 4 offset 4
window bed2 north width 4 offset 4
"""


def test_door_into_a_narrow_hall_blocks_circulation():
    r = compile_source(_BLOCKS_SRC.format(swing="into hall"))
    assert "DOOR_BLOCKS_HALL" in _codes(r, "info")


def test_door_swinging_into_the_room_does_not_block_the_hall():
    r = compile_source(_BLOCKS_SRC.format(swing="into living"))
    assert "DOOR_BLOCKS_HALL" not in _codes(r, "info")


def test_unannotated_door_is_not_flagged_for_hall_blocking():
    # Without `into` we don't know the swing direction, so we don't guess.
    r = compile_source(_BLOCKS_SRC.format(swing=""))
    assert "DOOR_BLOCKS_HALL" not in _codes(r, "info")


def test_door_blocks_hall_is_registered():
    assert "DOOR_BLOCKS_HALL" in REGISTRY


def test_overlap_hint_surfaces_a_relative_anchor_collision():
    src = """\
plan "Collide"
envelope 30 x 20
ceiling 9
room living: living  at 0,0  size 14 x 20
room kitchen: kitchen at 14,0 size 16 x 20
room pantry: pantry west-of kitchen size 6 x 20
entry living south width 3 offset 4
"""
    r = compile_source(src)
    hint = next(d.hint for d in r.errors if d.code == "OVERLAP")
    assert "west_of kitchen" in hint and "re-anchor" in hint


# --- BED_SOUND: acoustic buffer between adjacent bedrooms --------------------


def test_adjacent_bedrooms_flag_a_sound_buffer():
    src = """\
plan "Adjacent"
envelope 40 x 22
ceiling 9
room living: living  at 0,0  size 40 x 8
room hall:   hallway at 0,8  size 40 x 3
room bed1:   bedroom at 0,11  size 20 x 11
room bed2:   bedroom at 20,11 size 20 x 11
door living - hall width 4
door hall - bed1 width 3
door hall - bed2 width 3
entry living south width 3 offset 10
window living south width 12 offset 4
window bed1 north width 5 offset 4
window bed2 north width 5 offset 4
"""
    assert "BED_SOUND" in _codes(compile_source(src), "info")


def test_closet_buffered_bedrooms_have_no_sound_flag():
    src = """\
plan "Buffered"
envelope 44 x 22
ceiling 9
room living: living  at 0,0  size 44 x 8
room hall:   hallway at 0,8  size 44 x 3
room bed1:   bedroom at 0,11  size 18 x 11
room c1:     closet  at 18,11 size 4 x 11
room c2:     closet  at 22,11 size 4 x 11
room bed2:   bedroom at 26,11 size 18 x 11
door living - hall width 4
door hall - bed1 width 3
door hall - bed2 width 3
door bed1 - c1 width 2.5
door bed2 - c2 width 2.5
entry living south width 3 offset 10
window living south width 12 offset 4
window bed1 north width 5 offset 4
window bed2 north width 5 offset 4
"""
    assert "BED_SOUND" not in _codes(compile_source(src), "info")


def test_bedrooms_across_a_hall_do_not_flag():
    src = """\
plan "Across"
envelope 24 x 40
ceiling 9
room bed1: bedroom at 0,0  size 24 x 14
room hall: hallway at 0,14 size 24 x 4
room bed2: bedroom at 0,18 size 24 x 14
room living: living at 0,32 size 24 x 8
door bed1 - hall width 3
door hall - bed2 width 3
door hall - living width 4
entry living north width 3 offset 10
window bed1 south width 6 offset 4
window bed2 west width 6 offset 4
window living north width 6 offset 4
"""
    assert "BED_SOUND" not in _codes(compile_source(src), "info")


def test_bed_sound_is_registered():
    assert "BED_SOUND" in REGISTRY


# --- CLOSET_SHAPE: walk-in vs long, skinny closet ---------------------------


def _closet_codes(width, length):
    plan = (
        barndominium("Closet").envelope(width=40, length=30).ceiling(9)
        .add_room("bed", "bedroom", x=0, y=0, width=14, length=14)
        .add_room("c", "closet", x=14, y=0, width=width, length=length)
        .connect("bed", "c", width=2.5)
        .add_room("living", "living", x=0, y=14, width=40, length=16)
        .connect("bed", "living", width=2.67)
        .entrance("living", "south", width=3, offset=10)
        .add_window("bed", "west", width=4, offset=4)
        .add_window("living", "south", width=12, offset=4)
    )
    return {i.code for i in validate(plan).infos}


def test_long_skinny_closet_suggests_a_walkin():
    assert "CLOSET_SHAPE" in _closet_codes(2.5, 12)   # 30 sq ft, 4.8:1


def test_square_walkin_closet_is_fine():
    assert "CLOSET_SHAPE" not in _closet_codes(6, 6)   # a walk-in


def test_small_reach_in_closet_is_exempt():
    assert "CLOSET_SHAPE" not in _closet_codes(2, 8)   # 16 sq ft — a normal reach-in


def test_wide_shallow_closet_is_exempt():
    assert "CLOSET_SHAPE" not in _closet_codes(10, 3)  # 3.3:1 — a wide reach-in


def test_walkable_long_closet_is_exempt():
    assert "CLOSET_SHAPE" not in _closet_codes(4, 12)  # 4 ft deep — a long walk-in


def test_closet_shape_is_registered():
    from barndsl.diagnostics import REGISTRY as REG

    assert "CLOSET_SHAPE" in REG


# --- exterior doors must actually render (regression) ------------------------


def test_exterior_door_is_drawn_in_the_svg():
    from barndsl import render_svg

    # One room + one entry, no interior swing doors: any swing arc in the SVG
    # must be the exterior door. Guards a regression where the exterior-door
    # draw loop was orphaned after a method return and silently stopped running.
    src = """\
plan "Ext"
envelope 20 x 16
ceiling 9
room living: living at 0,0 size 20 x 16
entry living south width 3 offset 8
window living west width 8 offset 4
"""
    svg = render_svg(compile_source(src).plan)
    assert " A " in svg  # _door_symbol draws a swing arc; exterior doors use it


# ===========================================================================
# Second review round — rules distilled from the HTML design-review feedback
# ===========================================================================

# --- HALL_TIGHT: 3 ft is legal but 4 ft is comfortable -----------------------

_HALLW = """\
plan "HallW"
envelope 40 x 30
ceiling 9
room living: living  at 0,0 size 40 x 15
room hall:   hallway at 0,15 size 40 x {w}
room bed1:   bedroom at 0,{y}  size 16 x 12
room bed2:   bedroom at 16,{y} size 16 x 12
open living - hall width 4
door hall - bed1 width 3 offset 0.5
door hall - bed2 width 3 offset 0.5
entry living south width 3 offset 4
entry living west width 3 offset 4
window living south width 12 offset 4
window bed1 north width 5 offset 5
window bed2 north width 5 offset 5
"""


def test_hall_tight_flags_a_three_foot_hall():
    r = compile_source(_HALLW.format(w=3, y=18))
    assert "HALL_TIGHT" in _codes(r, "info")
    assert "HALL_WIDTH" not in _codes(r, "error")  # 3 ft is still legal


def test_hall_tight_silent_at_four_feet():
    r = compile_source(_HALLW.format(w=4, y=19))
    assert "HALL_TIGHT" not in _codes(r, "info")


# --- HALL_DEADEND: a stub running past the last doorway ----------------------

_STUB = """\
plan "Stub"
envelope {ew} x 30
ceiling 9
room hall:   hallway at 0,0 size {hw} x 4
room living: living  at 0,4 size 15 x 26
room bed:    bedroom at 15,4 size 13 x 26
door hall - living width 4 offset 1
door hall - bed width 3 offset 9.5
entry hall south width 3 offset 4
window living west width 10 offset 8
window bed north width 5 offset 5
"""


def test_hall_deadend_flags_a_stub_past_the_last_door():
    # Hall runs to x=40 but the last room ends at x=28 — a 12 ft dead-end stub.
    r = compile_source(_STUB.format(ew=40, hw=40))
    deadends = [d for d in r.infos if d.code == "HALL_DEADEND"]
    assert deadends and "past its last doorway" in deadends[0].message


def test_hall_deadend_silent_when_trimmed_to_the_last_door():
    r = compile_source(_STUB.format(ew=28, hw=28))
    assert "HALL_DEADEND" not in _codes(r, "info")


# --- NO_BACK_DOOR: a home wants a front and a back door ----------------------

_BACK = """\
plan "Back"
envelope 30 x 24
ceiling 9
room living:  living  at 0,0  size 18 x 24
room kitchen: kitchen at 18,0 size 12 x 24
open living - kitchen width 8
entry living south width 3 offset 4
{extra}
window living west width 10 offset 6
window kitchen east width 6 offset 8
"""


def test_no_back_door_flags_a_single_entrance():
    assert "NO_BACK_DOOR" in _codes(compile_source(_BACK.format(extra="")), "info")


def test_no_back_door_silent_with_two_entrances():
    r = compile_source(_BACK.format(extra="entry kitchen east width 3 offset 4"))
    assert "NO_BACK_DOOR" not in _codes(r, "info")


def test_garage_door_is_not_a_back_door():
    # A vehicle door on the garage doesn't give the *house* a second way out.
    src = """\
plan "Garage"
envelope 40 x 24
ceiling 9
room living: living at 0,0 size 22 x 24
room garage: garage at 22,0 size 18 x 24
door living - garage width 2.67
entry living south width 3 offset 4
entry garage south width 9 offset 4 no-egress
window living west width 10 offset 6
"""
    assert "NO_BACK_DOOR" in _codes(compile_source(src), "info")


# --- BATH_OVERSIZE: an ensuite bigger than its bedroom -----------------------

_ENSUITE = """\
plan "Suite"
envelope 30 x 30
ceiling 10
room living:  living   at 0,0  size 30 x 12
room hall:    hallway  at 0,12 size 30 x 4
room master:  bedroom  at 0,16 size 10 x 14
room mbath:   bathroom at 10,16 size {bx} x 14
open living - hall width 4
door hall - master width 3 offset 1
door master - mbath width 2.67 offset 1
{shared}
entry living south width 3 offset 4
entry living west width 3 offset 4
window living south width 12 offset 4
window master north width 5 offset 2
"""


def test_bath_oversize_flags_an_ensuite_bigger_than_its_bedroom():
    # master 140 sq ft, ensuite 224 sq ft.
    r = compile_source(_ENSUITE.format(bx=16, shared=""))
    over = {d.room for d in r.infos if d.code == "BATH_OVERSIZE"}
    assert "mbath" in over


def test_bath_oversize_silent_for_a_smaller_ensuite():
    r = compile_source(_ENSUITE.format(bx=6, shared=""))
    assert "BATH_OVERSIZE" not in _codes(r, "info")


def test_bath_oversize_ignores_a_shared_bath():
    # The big bath also opens to the hall, so it isn't a private ensuite.
    r = compile_source(_ENSUITE.format(bx=16, shared="door hall - mbath width 2.67 offset 1"))
    assert "BATH_OVERSIZE" not in _codes(r, "info")


# --- STAIR_BLOCKS_DOOR / STAIR_WALL ------------------------------------------

_LVL = """\
plan "Lvl"
envelope 24 x 20
ceiling 8
room living: living   at 0,0  size 24 x 14
room bath:   bathroom at 16,14 size 8 x 6
room loft:   loft     at 0,0  size 24 x 14 level 1
stair flight at {sx},{sy} size 4 x 11 from 0 to 1
door living - bath width 2.67 offset 2
entry living south width 3 offset 4
window living south width 10 offset 6
window bath east width 3 offset 1
"""


def test_stair_blocks_door_when_it_intrudes_on_a_doorway():
    # Stair x17..21 sits in front of the living-bath door (x18..20.67).
    r = compile_source(_LVL.format(sx=17, sy=2))
    assert "STAIR_BLOCKS_DOOR" in _codes(r, "warning")


def test_stair_blocks_door_silent_when_clear():
    # Stair along the west wall, far from the east-side door.
    r = compile_source(_LVL.format(sx=0, sy=2))
    assert "STAIR_BLOCKS_DOOR" not in _codes(r, "warning")


def test_stair_wall_flags_a_free_floating_flight():
    r = compile_source(_LVL.format(sx=8, sy=2))  # marooned mid-living
    assert "STAIR_WALL" in _codes(r, "info")


def test_stair_wall_silent_against_a_wall():
    r = compile_source(_LVL.format(sx=0, sy=2))  # flush to the west envelope wall
    assert "STAIR_WALL" not in _codes(r, "info")


# --- DOOR_CENTERED: back a swing door to a corner ----------------------------

_DC = """\
plan "DC"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 30 x 12
room hall:   hallway at 0,12 size 30 x 4
room bed:    bedroom at 0,16 size 16 x 8
{conn}
open living - hall width 4
entry living south width 3 offset 4
entry living west width 3 offset 4
window living south width 12 offset 4
window bed north width 5 offset 5
"""


def test_door_centered_flags_a_mid_wall_swing():
    r = compile_source(_DC.format(conn="door hall - bed width 3"))
    centred = {(d.room) for d in r.infos if d.code == "DOOR_CENTERED"}
    assert centred  # the hall-bed door floats 6.5 ft from each corner


def test_door_centered_cleared_by_an_offset():
    r = compile_source(_DC.format(conn="door hall - bed width 3 offset 0.5"))
    assert "DOOR_CENTERED" not in _codes(r, "info")


def test_door_centered_exempts_a_cased_opening():
    r = compile_source(_DC.format(conn="open hall - bed width 3"))
    assert "DOOR_CENTERED" not in _codes(r, "info")


# --- WINDOW_PARTITION: a window butting an interior wall ---------------------

_WP = """\
plan "WP"
envelope 30 x 20
ceiling 9
room living:  living  at 0,0  size 15 x 20
room kitchen: kitchen at 15,0 size 15 x 20
open living - kitchen width 8
entry living south width 3 offset 4
entry kitchen south width 3 offset 4
window {win}
window kitchen north width 5 offset 5
"""


def test_window_partition_flags_a_window_against_a_partition():
    # Window far edge lands exactly on the living-kitchen junction at x=15.
    r = compile_source(_WP.format(win="living south width 5 offset 10"))
    assert "WINDOW_PARTITION" in _codes(r, "info")


def test_window_partition_silent_when_centred():
    r = compile_source(_WP.format(win="living south width 5 offset 5"))
    assert "WINDOW_PARTITION" not in _codes(r, "info")


def test_window_partition_exempts_a_true_building_corner():
    # Flush to x=0, but that's the building's SW corner, not a partition.
    r = compile_source(_WP.format(win="living south width 5 offset 0"))
    assert "WINDOW_PARTITION" not in _codes(r, "info")


# --- ROOM_TIGHT short-side floor for a full bath -----------------------------


def test_room_tight_flags_a_narrow_full_bath():
    # 4 x 13 = 52 sq ft (over the 48 area floor) but only 4 ft across.
    src = """\
plan "Narrow"
envelope 30 x 20
ceiling 9
room living: living   at 0,0  size 26 x 20
room bath:   bathroom at 26,0 size 4 x 13
door living - bath width 2.67
entry living south width 3 offset 4
entry living west width 3 offset 4
window living south width 12 offset 4
window bath east width 2 offset 5
"""
    tight = [d for d in compile_source(src).infos if d.code == "ROOM_TIGHT" and d.room == "bath"]
    assert tight and "short side" in tight[0].message


# --- all the new codes are catalogued ---------------------------------------


def test_second_round_codes_are_registered():
    for code in (
        "HALL_TIGHT", "NO_BACK_DOOR", "BATH_OVERSIZE", "STAIR_BLOCKS_DOOR",
        "STAIR_WALL", "DOOR_CENTERED", "WINDOW_PARTITION",
    ):
        assert code in REGISTRY
        assert explain(code) and "Unknown" not in explain(code)


# ===========================================================================
# Third review round — swing clash + build module (from the HTML feedback)
# ===========================================================================

# --- DOOR_SWING_CLASH --------------------------------------------------------

_CLASH = """\
plan "Clash"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 30 x 12
room bed:    bedroom at 0,12 size 18 x 12
room closet: closet  at 18,12 size 6 x 12
{doors}
entry living south width 3 offset 4
entry living west width 3 offset 4
window living south width 12 offset 4
window bed north width 5 offset 6
"""


def test_door_swing_clash_flags_overlapping_leaves():
    # Both leaves hinge at the shared SE corner of the bedroom and sweep into it.
    r = compile_source(_CLASH.format(
        doors="door living - bed width 3 into bed offset 15 hinge far\n"
              "door bed - closet width 2.5 into bed offset 0.5 hinge near"))
    assert "DOOR_SWING_CLASH" in _codes(r, "info")


def test_door_swing_clash_silent_when_doors_are_apart():
    r = compile_source(_CLASH.format(
        doors="door living - bed width 3 offset 0.5\n"
              "door bed - closet width 2.5 into bed offset 0.5 hinge near"))
    assert "DOOR_SWING_CLASH" not in _codes(r, "info")


def test_door_swing_clash_sees_a_french_pair():
    # A double/french pair swings two leaves; converting the clashing door to
    # french must NOT silence the check (review defect: double-leaf doors were
    # invisible to DOOR_SWING_CLASH while the renderer drew swinging leaves).
    r = compile_source(_CLASH.format(
        doors="door living - bed french width 6 into bed offset 12 hinge far\n"
              "door bed - closet width 2.5 into bed offset 0.5 hinge near"))
    assert "DOOR_SWING_CLASH" in _codes(r, "info")


def test_door_swing_clash_avoided_by_a_pocket_door():
    # A pocket leaf has no swing arc, so it can't clash.
    r = compile_source(_CLASH.format(
        doors="door living - bed width 3 into bed offset 15 hinge far\n"
              "door bed - closet pocket 2.5 offset 0.5"))
    assert "DOOR_SWING_CLASH" not in _codes(r, "info")


# --- ENVELOPE_MODULE ---------------------------------------------------------

_MOD = """\
plan "Mod"
envelope {ew} x {el}
ceiling 9
room living: living at 0,0 size {ew} x {el}
entry living south width 3 offset 3
window living west width 6 offset 4
"""


def test_envelope_module_flags_off_module_dimensions():
    r = compile_source(_MOD.format(ew=31, el=24))  # 31 isn't a multiple of 3
    mod = [d for d in r.infos if d.code == "ENVELOPE_MODULE"]
    assert mod and "31" in mod[0].message


def test_envelope_module_silent_on_module():
    r = compile_source(_MOD.format(ew=30, el=24))  # both multiples of 3
    assert "ENVELOPE_MODULE" not in _codes(r, "info")


def test_envelope_module_flags_a_wing():
    src = """\
plan "Wing"
envelope 30 x 24
wing 9 x 10 at 30,0
ceiling 9
room living: living at 0,0 size 30 x 24
room ext:    office at 30,0 size 9 x 10
door living - ext width 2.67
entry living south width 3 offset 3
window living west width 6 offset 4
window ext east width 3 offset 3
"""
    mod = [d for d in compile_source(src).infos if d.code == "ENVELOPE_MODULE"]
    assert mod and "wing 1 length 10" in mod[0].message  # 10 isn't a multiple of 3


def test_round_three_codes_are_registered():
    for code in ("DOOR_SWING_CLASH", "ENVELOPE_MODULE"):
        assert code in REGISTRY
        assert "Unknown" not in explain(code)
