"""Room / door / window schedules emitted straight from a plan (no Revit)."""

from __future__ import annotations

import csv
import io

from barndsl import compile_source
from barndsl.schedule import (
    door_rows,
    room_rows,
    schedules_csv,
    schedules_markdown,
    window_rows,
)

SRC = """\
plan "Sched Test"
envelope 33 x 24
ceiling 9
room living:  living   at 0,0    size 18 x 14
room kitchen: kitchen  north-of living size 18 x 10
room hall:    hallway  at 18,0   size 4 x 24
room bed:     bedroom  at 22,0   size 11 x 12
room bath:    bathroom at 22,15  size 11 x 9
open living - kitchen width 8
door living - hall width 3 offset 0.5
door hall - bed width 3 offset 0.5
door hall - bath width 2.67 offset 5.83
entry living south width 3 offset 13
entry kitchen west width 3 offset 3
window living south width 8 offset 2
window bed  south width 4 offset 3
"""


def _plan():
    result = compile_source(SRC)
    assert result.plan is not None
    return result.plan


def test_room_rows_cover_every_room():
    plan = _plan()
    rows = room_rows(plan)
    assert [r["mark"] for r in rows] == [r.id for r in plan.rooms]
    living = next(r for r in rows if r["mark"] == "living")
    assert living["area"] == 252
    # Exterior walls come from the validator, not invented here.
    assert "south" in living["exterior"] and "west" in living["exterior"]


def test_door_rows_number_interior_then_exterior():
    plan = _plan()
    rows = door_rows(plan)
    # 4 interior (the cased open + three swings) + 2 exterior entries.
    assert len(rows) == 6
    assert [r["mark"] for r in rows] == ["D1", "D2", "D3", "D4", "D5", "D6"]
    cased = next(r for r in rows if r["kind"] == "cased")
    assert cased["from"] == "living" and cased["to"] == "kitchen"
    ext = [r for r in rows if r["kind"].startswith("exterior")]
    assert len(ext) == 2
    assert "exterior" in ext[0]["to"]


def test_window_rows_have_height_and_glazed_area():
    plan = _plan()
    rows = window_rows(plan)
    assert len(rows) == 2
    w = rows[0]
    assert w["mark"] == "W1"
    # default head 6.67 - sill 3.0 ≈ 3.67 ft tall.
    assert abs(w["height"] - 3.67) < 0.01
    assert w["area"] > 0


def test_window_near_jamb_offsets_and_corner():
    plan = _plan()
    rows = window_rows(plan)
    # `window living south width 8 offset 2` — a south (horizontal) wall is
    # measured from its WEST start; offset is already the near-jamb distance.
    w1 = next(r for r in rows if r["mark"] == "W1")
    assert w1["offset"] == 2 and w1["corner"] == "W"
    w2 = next(r for r in rows if r["mark"] == "W2")  # window bed south offset 3
    assert w2["offset"] == 3 and w2["corner"] == "W"


def test_exterior_door_offset_and_corner():
    plan = _plan()
    rows = door_rows(plan)
    south_entry = next(r for r in rows if r["to"] == "exterior (south)")
    assert south_entry["offset"] == 13 and south_entry["corner"] == "W"
    west_entry = next(r for r in rows if r["to"] == "exterior (west)")
    # A west (vertical) wall is measured from its SOUTH start.
    assert west_entry["offset"] == 3 and west_entry["corner"] == "S"


def test_interior_door_offset_explicit_and_centered():
    plan = _plan()
    rows = door_rows(plan)
    # `door living - hall width 3 offset 0.5` sits on the vertical wall at x=18
    # (measured from the SOUTH end) at an explicit 0.5 ft.
    lh = next(r for r in rows if r["from"] == "living" and r["to"] == "hall")
    assert lh["offset"] == 0.5 and lh["corner"] == "S"
    # `open living - kitchen width 8` has no offset → centred on the 18 ft shared
    # (horizontal) wall: near jamb at (18 - 8) / 2 = 5 ft from the WEST end.
    cased = next(r for r in rows if r["kind"] == "cased")
    assert cased["offset"] == 5.0 and cased["corner"] == "W"


def test_offset_column_renders_in_markdown_and_csv():
    md = schedules_markdown(_plan(), rooms=False)
    assert "Near jamb" in md
    assert "13′ from W" in md   # the south entry
    assert "5′ from W" in md    # the centred cased opening
    csv_out = schedules_csv(_plan(), rooms=False, doors=True, windows=False)
    assert "Near jamb" in csv_out
    assert "from S" in csv_out  # the vertical-wall doors


def test_markdown_has_a_table_per_schedule():
    md = schedules_markdown(_plan())
    assert "## Room Schedule" in md
    assert "## Door Schedule" in md
    assert "## Window Schedule" in md
    # Each table has a header rule row.
    assert md.count("| --- |") >= 3


def test_markdown_selection_limits_tables():
    md = schedules_markdown(_plan(), rooms=True, doors=False, windows=False)
    assert "Room Schedule" in md
    assert "Door Schedule" not in md
    assert "Window Schedule" not in md


def test_csv_is_parseable_per_block():
    out = schedules_csv(_plan(), rooms=True, doors=False, windows=False)
    # Drop the leading "# Title" comment line, then the rest parses as CSV.
    lines = [ln for ln in out.splitlines() if not ln.startswith("#") and ln.strip()]
    reader = list(csv.reader(io.StringIO("\n".join(lines))))
    assert reader[0][0] == "Mark"
    assert len(reader) == 1 + len(_plan().rooms)
