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
    for fname in ("validation.py", "compiler.py", "agent.py"):
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


def test_room_tight_exempts_half_bath():
    r = compile_source(_TIGHT_SRC.format(kw=6))
    tight = {d.room for d in r.infos if d.code == "ROOM_TIGHT"}
    assert "powder" not in tight  # a powder room is fine small


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
    r = _hall("door living - hall width 3\ndoor hall - bed width 2.67")
    assert "HALL_DEADEND" not in _codes(r, "info")


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
