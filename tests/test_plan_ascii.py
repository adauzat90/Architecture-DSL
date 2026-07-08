"""The ASCII plan view (`introspect.render_ascii_plan`) and its use in feedback.

A blind model reads a table of rectangles; a shop band sandwiched between the
living core and the bedrooms hides in that table but is obvious in a drawing.
These tests pin the north-up occupancy grid: letters land at the right cells,
voids show as `.`, cells outside the footprint are blank, multi-level plans
render one grid per level, output is ASCII-only (cp1252 consoles), and the whole
thing is a deterministic pure function of the plan.
"""

from __future__ import annotations

from pathlib import Path

from barndsl import compile_source
from barndsl.introspect import render_ascii_plan

GALLERY = Path(__file__).resolve().parent.parent / "examples" / "gallery"

# A tiny 8x6 plan split W|E into two 4x6 rooms — small enough to read every cell.
# At 2 ft/char it is 4 cols x 3 rows: living owns the west half, bed the east.
TINY = """\
plan "Tiny"
envelope 8 x 6
ceiling 9
room living: living at 0,0 size 4 x 6
room bed: bedroom at 4,0 size 4 x 6
door living - bed width 2.67 into bed
entry living south width 3 offset 1
window living south width 2 offset 1
window bed south width 2 offset 1
"""

# A plan with an unassigned pocket: a 60x30 envelope, a 44x30 living room and a
# 16x24 kitchen offset up, leaving a 16x6 void at the SE corner (x 44-60, y 0-6).
VOIDED = """\
plan "Pocket"
envelope 60 x 30
ceiling 9
room living: living  at 0,0   size 44 x 30
room kitchen: kitchen at 44,6  size 16 x 24
entry living south width 3 offset 6
window living south width 8 offset 8
window kitchen east width 5 offset 8
"""


def _grid_rows(text: str) -> list[str]:
    """The occupancy-grid rows only (drop the header, level labels and legend)."""
    rows = []
    for line in text.splitlines():
        if line.startswith("PLAN VIEW") or line.startswith("Level "):
            continue
        if line.startswith("  ") and " = " in line:  # legend entry
            continue
        rows.append(line)
    return rows


# -- header, scale and legend --------------------------------------------------


def test_header_states_north_up_and_the_scale():
    text = render_ascii_plan(compile_source(TINY).plan)
    head = text.splitlines()[0]
    assert head.startswith("PLAN VIEW (north up,")
    assert "1 char ~ 2 ft" in head
    assert ". = unassigned void" in head


def test_legend_maps_each_letter_to_id_type_and_size():
    text = render_ascii_plan(compile_source(TINY).plan)
    assert "  L = living (living 4x6)" in text
    assert "  B = bed (bedroom 4x6)" in text


def test_scale_line_reflects_a_custom_cell_size():
    text = render_ascii_plan(compile_source(TINY).plan, cell_ft=1.0)
    assert "1 char ~ 1 ft" in text.splitlines()[0]


# -- letters land at the right coordinates -------------------------------------


def test_letters_land_at_correct_coordinates():
    # 4 cols x 3 rows; west half is living (L), east half is bed (B), every row.
    rows = _grid_rows(render_ascii_plan(compile_source(TINY).plan))
    assert rows == ["LLBB", "LLBB", "LLBB"]


def test_row_zero_is_north_max_y():
    # A north room and a south room stacked: row 0 (top) must be the north one.
    src = """\
plan "Stack"
envelope 6 x 8
ceiling 9
room south: living  at 0,0 size 6 x 4
room north: bedroom at 0,4 size 6 x 4
door south - north width 2.67 into north
entry south south width 3 offset 1
window south south width 2 offset 1
window north north width 2 offset 1
"""
    rows = _grid_rows(render_ascii_plan(compile_source(src).plan))
    # 3 cols x 4 rows; the top two rows are the north bedroom, the bottom two the
    # south living room (north is up).
    letters = {rows[0][0], rows[1][0]}
    assert letters == {"N"}  # top rows are the north room ('north' -> N)
    assert {rows[2][0], rows[3][0]} == {"S"}  # bottom rows are the south room


