"""Placed-fixture code checks — the clearance / landing / triangle / vent / stair
rules over the resolved layout (seeds + authored pieces).

Each rule gets a firing case and a near-miss that must stay clean, plus a guard
that the auto-seeds never trip the authored-only rules. The placement geometry
lives in :mod:`barndsl.fixtures`; this file pins the *diagnostics*.
"""

import re
from pathlib import Path

from barndsl.compiler import compile_source
from barndsl.diagnostics import REGISTRY, explain

_NEW_CODES = (
    "FIXTURE_TOILET_CLEARANCE",
    "FIXTURE_FRONT",
    "FIXTURE_ROOM_TYPE",
    "FIXTURE_BACKING",
    "FIXTURE_EGRESS",
    "RANGE_WINDOW",
    "RANGE_LANDING",
    "KITCHEN_TRIANGLE",
    "DRYER_VENT",
    "FIXTURE_STAIR",
)


def _codes(src):
    r = compile_source(src)
    assert r.plan is not None, [d.message for d in r.diagnostics if d.severity.value == "error"]
    return {i.code for i in r.diagnostics}


# --- test scaffolds ----------------------------------------------------------


def _bath(*extra):
    """A 14x12 bath with the lavatory/tub parked far from the toilet corner, so
    the toilet's clearance is judged against known geometry, not stray seeds."""
    base = (
        'plan "T"\nenvelope 42 x 30\nceiling 9\n'
        "room bath: bathroom at 0,0 size 14 x 12\n"
        "room rest: living at 14,0 size 28 x 30\n"
        "entry rest south width 3 offset 2\n"
        "fixture lavatory in bath at 11,10 wall N\n"
        "fixture tub in bath at 9,4 wall E\n"
    )
    return base + "".join(e + "\n" for e in extra)


def _kit(*extra):
    base = (
        'plan "T"\nenvelope 40 x 30\nceiling 9\n'
        "room kit: kitchen at 0,0 size 20 x 14\n"
        "room rest: living at 20,0 size 20 x 30\n"
        "entry rest south width 3 offset 2\n"
    )
    return base + "".join(e + "\n" for e in extra)


def _bed(*extra):
    base = (
        'plan "T"\nenvelope 40 x 30\nceiling 9\n'
        "room bed: bedroom at 0,16 size 14 x 14\n"
        "room rest: living at 0,0 size 40 x 16\n"
        "room k: kitchen at 14,16 size 26 x 14\n"
        "entry rest south width 3 offset 2\n"
        "window bed north width 4 offset 5\n"
    )
    return base + "".join(e + "\n" for e in extra)


# --- FIXTURE_TOILET_CLEARANCE (IRC R307.1) -----------------------------------


def test_toilet_side_clearance_warns_under_15_in():
    # dresser 9 in from the toilet centreline (< 15 in each side).
    codes = _codes(_bath("fixture toilet in bath at 2,0", "fixture dresser in bath at 4.0,0"))
    assert "FIXTURE_TOILET_CLEARANCE" in codes


def test_toilet_side_clearance_clean_at_exactly_15_in():
    # dresser edge exactly 15 in from centre — the code minimum, so silent.
    codes = _codes(_bath("fixture toilet in bath at 2,0", "fixture dresser in bath at 4.5,0"))
    assert "FIXTURE_TOILET_CLEARANCE" not in codes


def test_toilet_front_clearance_warns_under_21_in():
    # a dresser 12 in in front of the toilet face (< 21 in clear floor).
    codes = _codes(_bath("fixture toilet in bath at 0,0", "fixture dresser in bath at 0,3.33"))
    assert "FIXTURE_TOILET_CLEARANCE" in codes


def test_toilet_front_clearance_clean_with_room_ahead():
    codes = _codes(_bath("fixture toilet in bath at 0,0", "fixture dresser in bath at 0,4.5"))
    assert "FIXTURE_TOILET_CLEARANCE" not in codes


def test_toilet_clearance_message_cites_irc():
    r = compile_source(_bath("fixture toilet in bath at 2,0", "fixture dresser in bath at 4.0,0"))
    msg = next(i.message for i in r.diagnostics if i.code == "FIXTURE_TOILET_CLEARANCE")
    assert "R307.1" in msg


# --- FIXTURE_FRONT -----------------------------------------------------------


def test_front_strip_blocked_by_fixture_infos():
    # a desk (3 ft front) with a dresser parked 2 ft ahead of its face.
    codes = _codes(_kit("fixture desk in rest at 2,0 wall S", "fixture dresser in rest at 2,2 wall S"))
    assert "FIXTURE_FRONT" in codes


def test_front_strip_clear_is_silent():
    codes = _codes(_kit("fixture desk in rest at 2,0 wall S"))
    assert "FIXTURE_FRONT" not in codes


def test_free_standing_needs_a_walkway():
    src = (
        'plan "T"\nenvelope 30 x 20\nceiling 9\n'
        "room din: dining at 0,0 size 9 x 7\n"
        "room rest: living at 9,0 size 21 x 20\n"
        "entry rest south width 3 offset 2\n"
        "fixture dining_table in din\n"  # 6x3.33 table in a 7 ft-deep room: no long-side aisle
    )
    assert "FIXTURE_FRONT" in _codes(src)


