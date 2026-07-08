"""Tests for the design-quality INFO nudges added to `_validate_design_quality`.

These never block a compile (they are INFO, so `is_valid` stays true); they flow
through the same diagnostic channel as code errors to coach the author toward a
more livable, more economical plan:

- WET_GROUP        — cluster wet rooms onto a shared plumbing wall.
- NO_CLOSET        — a bedroom with no adjacent closet.
- ROOM_PROPORTION  — a habitable room shaped like a bowling alley.
- GARAGE_BEDROOM   — a garage opening into a sleeping room (WARNING; IRC R302.5.1).
- GARAGE_NO_ENTRY  — a garage with no interior people-door into the house.
"""

from __future__ import annotations

from barndsl import compile_source


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- WET_GROUP ---------------------------------------------------------------


def test_scattered_wet_rooms_nudge_to_cluster():
    # kitchen, bath, and laundry each sit alone (no two share a wall); the
    # office and bedroom fill the envelope so no AREA_VOID error muddies it.
    src = """\
plan "Scattered plumbing"
envelope 40 x 30
ceiling 9
room living:  living   at 0,0   size 16 x 30
room kitchen: kitchen  at 16,0  size 12 x 12
room office:  office   at 28,0  size 12 x 12
room hall:    hallway  at 16,12 size 24 x 6
room laundry: laundry  at 16,18 size 8 x 12
room bed:     bedroom  at 24,18 size 8 x 12
room bath:    bathroom at 32,18 size 8 x 12
door living - hall width 3
door living - kitchen width 6
door hall - office width 2.67
door hall - bath width 2.67
door hall - laundry width 2.67
door hall - bed width 2.67
entry living south width 3 offset 6
window bath east width 3 offset 4
window bed north width 4 offset 2
"""
    r = compile_source(src)
    assert "WET_GROUP" in _codes(r, "info")
    assert r.ok  # it's a nudge, not a blocker


def test_clustered_wet_rooms_are_silent():
    # bath and laundry share a wall with the kitchen → a wet wall, no nudge.
    src = """\
plan "Wet wall"
envelope 40 x 30
ceiling 9
room living:  living   at 0,0   size 16 x 30
room kitchen: kitchen  at 16,0  size 24 x 12
room bath:    bathroom at 16,12 size 12 x 10
room laundry: laundry  at 28,12 size 12 x 10
room hall:    hallway  at 16,22 size 24 x 4
door living - kitchen width 6
door living - hall width 3
door hall - bath width 2.67
door hall - laundry width 2.67
entry living south width 3 offset 6
window bath west width 3 offset 4
"""
    r = compile_source(src)
    assert "WET_GROUP" not in _codes(r, "info")


def test_two_wet_rooms_do_not_trip_wet_group():
    # Fewer than 3 wet rooms: a lone bath away from the kitchen is normal.
    src = """\
plan "Just two"
envelope 36 x 24
ceiling 9
room living:  living   at 0,0   size 18 x 24
room kitchen: kitchen  at 18,0  size 18 x 12
room bath:    bathroom at 18,12 size 12 x 12
door living - kitchen width 6
door living - bath width 2.67
entry living south width 3 offset 6
window bath east width 3 offset 4
"""
    r = compile_source(src)
    assert "WET_GROUP" not in _codes(r, "info")


# --- NO_CLOSET ---------------------------------------------------------------


def test_bedroom_without_a_closet_is_flagged():
    src = """\
plan "No closet"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 14 x 24
room hall:   hallway at 14,0 size 4 x 24
room bed:    bedroom at 18,0 size 12 x 24
door living - hall width 3
door hall - bed width 2.67
entry living south width 3 offset 6
window bed east width 4 offset 8
"""
    r = compile_source(src)
    assert "NO_CLOSET" in _codes(r, "info")
    msg = next(d for d in r.infos if d.code == "NO_CLOSET").message
    assert "bed" in msg


def test_bedroom_with_an_adjacent_closet_is_silent():
    src = """\
plan "Has closet"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 14 x 24
room hall:   hallway at 14,0 size 4 x 24
room bed:    bedroom at 18,0 size 12 x 18
room cl:     closet  at 18,18 size 12 x 6
door living - hall width 3
door hall - bed width 2.67
door bed - cl width 2.5
entry living south width 3 offset 6
window bed east width 4 offset 6
"""
    r = compile_source(src)
    assert "NO_CLOSET" not in _codes(r, "info")


