"""The helpers modules share are public names, pinned here (TD-3).

Each of these used to be imported across modules under its ``_private`` name;
``barndsl dev audit`` now fails on any such import. These tests pin the
contracts other modules rely on.
"""

from __future__ import annotations

import pytest

from barndsl import compile_source
from barndsl.compiler import PLACEMENT_ANCHORS, did_you_mean, parse_ft_in, tokenize_line
from barndsl.fixtures import quarter_turns
from barndsl.geometry import point_rect_distance
from barndsl.pragma import comment_start
from barndsl.schedule import schedule_tables


def test_tokenize_line_splits_words_keeps_quoted_spans_and_drops_comments():
    toks = tokenize_line('note "a # b, c" at 2,3 # trailing', 7)
    assert [t.text for t in toks] == ["note", "a # b, c", "at", "2", "3"]
    quoted = toks[1]
    assert quoted.quoted and (quoted.line, quoted.col, quoted.end_col) == (7, 6, 16)
    assert tokenize_line("room a: living {at 0,0}", 1)[2].text == "living"


@pytest.mark.parametrize(
    ("text", "feet"),
    [("12-6", 12.5), ("12′6″", 12.5), ("12'6", 12.5), ("12′", 12.0), ("6″", 0.5),
     ("12", None), ("12-13", None), ("-12-6", None), ("abc", None)],
)
def test_parse_ft_in(text, feet):
    assert parse_ft_in(text) == feet


def test_did_you_mean_ranks_close_matches_or_says_nothing():
    assert did_you_mean("rooom", ("room", "roof", "porch")) == "Did you mean `room` or `roof`? "
    assert did_you_mean("ROM", ("room", "roof")) == "Did you mean `room`? "  # case-folded
    assert did_you_mean("zzz", ("room",)) == ""


def test_placement_anchors_are_what_the_parser_accepts():
    for anchor in PLACEMENT_ANCHORS:
        src = ('plan "P"\nenvelope 40 x 40\nceiling 9\n'
               "room a: living at 10,10 size 10 x 10\n"
               f"room b: office {anchor} a size 10 x 10\n")
        assert not [d for d in compile_source(src).errors if d.code == "BAD_PLACEMENT"], anchor


def test_comment_start_skips_hashes_inside_strings():
    assert comment_start('note "a # b" # real') == 13
    assert comment_start('note "say \\" # hi" # real') == 19
    assert comment_start("room a: living at 0,0") is None


@pytest.mark.parametrize(("rotation", "turns"), [(0, 0), (90, 1), (180, 2), (270, 3), (360, 0), (-90, 3), (100, 1), (None, 0)])
def test_quarter_turns(rotation, turns):
    assert quarter_turns(rotation) == turns


def test_point_rect_distance():
    rect = (0.0, 0.0, 4.0, 2.0)
    assert point_rect_distance(1.0, 1.0, rect) == 0.0  # inside
    assert point_rect_distance(7.0, 6.0, rect) == 5.0  # 3-4-5 off the corner
    assert point_rect_distance(2.0, -1.5, rect) == 1.5  # straight below


def test_schedule_tables_yield_the_requested_schedules():
    plan = compile_source(
        'plan "S"\nenvelope 20 x 10\nceiling 9\n'
        "room a: living at 0,0 size 10 x 10\nroom b: bedroom at 10,0 size 10 x 10\n"
        "door a - b width 3\nentry a south width 3 offset 2\nwindow b east width 3 offset 2\n"
    ).plan
    tables = list(schedule_tables(plan, rooms=True, doors=True, windows=True))
    assert [t[0] for t in tables] == ["Room Schedule", "Door Schedule", "Window Schedule"]
    assert [len(t[2]) for t in tables] == [2, 2, 1]
    assert all(c.header for _, cols, _ in tables for c in cols)
    assert [t[0] for t in schedule_tables(plan, rooms=False, doors=True, windows=False)] == ["Door Schedule"]
