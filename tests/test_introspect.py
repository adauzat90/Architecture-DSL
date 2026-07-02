"""The geometry pack: `plan_summary` / `summary_text` / `barndsl inspect`.

What the summary must guarantee (see `barndsl/introspect.py`):

- **rooms** carry the resolved rectangle and the same exterior-wall assignment
  the validator uses (footprint-aware, wings included);
- **adjacency** is the compiler's door graph — one edge per interior door;
- **unplaced** names the actual uncovered footprint pockets, not just a total;
- **free_spans** are legal by construction: an opening placed inside a reported
  span compiles without DOOR_OOB / OPENING_OOB / OPENING_CLASH, because the
  offsets follow the validator's own conventions (door offsets from the shared
  wall's south/west end, window/entry offsets from the room wall's start).
"""

from __future__ import annotations

import json

from barndsl import compile_source, plan_summary, summary_text
from barndsl.cli import main
from barndsl.validation import exterior_walls

# Full-coverage two-room plan: a centred interior door plus two south openings.
FULL = """\
plan "Introspect"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,0 size 12 x 24
door living - bed width 2.67
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""

# Leaves one 12 x 8 pocket at (28, 22) — 96 sqft of unplaced footprint.
POCKET = """\
plan "Pocket"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 40 x 22
room bed: bedroom at 0,22 size 28 x 8
door living - bed width 2.67
entry living south width 3 offset 4
window bed north width 4 offset 3
window living south width 8 offset 8
"""

# The rooms share only part of a wall: the shared edge starts at y=6, so a door
# offset counts from there — not from either room's own corner.
OFFSET = """\
plan "Offset"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,6 size 12 x 18
door living - bed width 3 offset 5
entry living south width 3 offset 4
window bed east width 4 offset 3
window living west width 6 offset 8
"""


def _summary(src: str) -> dict:
    result = compile_source(src)
    assert result.plan is not None
    return plan_summary(result.plan)


def _spans(summary: dict, room: str, wall: str, to: str) -> list[tuple[float, float]]:
    return [
        (s["lo"], s["hi"])
        for s in summary["free_spans"]
        if (s["room"], s["wall"], s["to"]) == (room, wall, to)
    ]


# -- rooms ---------------------------------------------------------------------


def test_rooms_carry_resolved_rectangles_and_exterior_walls():
    result = compile_source(FULL)
    summary = plan_summary(result.plan)
    rooms = {r["id"]: r for r in summary["rooms"]}
    assert rooms["living"] == {
        "id": "living",
        "type": "living",
        "level": 0,
        "x": 0.0,
        "y": 0.0,
        "width": 18.0,
        "length": 24.0,
        "exterior": ["south", "north", "west"],
    }
    # The exterior assignment is the validator's, not a re-derivation.
    for r in result.plan.rooms:
        assert rooms[r.id]["exterior"] == [
            w.value for w in exterior_walls(result.plan, r)
        ]


def test_wing_seam_walls_are_not_exterior():
    src = """\