def test_no_closet_reports_once_per_bedroom():
    src = """\
plan "Two closetless beds"
envelope 40 x 24
ceiling 9
room living: living  at 0,0   size 12 x 24
room hall:   hallway at 12,0  size 4 x 24
room bed1:   bedroom at 16,0  size 12 x 24
room bed2:   bedroom at 28,0  size 12 x 24
door living - hall width 3
door hall - bed1 width 2.67
door hall - bed2 width 2.67
entry living south width 3 offset 6
window bed1 north width 4 offset 4
window bed2 east width 4 offset 8
"""
    r = compile_source(src)
    closetless = {d.room for d in r.infos if d.code == "NO_CLOSET"}
    assert closetless == {"bed1", "bed2"}


# --- ROOM_PROPORTION ---------------------------------------------------------


def test_bowling_alley_living_room_is_flagged():
    # 30 x 8 living = 3.75:1 — too elongated to furnish.
    src = """\
plan "Long room"
envelope 30 x 20
ceiling 9
room living:  living  at 0,0  size 30 x 8
room kitchen: kitchen at 0,8  size 30 x 12
door living - kitchen width 8
entry living south width 3 offset 12
window living south width 12 offset 8
window kitchen north width 12 offset 8
"""
    r = compile_source(src)
    assert "ROOM_PROPORTION" in _codes(r, "info")
    msg = next(d for d in r.infos if d.code == "ROOM_PROPORTION").message
    assert "living" in msg


def test_a_skinny_hallway_is_not_a_proportion_warning():
    # A 24 x 3 hallway is 8:1 but hallways are *meant* to be skinny (not habitable).
    src = """\
plan "Spine"
envelope 24 x 27
ceiling 9
room hall:   hallway at 0,0  size 24 x 3
room living: living  at 0,3  size 24 x 24
door hall - living width 3
entry hall south width 3 offset 10
window living north width 14 offset 5
"""
    r = compile_source(src)
    assert "ROOM_PROPORTION" not in _codes(r, "info")


def test_a_squarish_room_is_silent():
    src = """\
plan "Square"
envelope 20 x 20
ceiling 9
room living: living at 0,0 size 20 x 20
entry living south width 3 offset 8
window living south width 8 offset 6
window living west width 8 offset 6
"""
    r = compile_source(src)
    assert "ROOM_PROPORTION" not in _codes(r, "info")


# --- MUDROOM_SHAPE -------------------------------------------------------------
# A mudroom is a drop zone (bench + hooks + a 3 ft walkway), so it wants >= 5 ft
# of width and a compact footprint. ROOM_PROPORTION never sees one (mudrooms
# aren't habitable) — live agent plans buffered shops with 3-4 ft strips
# labelled "mudroom" and no check said a word. Under the width bar the room
# CANNOT do its job, so that tier is a WARNING; elongated-but-wide is an INFO.


def test_long_skinny_mudroom_is_a_warning():
    # The Wheatland shape: a mudroom strip running the building's full depth.
    src = """\
plan "Skinny mud"
envelope 40 x 28
ceiling 9
room shop: shop at 0,0 size 20 x 28
room mud: mudroom at 20,0 size 4 x 28
room living: living at 24,0 size 16 x 28
door mud - shop width 3 offset 1 into shop
door mud - living width 3 offset 1
entry living south width 3 offset 1
entry shop south width 3 offset 2
window living south width 8 offset 6
window living east width 8 offset 6
"""
    r = compile_source(src)
    assert "MUDROOM_SHAPE" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "MUDROOM_SHAPE").message
    assert "corridor" in msg
    assert r.ok  # a warning, not a compile stopper


def test_narrow_mudroom_warns_even_when_compact():
    # 4 x 8 is only 2:1, but 4 ft can't hold a bench plus a walkway.
    src = """\
plan "Narrow mud"
envelope 28 x 20
ceiling 9
room living: living at 0,0 size 24 x 20
room mud: mudroom at 24,0 size 4 x 8
room util: utility at 24,8 size 4 x 12
door living - mud width 3 offset 1
door living - util width 2.67 offset 2
entry mud south width 3 offset 0.5
entry living south width 3 offset 6
window living south width 10 offset 8
window living west width 8 offset 6
"""
    r = compile_source(src)
    assert "MUDROOM_SHAPE" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "MUDROOM_SHAPE").message
    assert "only 4 ft wide" in msg