def test_free_standing_with_walkway_is_silent():
    src = (
        'plan "T"\nenvelope 30 x 20\nceiling 9\n'
        "room din: dining at 0,0 size 9 x 7\n"
        "room rest: living at 9,0 size 21 x 20\n"
        "entry rest south width 3 offset 2\n"
        "fixture dining_table in rest\n"  # centred in a big room
    )
    assert "FIXTURE_FRONT" not in _codes(src)


# --- FIXTURE_ROOM_TYPE -------------------------------------------------------


def test_tub_in_a_living_room_is_flagged():
    assert "FIXTURE_ROOM_TYPE" in _codes(_kit("fixture tub in rest at 1,1"))


def test_tub_in_a_bathroom_is_fine():
    assert "FIXTURE_ROOM_TYPE" not in _codes(_bath("fixture toilet in bath at 0,0"))


def test_room_type_never_cries_wolf_for_utility_appliances():
    # a water heater in a utility, a desk in a bedroom — both perfectly normal.
    src = (
        'plan "T"\nenvelope 40 x 30\nceiling 9\n'
        "room util: utility at 0,0 size 10 x 10\n"
        "room bed: bedroom at 10,0 size 14 x 14\n"
        "room rest: living at 24,0 size 16 x 30\n"
        "entry rest south width 3 offset 2\n"
        "window bed south width 4 offset 5\n"
        "fixture water_heater in util at 0,0 wall S\n"
        "fixture desk in bed at 0,0 wall S\n"
    )
    assert "FIXTURE_ROOM_TYPE" not in _codes(src)


# --- FIXTURE_BACKING ---------------------------------------------------------


def test_wall_backed_piece_floating_is_flagged():
    assert "FIXTURE_BACKING" in _codes(_kit("fixture dresser in rest at 6,6"))


def test_wall_backed_piece_against_a_wall_is_fine():
    assert "FIXTURE_BACKING" not in _codes(_kit("fixture dresser in rest at 6,0 wall S"))


# --- FIXTURE_EGRESS ----------------------------------------------------------


def test_wardrobe_over_egress_window_warns():
    assert "FIXTURE_EGRESS" in _codes(_bed("fixture wardrobe in bed at 5,12 wall N"))


def test_wardrobe_clear_of_egress_window_is_silent():
    assert "FIXTURE_EGRESS" not in _codes(_bed("fixture wardrobe in bed at 0,12 wall N"))


def test_short_furniture_never_triggers_egress():
    # a dresser is not a "tall" kind — it doesn't block the escape opening.
    assert "FIXTURE_EGRESS" not in _codes(_bed("fixture dresser in bed at 5,12 wall N"))


# --- RANGE_WINDOW ------------------------------------------------------------


def test_range_under_operable_window_warns():
    codes = _codes(_kit(
        "window kit south width 4 offset 6",
        "fixture refrigerator in kit at 0,0 wall S",
        "fixture sink in kit at 3,0 wall S",
        "fixture range in kit at 6,0 wall S",
    ))
    assert "RANGE_WINDOW" in codes


def test_range_under_fixed_glass_is_fine():
    codes = _codes(_kit(
        "window kit south fixed width 4 offset 6",
        "fixture refrigerator in kit at 0,0 wall S",
        "fixture sink in kit at 3,0 wall S",
        "fixture range in kit at 6,0 wall S",
    ))
    assert "RANGE_WINDOW" not in codes


def test_range_clear_of_window_is_fine():
    codes = _codes(_kit(
        "window kit south width 4 offset 6",
        "fixture range in kit at 0,0 wall S",
        "fixture refrigerator in kit at 2.5,0 wall S",
        "fixture sink in kit at 5,0 wall S",
    ))
    assert "RANGE_WINDOW" not in codes


# --- RANGE_LANDING -----------------------------------------------------------


def test_isolated_range_wants_a_landing():
    codes = _codes(_kit(
        "fixture refrigerator in kit at 0,0 wall S",
        "fixture range in kit at 6,0 wall S",
        "fixture sink in kit at 12,0 wall S",
    ))
    assert "RANGE_LANDING" in codes


def test_range_beside_casework_is_fine():
    codes = _codes(_kit(
        "fixture refrigerator in kit at 0,0 wall S",
        "fixture range in kit at 3,0 wall S",  # fridge's right edge touches the range
        "fixture sink in kit at 8,0 wall S",
    ))
    assert "RANGE_LANDING" not in codes


# --- KITCHEN_TRIANGLE --------------------------------------------------------


def test_scattered_work_triangle_infos():
    codes = _codes(_kit(
        "fixture refrigerator in kit at 0,0 wall S",
        "fixture range in kit at 15,0 wall S",
        "fixture sink in kit at 0,11 wall N",
    ))
    assert "KITCHEN_TRIANGLE" in codes