plan "Elbow"
envelope 30 x 24
wing 12 x 12 at 0,24
room a: living at 0,0 size 12 x 24
room b: bedroom at 0,24 size 12 x 12
"""
    summary = _summary(src)
    rooms = {r["id"]: r for r in summary["rooms"]}
    assert "north" not in rooms["a"]["exterior"]  # the seam into the wing
    assert set(rooms["b"]["exterior"]) == {"north", "west", "east"}
    # And the seam is a free span between the two rooms, from both sides.
    assert _spans(summary, "a", "north", "b") == [(0.0, 12.0)]
    assert _spans(summary, "b", "south", "a") == [(0.0, 12.0)]


# -- adjacency -------------------------------------------------------------------


def test_adjacency_lists_the_door_edges_with_kind_and_width():
    summary = _summary(FULL)
    assert summary["adjacency"] == [
        {"a": "living", "b": "bed", "kind": "swing", "width": 2.67}
    ]


# -- unplaced footprint ----------------------------------------------------------


def test_full_coverage_has_no_pockets():
    summary = _summary(FULL)
    assert summary["unplaced"] == [{"level": 0, "sqft": 0.0, "pockets": []}]


def test_pockets_name_the_uncovered_rectangles():
    summary = _summary(POCKET)
    (ground,) = summary["unplaced"]
    assert ground["level"] == 0 and ground["sqft"] == 96.0
    assert ground["pockets"] == [{"x": 28.0, "y": 22.0, "width": 12.0, "length": 8.0}]


def test_unplaced_is_reported_per_level():
    src = FULL + "room loft: loft at 0,0 size 18 x 24 level 1\n"
    summary = _summary(src)
    by_level = {u["level"]: u for u in summary["unplaced"]}
    assert set(by_level) == {0, 1}
    assert by_level[0]["sqft"] == 0.0
    assert by_level[1]["sqft"] == 288.0  # the footprint minus the 18 x 24 loft


# -- free wall spans ---------------------------------------------------------------


def test_exterior_spans_subtract_openings_and_drop_sub_door_slivers():
    summary = _summary(FULL)
    # living south (18 ft): entry blocks 4-7, window blocks 8-14. The 7-8 ft
    # leftover is under MIN_SPAN (no stock leaf fits), so only two spans remain.
    assert _spans(summary, "living", "south", "exterior") == [(0.0, 4.0), (14.0, 18.0)]
    # An unbroken exterior wall is one whole-wall span.
    assert _spans(summary, "living", "west", "exterior") == [(0.0, 24.0)]


def test_interior_spans_follow_the_centred_door_convention():
    summary = _summary(FULL)
    # The centred 2.67 ft door on the 24 ft shared wall blocks 10.665-13.335 —
    # exactly what DOOR_OOB/OPENING_CLASH would measure.
    assert _spans(summary, "living", "east", "bed") == [(0.0, 10.665), (13.335, 24.0)]
    assert _spans(summary, "bed", "west", "living") == [(0.0, 10.665), (13.335, 24.0)]
    # No interior wall faces the loft of another level or a non-neighbour.
    assert _spans(summary, "living", "north", "bed") == []


def test_interior_spans_are_measured_from_the_shared_wall_start():
    summary = _summary(OFFSET)
    # The shared wall runs y 6..24 (18 ft); the door at offset 5 blocks 5-8 in
    # shared-wall coordinates, NOT in either room's own wall coordinates.
    assert _spans(summary, "living", "east", "bed") == [(0.0, 5.0), (8.0, 18.0)]
    assert _spans(summary, "bed", "west", "living") == [(0.0, 5.0), (8.0, 18.0)]


def test_reported_spans_are_legal_by_construction():
    """Placing an opening inside a reported span never fires an OOB/clash."""
    summary = _summary(OFFSET)
    (span_lo, _) = _spans(summary, "living", "east", "bed")[1]
    (win_lo, _) = _spans(summary, "living", "south", "exterior")[1]
    src = OFFSET + (
        f"door living - bed width 2.5 offset {span_lo}\n"
        f"entry living south width 2.5 offset {win_lo}\n"
    )
    result = compile_source(src)
    assert result.plan is not None
    codes = {d.code for d in result.diagnostics}
    assert not codes & {"DOOR_OOB", "OPENING_OOB", "OPENING_CLASH", "DOOR_NOADJ"}


def test_cross_level_rooms_share_no_spans():
    src = FULL + "room loft: loft at 0,0 size 18 x 24 level 1\n"
    summary = _summary(src)
    assert not any(
        s["to"] == "loft" or s["room"] == "loft" and s["to"] != "exterior"
        for s in summary["free_spans"]
    )


# -- determinism -------------------------------------------------------------------


def test_summary_is_deterministic_and_json_able():
    a = _summary(POCKET)
    b = _summary(POCKET)
    assert a == b
    assert json.loads(json.dumps(a)) == a
    assert set(a) == {"name", "rooms", "adjacency", "unplaced", "free_spans"}


# -- the text block -----------------------------------------------------------------


def test_summary_text_is_the_compact_deterministic_block():
    summary = _summary(FULL)
    text = summary_text(summary)
    assert "  living living L0 0,0 18 x 24 | south north west" in text
    assert "Adjacency (door edges): living-bed (swing 2.67)" in text
    assert "Unplaced footprint (L0): none" in text
    assert "  living east -> bed: 0-10.665, 13.335-24" in text
    assert summary_text(_summary(FULL)) == text


def test_summary_text_names_the_pockets():
    text = summary_text(_summary(POCKET))
    assert "Unplaced footprint (L0): 96 sqft in 1 pocket: 12 x 8 at 28,22" in text


# -- CLI ------------------------------------------------------------------------


def test_cli_inspect_prints_the_tables(tmp_path, capsys):
    p = tmp_path / "full.barn"
    p.write_text(FULL)
    assert main(["inspect", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Plan: Introspect" in out
    assert "Rooms (id type level x,y w x l | exterior walls):" in out
    assert "Free wall spans" in out


def test_cli_inspect_json_matches_plan_summary(tmp_path, capsys):
    p = tmp_path / "pocket.barn"
    p.write_text(POCKET)
    assert main(["inspect", str(p), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == _summary(POCKET)


def test_cli_inspect_still_works_on_a_plan_with_errors(tmp_path, capsys):
    # An out-of-bounds room is a hard error, but its geometry is exactly what
    # you want to look up — inspect reports it rather than refusing.
    src = FULL.replace("room bed: bedroom at 18,0 size 12 x 24",
                       "room bed: bedroom at 18,0 size 20 x 24")
    result = compile_source(src)
    assert result.plan is not None and result.errors
    p = tmp_path / "oob.barn"
    p.write_text(src)
    assert main(["inspect", str(p)]) == 0
    assert "bed bedroom L0 18,0 20 x 24" in capsys.readouterr().out


def test_cli_inspect_fails_without_a_plan(tmp_path, capsys):
    p = tmp_path / "bad.barn"
    p.write_text("nonsense\n")
    assert main(["inspect", str(p)]) == 1
