"""One interior-door span rule, shared by every consumer.

:func:`barndsl.geometry.door_span` is where an interior door sits on its shared
wall as built: a leaf wider than the wall is cut to it and an offset that runs
off either end slides back on (``DOOR_FIT``/``DOOR_OOB`` report the authored
overrun). The drawing, wall bodies, Revit exchange, schedules and introspection
must all agree with it — they used to re-derive the span three different ways,
which only showed on exactly these overrunning doors.
"""

from __future__ import annotations

import pytest

from barndsl import compile_source
from barndsl.edits import Edit, apply_edit, opening_overlays
from barndsl.elements import InteriorDoor
from barndsl.geometry import SharedEdge, door_offset, door_span
from barndsl.introspect import plan_summary
from barndsl.revit import to_revit_model
from barndsl.schedule import door_rows, opening_tag_points
from barndsl.wallbodies import opening_gaps

# A vertical shared wall at x = 5 running y 10 → 22 (12 ft), so a span that
# forgets to add ``edge.lo`` shows up.
EDGE = SharedEdge("v", 5.0, 10.0, 22.0)


@pytest.mark.parametrize(
    ("width", "offset", "span"),
    [
        (3.0, None, (14.5, 17.5)),  # centred
        (3.0, 2.0, (12.0, 15.0)),  # measured from the south/west end
        (3.0, 11.0, (19.0, 22.0)),  # runs off the far end: slid back on
        (3.0, -1.0, (10.0, 13.0)),  # starts before the wall: slid forward
        (14.0, None, (10.0, 22.0)),  # wider than the wall: cut to it
        (14.0, 4.0, (10.0, 22.0)),
    ],
)
def test_door_span_clamps_onto_the_wall(width, offset, span):
    door = InteriorDoor("a", "b", width=width, offset=offset)
    assert door_span(EDGE, door) == pytest.approx(span)
    assert door_offset(EDGE, door) == pytest.approx(span[0] - EDGE.lo)


def test_door_offset_keeps_an_offset_that_fits_exactly():
    # The schedule reports this number: an in-range offset must come back
    # unchanged, not re-derived through world coordinates.
    door = InteriorDoor("a", "b", width=3.0, offset=0.1 + 0.2)
    assert door_offset(EDGE, door) == door.offset


PLAN = """\
plan "Overrun"
envelope 22 x 10
ceiling 9
room a: living  at 0,0  size 12 x 10
room b: bedroom at 12,0 size 10 x 10
door a - b {door}
entry a south width 3 offset 2
window b south width 4 offset 3
"""


@pytest.mark.parametrize(
    ("door", "code", "span"),
    [
        ("width 3 offset 9", "DOOR_OOB", (7.0, 10.0)),  # authored 9-12 on a 10 ft wall
        ("width 12", "DOOR_FIT", (0.0, 10.0)),  # authored -1 to 11
    ],
)
def test_every_consumer_places_an_overrunning_door_at_its_span(door, code, span):
    result = compile_source(PLAN.format(door=door))
    assert code in {d.code for d in result.diagnostics}
    plan = result.plan
    lo, hi = span
    mid = (lo + hi) / 2.0

    # Wall bodies (the SVG and DXF walls are cut at these jambs).
    gaps = [(g.lo, g.hi) for g in opening_gaps(plan, 0) if g.orientation == "v"]
    assert gaps == [pytest.approx(span)]

    # The Revit exchange hosts the door on the wall, not past its end.
    (opening,) = [o for o in to_revit_model(plan).openings if o.rooms == ["a", "b"]]
    assert opening.location == pytest.approx((12.0, mid))
    assert opening.width == pytest.approx(hi - lo)

    # The schedule's tag bubble and offset column describe the drawn door.
    tags = {mark: (x, y) for mark, x, y in opening_tag_points(plan)}
    assert tags["D1"][1] == pytest.approx(mid)
    row = next(r for r in door_rows(plan) if r["mark"] == "D1")
    assert row["offset"] == pytest.approx(lo)

    # Introspection offers only the wall the door leaves free.
    free = [(s["lo"], s["hi"]) for s in plan_summary(plan)["free_spans"]
            if s["room"] == "a" and s["to"] == "b"]
    expected = [(0.0, lo)] if lo >= 2.0 else []
    assert free == [pytest.approx(e) for e in expected]


def test_the_swing_checks_judge_the_drawn_leaf():
    # A 12 ft door on the 10 ft wall is drawn as a 10 ft leaf swinging into the
    # bedroom. Judged as the authored 12 ft leaf, it no longer fit the bedroom,
    # flipped into the living room and fouled the entry swing there.
    codes = {d.code for d in compile_source(PLAN.format(door="width 12")).diagnostics}
    assert "DOOR_FIT" in codes
    assert not codes & {"DOOR_SWING_UNSET", "DOOR_SWING_CLASH"}


# --- where the authored offset must survive -----------------------------------


def test_the_editor_edits_the_authored_offset():
    # The drag handle and inspector write the source's `offset`, so they read it
    # too: moving the overrunning door to where it is drawn (7) must still be a
    # real edit that clears DOOR_OOB, not an "already there" no-op.
    src = PLAN.format(door="width 3 offset 9")
    plan = compile_source(src).plan
    (row,) = [o for o in opening_overlays(plan) if o["kind"] == "interior"]
    assert row["offset"] == 9.0

    r = apply_edit(src, Edit("move_opening", opening="interior", key="a~b~0", offset=7))
    assert r.ok and r.changed
    assert "door a - b width 3 offset 7" in r.source
    assert "DOOR_OOB" not in {d.code for d in compile_source(r.source).diagnostics}


PART = """\
room a: living  at 0,0  size 12 x 10
room b: bedroom at 12,0 size 10 x 10
door a - b width 3 offset 9
"""


@pytest.mark.parametrize("xform", ["", " mirror x", " mirror y", " rotate 90", " rotate 180", " mirror y rotate 90"])
def test_a_stamped_part_keeps_its_door_overrun(tmp_path, xform):
    # Composition maps the authored offset through the transform; mapping the
    # built span instead would quietly fix the part's door and hide DOOR_OOB.
    (tmp_path / "p.barn").write_text(PART)
    src = f'plan "H"\nenvelope 30 x 30\nceiling 9\nuse "p.barn" as q at 0,0{xform}\n'
    result = compile_source(src, base_dir=str(tmp_path))
    assert "DOOR_OOB" in {d.code for d in result.diagnostics}