def test_compact_work_triangle_is_silent():
    codes = _codes(_kit(
        "fixture refrigerator in kit at 0,0 wall S",
        "fixture range in kit at 3,0 wall S",
        "fixture sink in kit at 6,0 wall S",
    ))
    assert "KITCHEN_TRIANGLE" not in codes


# --- DRYER_VENT --------------------------------------------------------------


def _dry(*extra):
    base = (
        'plan "T"\nenvelope 50 x 40\nceiling 9\n'
        "room big: living at 0,0 size 50 x 40\n"
        "entry big south width 3 offset 2\n"
        "window big north width 4 offset 5\n"
    )
    return base + "".join(e + "\n" for e in extra)


def test_dryer_far_from_exterior_wall_infos():
    assert "DRYER_VENT" in _codes(_dry("fixture dryer in big at 20,20 wall S"))


def test_dryer_near_exterior_wall_is_silent():
    assert "DRYER_VENT" not in _codes(_dry("fixture dryer in big at 20,0 wall S"))


# --- FIXTURE_STAIR -----------------------------------------------------------


def test_fixture_on_a_stair_footprint_warns():
    src = (
        'plan "T"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 24 x 18\n"
        "room rest: living at 24,0 size 16 x 30\n"
        "room loft: loft at 0,0 size 24 x 18 level 1\n"
        "entry rest south width 3 offset 2\n"
        "stair flight at 2,2 size 4 x 12 from 0 to 1\n"
        "fixture sofa in living at 2,2\n"  # dropped onto the flight
    )
    assert "FIXTURE_STAIR" in _codes(src)


def test_fixture_clear_of_the_stair_is_silent():
    src = (
        'plan "T"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 24 x 18\n"
        "room rest: living at 24,0 size 16 x 30\n"
        "room loft: loft at 0,0 size 24 x 18 level 1\n"
        "entry rest south width 3 offset 2\n"
        "stair flight at 0,2 size 4 x 12 from 0 to 1\n"
        "fixture sofa in living at 14,0 wall S\n"  # well clear of the flight
    )
    assert "FIXTURE_STAIR" not in _codes(src)


# --- seeds never trip the authored-only rules --------------------------------


_AUTHORED_ONLY = (
    "FIXTURE_TOILET_CLEARANCE",
    "FIXTURE_FRONT",
    "FIXTURE_ROOM_TYPE",
    "FIXTURE_BACKING",
    "FIXTURE_EGRESS",
)

_SEEDED_PLAN = (
    'plan "T"\nenvelope 40 x 24\nceiling 9\n'
    "room kit: kitchen at 0,0 size 16 x 12\n"
    "room bath: bathroom at 16,0 size 10 x 12\n"
    "room laundry: laundry at 26,0 size 14 x 12\n"
    "room rest: living at 0,12 size 40 x 12\n"
    "entry rest north width 3 offset 2\n"
    "window kit south width 6 offset {off}\n"
)


def test_seeds_never_trip_the_authored_only_rules():
    # A window placed where the seeded appliance run lands is a real conflict, so
    # the seed-judged RANGE_WINDOW may fire; but the *authored-only* rules must not
    # nag about the auto-placer's own choices.
    codes = _codes(_SEEDED_PLAN.format(off=5))
    assert not (set(_AUTHORED_ONLY) & codes), sorted(set(_AUTHORED_ONLY) & codes)


def test_well_formed_seeded_plan_is_completely_clean():
    # Window clear of the appliance run (like the curated gallery): none of the ten
    # placed-fixture checks fires on a plan carrying only auto-seeds.
    codes = _codes(_SEEDED_PLAN.format(off=9))
    assert not (set(_NEW_CODES) & codes), sorted(set(_NEW_CODES) & codes)


# --- rule 11: unknown fixture kind stays a clean, catalog-listing error -------


def test_unknown_fixture_kind_lists_the_catalog():
    r = compile_source(
        'plan "T"\nenvelope 20 x 16\nceiling 9\n'
        "room bed: bedroom at 0,0 size 14 x 12\n"
        "window bed south width 4 offset 5\n"
        "fixture flux_capacitor in bed\n"
    )
    err = next(d for d in r.diagnostics if d.severity.value == "error" and "flux_capacitor" in d.message)
    # the hint already names the real catalog kinds, like BAD_TYPE does for room types.
    assert err.hint and "toilet" in err.hint and "range" in err.hint


# --- registry / explain ------------------------------------------------------


def test_new_codes_are_registered_and_explained():
    for code in _NEW_CODES:
        assert code in REGISTRY, code
        text = explain(code)
        assert text.startswith(code) and len(text) > len(code) + 20


def test_registry_covers_every_code_emitted_in_fixtures():
    text = (Path(__file__).resolve().parent.parent / "src" / "barndsl" / "fixtures.py").read_text()
    emitted = set(re.findall(r'Severity\.\w+,\s*"([A-Z_]{3,})"', text))
    missing = sorted(emitted - set(REGISTRY))
    assert not missing, missing