def test_wide_but_elongated_mudroom_is_an_info_nudge():
    # 6 ft wide holds the bench and walkway, but 6 x 18 (3:1) reads corridor.
    src = """\
plan "Long mud"
envelope 30 x 18
ceiling 9
room living: living at 0,0 size 24 x 18
room mud: mudroom at 24,0 size 6 x 18
door living - mud width 3 offset 1
entry mud south width 3 offset 1.5
entry living south width 3 offset 6
window living south width 10 offset 8
window living west width 8 offset 5
"""
    r = compile_source(src)
    assert "MUDROOM_SHAPE" in _codes(r, "info")
    assert "MUDROOM_SHAPE" not in _codes(r, "warning")


def test_compact_mudroom_is_silent():
    # The classic 6 x 8 drop zone passes without comment.
    src = """\
plan "Good mud"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 24 x 24
room mud: mudroom at 24,0 size 6 x 8
room laundry: laundry at 24,8 size 6 x 16
door living - mud width 3 offset 1
door living - laundry width 2.67 offset 2
entry mud south width 3 offset 1.5
entry living south width 3 offset 6
window living south width 12 offset 8
"""
    r = compile_source(src)
    assert "MUDROOM_SHAPE" not in _codes(r, "info")


# --- GARAGE_BEDROOM (warning) ------------------------------------------------


def test_garage_opening_into_a_bedroom_warns():
    src = """\
plan "Garage into bed"
envelope 40 x 24
ceiling 9
room living: living  at 0,0   size 16 x 24
room bed:    bedroom at 16,0  size 12 x 24
room garage: garage  at 28,0  size 12 x 24
door living - bed width 2.67
door bed - garage width 2.67
entry living south width 3 offset 6
entry garage south width 9 offset 1
window bed north width 4 offset 4
"""
    r = compile_source(src)
    assert "GARAGE_BEDROOM" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "GARAGE_BEDROOM").message
    assert "bed" in msg


def test_garage_into_a_mudroom_does_not_warn():
    src = """\
plan "Garage into mud"
envelope 44 x 24
ceiling 9
room living: living  at 0,0   size 16 x 24
room bed:    bedroom at 16,0  size 12 x 24
room mud:    mudroom at 28,0  size 6 x 24
room garage: garage  at 34,0  size 10 x 24
door living - bed width 2.67
door living - mud width 2.67
door mud - garage width 2.67
entry living south width 3 offset 6
entry garage south width 9 offset 1
window bed north width 4 offset 4
"""
    r = compile_source(src)
    assert "GARAGE_BEDROOM" not in _codes(r, "warning")


# --- GARAGE_NO_ENTRY (info) --------------------------------------------------


def test_garage_with_no_interior_door_is_flagged():
    # The garage abuts the living room but only connects via its own vehicle entry,
    # so reachability (NO_ACCESS) is satisfied yet there's no way in from the house.
    src = """\
plan "Disconnected garage"
envelope 40 x 24
ceiling 9
room living: living at 0,0   size 24 x 24
room garage: garage at 24,0  size 16 x 24
entry living south width 3 offset 6
entry garage south width 9 offset 3
window living north width 12 offset 6
"""
    r = compile_source(src)
    assert "GARAGE_NO_ENTRY" in _codes(r, "info")
    assert "NO_ACCESS" not in _codes(r, "error")  # the gap this check fills
    msg = next(d for d in r.infos if d.code == "GARAGE_NO_ENTRY").message
    assert "garage" in msg


def test_garage_with_an_interior_door_is_silent():
    src = """\
plan "Connected garage"
envelope 40 x 24
ceiling 9
room living: living at 0,0   size 24 x 24
room garage: garage at 24,0  size 16 x 24
door living - garage width 2.67
entry living south width 3 offset 6
entry garage south width 9 offset 3
window living north width 12 offset 6
"""
    r = compile_source(src)
    assert "GARAGE_NO_ENTRY" not in _codes(r, "info")
