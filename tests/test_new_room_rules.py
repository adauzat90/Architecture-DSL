"""Design-quality checks for the newer semantic room types."""

from __future__ import annotations

from barndsl.compiler import compile_source


def _codes(src: str) -> set[str]:
    return {d.code for d in compile_source(src).diagnostics}


def test_safe_room_rules_fire_for_vulnerable_shelter():
    src = """
plan "Safe"
envelope 24 x 12
room living: living at 0,0 size 12 x 12
room storm: safe_room at 12,0 size 2 x 4
entry living south width 3 offset 2
open living - storm width 3
window storm south width 1 offset 0.5
"""
    codes = _codes(src)
    assert {"SAFE_ROOM_WINDOW", "SAFE_ROOM_EXTERIOR", "SAFE_ROOM_SIZE", "SAFE_ROOM_ACCESS"} <= codes


def test_safe_room_size_flags_long_skinny_shelter():
    src = """
plan "Safe Skinny"
envelope 24 x 14
room living: living at 0,0 size 12 x 14
room storm: safe_room at 12,0 size 4 x 14
entry living south width 3 offset 2
door living - storm width 3
"""
    codes = _codes(src)
    assert "SAFE_ROOM_SIZE" in codes
    assert "SAFE_ROOM_EXTERIOR" in codes


def test_general_skinny_room_warning_fires_for_non_circulation_rooms():
    src = """
plan "Skinny Utility"
envelope 20 x 18
room living: living at 0,0 size 10 x 18
room util: utility at 10,0 size 4 x 18
entry living south width 3 offset 2
door living - util width 3
window living south width 4 offset 3
"""
    assert "ROOM_SKINNY" in _codes(src)


def test_mechanical_room_rules_fire_for_service_problems():
    src = """
plan "Mechanical"
envelope 24 x 10
room living: living at 0,0 size 10 x 10
room mech: mechanical at 10,0 size 4 x 10
room bed: bedroom at 14,0 size 10 x 10
entry living south width 3 offset 2
open living - mech width 3
door mech - bed width 3
window bed south width 4 offset 3
"""
    codes = _codes(src)
    assert {"MECH_CLEARANCE", "MECH_ACCESS", "MECH_BEDROOM"} <= codes


def test_foyer_storage_great_flex_and_rec_rules_fire():
    src = """
plan "New room semantics"
envelope 80 x 20
room foyer: foyer at 0,0 size 3 x 12
room bed: bedroom at 3,0 size 10 x 10
room store: storage at 13,0 size 2 x 12
room great: great_room at 15,0 size 10 x 10
room flex: flex at 25,0 size 10 x 10
room rec: rec_room at 35,0 size 10 x 10
room bed2: bedroom at 45,0 size 10 x 10
entry bed south width 3 offset 2
door foyer - bed width 3
door bed - store width 3
door store - great width 3
door great - flex width 3
door flex - rec width 3
door rec - bed2 width 3
window bed south width 4 offset 3
window bed2 south width 4 offset 3
"""
    codes = _codes(src)
    assert "FOYER_FLOW" in codes
    assert "FOYER_SHAPE" in codes
    assert {"STORAGE_SHAPE", "STORAGE_ACCESS"} <= codes
    assert {"GREAT_ROOM_SCALE", "GREAT_ROOM_FLOW"} <= codes
    assert "FLEX_FUTURE_BED" in codes
    assert {"REC_ROOM_SCALE", "REC_ROOM_NOISE"} <= codes


def test_foyer_shape_fires_for_agent_foyer_typed_as_hallway():
    src = """
plan "Entry Spine"
envelope 40 x 12
room foyer: hallway at 0,0 size 40 x 4
room living: living at 0,4 size 40 x 8
entry foyer west width 3 offset 0.5
door foyer - living width 3
window living south width 8 offset 8
"""
    assert "FOYER_SHAPE" in _codes(src)


def test_garage_vehicle_door_fires_for_trapped_shop():
    src = """
plan "Trapped Shop"
envelope 30 x 20
room shop: shop at 0,0 size 12 x 20
room mud: mudroom at 12,0 size 6 x 20
room living: living at 18,0 size 12 x 20
entry living south width 3 offset 4
door shop - mud width 3
door mud - living width 3
window living south width 4 offset 4
"""
    assert "GARAGE_VEHICLE_DOOR" in _codes(src)


def test_satisfied_new_room_rules_stay_quiet_on_basic_good_cases():
    src = """
plan "Quiet"
envelope 70 x 30
room foyer: foyer at 0,0 size 6 x 6
room great: great_room at 6,0 size 20 x 12 vaulted
room kitchen: kitchen at 26,0 size 12 x 12
room storm: safe_room at 6,12 size 6 x 6
room mech: mechanical at 12,12 size 6 x 6
room store: storage at 18,12 size 6 x 6
room flex: flex at 38,0 size 10 x 12
room flex_closet: closet at 48,0 size 4 x 12
room rec: rec_room at 52,0 size 12 x 12
room shop: shop at 0,18 size 12 x 12
entry foyer south width 3 offset 1
door shop north overhead width 9 height 8 offset 1
door foyer - great width 3
door storm - great width 3
door mech - great width 3
door store - great width 3
open great - kitchen width 6
door kitchen - flex width 3
door flex - flex_closet width 3
door flex_closet - rec width 3
window great south width 6 offset 6
window flex south width 4 offset 3
window rec south width 4 offset 3
"""
    codes = _codes(src)
    assert not {
        "SAFE_ROOM_WINDOW",
        "SAFE_ROOM_EXTERIOR",
        "SAFE_ROOM_SIZE",
        "SAFE_ROOM_ACCESS",
        "MECH_CLEARANCE",
        "MECH_ACCESS",
        "MECH_BEDROOM",
        "FOYER_FLOW",
        "STORAGE_SHAPE",
        "STORAGE_ACCESS",
        "GREAT_ROOM_SCALE",
        "GREAT_ROOM_FLOW",
        "FLEX_FUTURE_BED",
        "REC_ROOM_SCALE",
        "REC_ROOM_NOISE",
        "GARAGE_VEHICLE_DOOR",
        "ROOM_SKINNY",
    } & codes