# -- voids and outside ---------------------------------------------------------


def test_unassigned_footprint_shows_as_void_dot():
    text = render_ascii_plan(compile_source(VOIDED).plan)
    rows = _grid_rows(text)
    # The SE pocket (x 44-60, y 0-6) is inside the footprint but unassigned, so
    # its cells read as '.'; every cell here is either a room letter or a void.
    assert any("." in r for r in rows)
    joined = "\n".join(rows)
    assert " " not in joined  # a full rectangle: nothing is outside the footprint


def test_cells_outside_the_footprint_are_not_voids():
    # An L-shaped footprint (a wing) leaves a rectangular notch outside the
    # building; those cells are blank (space), NOT void dots. The notch here is at
    # the NE (x 20-30, y 10-20), a trailing gap, so the north rows are shorter than
    # the south rows rather than carrying a '.'.
    src = """\
plan "Ell"
envelope 20 x 20
wing 10 x 10 at 20,0
ceiling 10
room living: living at 0,0 size 20 x 20
room den: office at 20,0 size 10 x 10
door living - den width 2.67
entry living south width 3 offset 4
window living south width 6 offset 6
window den south width 3 offset 3
"""
    text = render_ascii_plan(compile_source(src).plan)
    rows = _grid_rows(text)
    # No void: the whole footprint is covered by the two rooms.
    assert not any("." in r for r in rows)
    # The northern rows (over the notch) are shorter than the southern rows that
    # span both the block and the wing — the outside notch shows as a ragged edge.
    assert min(len(r) for r in rows) < max(len(r) for r in rows)


# -- multi-level ---------------------------------------------------------------


def test_multi_level_plan_renders_one_grid_per_level():
    text = render_ascii_plan(compile_source((GALLERY / "two_story.barn").read_text()).plan)
    assert "Level 0:" in text
    assert "Level 1:" in text
    # The loft (level 1) appears in the level-1 legend; ground rooms do not.
    l0, l1 = text.split("Level 1:")
    assert "loft" in l1
    assert "loft" not in l0


def test_single_level_plan_has_no_level_headers():
    text = render_ascii_plan(compile_source(TINY).plan)
    assert "Level 0:" not in text
    assert "Level 1:" not in text


# -- determinism and ASCII-only ------------------------------------------------


def test_render_is_deterministic():
    a = render_ascii_plan(compile_source(TINY).plan)
    b = render_ascii_plan(compile_source(TINY).plan)
    assert a == b


def test_output_is_ascii_only():
    for src in (TINY, VOIDED, (GALLERY / "two_story.barn").read_text()):
        s = render_ascii_plan(compile_source(src).plan)
        assert s.isascii(), s


def test_wide_plan_stays_compact():
    # A 60 ft wide plan must render at <= ~35 chars per grid row (30 at 2 ft/char).
    text = render_ascii_plan(compile_source(VOIDED).plan)
    for row in _grid_rows(text):
        assert len(row) <= 35


# -- letter disambiguation -----------------------------------------------------


def test_colliding_base_letters_are_disambiguated():
    # `bath` and `bed` both want B; the second keeps the base's meaning with the
    # lowercase form rather than jumping to an unrelated letter.
    src = """\
plan "Collide"
envelope 30 x 12
ceiling 9
room living: living at 0,0 size 12 x 12
room bath: bathroom at 12,0 size 8 x 12
room bed: bedroom at 20,0 size 10 x 12
door living - bath width 2.67
door bath - bed width 2.67 into bed
entry living south width 3 offset 4
window living south width 4 offset 4
window bath east width 2 offset 4 sill 5
window bed south width 4 offset 3
"""
    text = render_ascii_plan(compile_source(src).plan)
    # Distinct one-char labels, and every grid cell is exactly one character wide.
    assert "  B = bath (bathroom 8x12)" in text
    assert "  b = bed (bedroom 10x12)" in text
    rows = _grid_rows(text)
    assert len({len(r) for r in rows}) == 1  # uniform row width -> 1 char per cell
