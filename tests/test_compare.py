"""`barndsl compare`: score/takeoff/diagnostic deltas between two plans."""

from __future__ import annotations

import json

from barndsl import compile_source, compare_plans, comparison_text
from barndsl.cli import main

A = """\
plan "A"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,0 size 12 x 24
door living - bed width 2.67
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""

# B narrows the door (DOOR_NARROW appears) and grows the envelope/rooms.
B = A.replace('plan "A"', 'plan "B"').replace(
    "door living - bed width 2.67", "door living - bed width 2"
).replace("envelope 30 x 24", "envelope 33 x 24").replace(
    "room bed: bedroom at 18,0 size 12 x 24",
    "room bed: bedroom at 18,0 size 15 x 24",
)


def _cmp():
    return compare_plans(compile_source(A), compile_source(B), ("A", "B"))


def test_sides_carry_score_metrics_and_code_multisets():
    cmp = _cmp()
    for side in (cmp["a"], cmp["b"]):
        assert 0 <= side["score"] <= 100
        assert side["metrics"]["footprint_sqft"] > 0
        assert isinstance(side["codes"], dict)
    assert cmp["a"]["name"] == "A" and cmp["b"]["name"] == "B"


def test_deltas_are_b_minus_a():
    cmp = _cmp()
    assert cmp["deltas"]["footprint_sqft"] == 3 * 24
    got = cmp["b"]["score"] - cmp["a"]["score"]
    assert abs(cmp["deltas"]["score"] - round(got, 1)) < 1e-9


def test_introduced_and_resolved_codes():
    cmp = _cmp()
    assert "DOOR_NARROW" in cmp["introduced"]
    # ENVELOPE_MODULE fires for A (30x24 ok; both multiples of 3) — neither
    # side should "resolve" a code the other still has.
    for code in cmp["resolved"]:
        assert cmp["a"]["codes"].get(code, 0) > cmp["b"]["codes"].get(code, 0)


def test_comparison_is_json_able_and_deterministic():
    one, two = _cmp(), _cmp()
    assert one == two
    assert json.loads(json.dumps(one)) == one


def test_text_rendering_names_the_headline_facts():
    text = comparison_text(_cmp())
    assert text.startswith("Score: A ")
    assert "footprint_sqft" in text
    assert "Introduced in B: " in text and "DOOR_NARROW" in text


def test_cli_compare_human_and_json(tmp_path, capsys):
    fa, fb = tmp_path / "a.barn", tmp_path / "b.barn"
    fa.write_text(A, encoding="utf-8")
    fb.write_text(B, encoding="utf-8")
    assert main(["compare", str(fa), str(fb)]) == 0
    out = capsys.readouterr().out
    assert "Score: a.barn" in out

    assert main(["compare", str(fa), str(fb), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["a"]["name"] == "a.barn"

    broken = tmp_path / "broken.barn"
    broken.write_text("not a plan at all\n", encoding="utf-8")
    assert main(["compare", str(fa), str(broken)]) == 1
