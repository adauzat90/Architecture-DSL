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


# A pair whose diagnostic COUNTS move without a code fully disappearing: A has
# three bedrooms lacking egress windows (BEDROOM_EGRESS/NAT_LIGHT/VENT_AREA each
# fire per-room); B adds a window to one of them, so each count merely DROPS.
_FEWER_A = """plan "A"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 20 x 30
room b1: bedroom at 20,0 size 10 x 15
room b2: bedroom at 30,0 size 10 x 15
room b3: bedroom at 20,15 size 10 x 15
door living - b1 width 2.8
door living - b2 width 2.8
door living - b3 width 2.8
entry living south width 3 offset 4
"""
_FEWER_B = _FEWER_A.replace('plan "A"', 'plan "B"') + "window b3 north width 4 offset 3\n"


def _fewer_cmp(swap=False):
    ra, rb = compile_source(_FEWER_A), compile_source(_FEWER_B)
    return compare_plans(rb, ra, ("B", "A")) if swap else compare_plans(ra, rb, ("A", "B"))


def test_count_drop_is_fewer_not_resolved():
    """The confirmed bug: a code whose count merely DROPS (3 → 2) is `fewer`,
    never `resolved` — it still fires in B."""
    cmp = _fewer_cmp()
    assert cmp["fewer"]["BEDROOM_EGRESS"] == [3, 2]
    assert "BEDROOM_EGRESS" not in cmp["resolved"]
    assert "BEDROOM_EGRESS" not in cmp["introduced"]
    # Every `fewer` entry is genuinely present-in-both, strictly dropped.
    for code, (na, nb) in cmp["fewer"].items():
        assert na > nb > 0


def test_more_bucket_is_symmetric_to_fewer():
    cmp = _fewer_cmp(swap=True)  # A now has MORE of these than B
    assert cmp["more"]["BEDROOM_EGRESS"] == [2, 3]
    assert not cmp["fewer"]
    for code, (na, nb) in cmp["more"].items():
        assert nb > na > 0


# A pair where a code goes to ZERO (resolved) and another rises (more): widening
# a too-narrow door removes DOOR_NARROW entirely and shifts DOOR_SIZE up.
_RES_A = """plan "A"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 20 x 30
room b1: bedroom at 20,0 size 10 x 15
room b2: bedroom at 30,0 size 10 x 15
window b1 north width 4 offset 3
window b2 north width 4 offset 3
door living - b1 width 2
door living - b2 width 2.8
entry living south width 3 offset 4
"""
_RES_B = _RES_A.replace('plan "A"', 'plan "B"').replace(
    "door living - b1 width 2\n", "door living - b1 width 2.8\n"
)


def test_resolved_means_gone_entirely():
    cmp = compare_plans(compile_source(_RES_A), compile_source(_RES_B), ("A", "B"))
    assert cmp["resolved"]["DOOR_NARROW"] == 1  # A had 1, B has 0
    assert cmp["b"]["codes"].get("DOOR_NARROW", 0) == 0
    for code in cmp["resolved"]:
        assert cmp["b"]["codes"].get(code, 0) == 0
    # Introduced is the mirror image: swapping the sides makes it introduced.
    swapped = compare_plans(compile_source(_RES_B), compile_source(_RES_A), ("B", "A"))
    assert swapped["introduced"]["DOOR_NARROW"] == 1


def test_text_renders_all_four_buckets():
    text = comparison_text(_fewer_cmp())
    assert "Fewer in B: BEDROOM_EGRESS (3 → 2)" in text
    res = comparison_text(compare_plans(compile_source(_RES_A), compile_source(_RES_B), ("A", "B")))
    assert "Resolved in B: DOOR_NARROW" in res
    assert "More in B: DOOR_SIZE (1 → 2)" in res


# --- cost delta --------------------------------------------------------------

from pathlib import Path  # noqa: E402

_COTTAGE = (Path(__file__).resolve().parent.parent / "examples" / "gallery" / "cottage.barn").read_text()


def test_cost_delta_present_when_both_compile_clean():
    a = compile_source(_COTTAGE)
    b = compile_source(_COTTAGE.replace('plan "', 'plan "', 1) + "\noverhang 2\n")
    cmp = compare_plans(a, b, ("A", "B"))
    assert set(cmp["cost"]) == {"a", "b", "delta"}
    assert cmp["cost"]["delta"] == round(cmp["cost"]["b"] - cmp["cost"]["a"], 2)
    assert cmp["cost"]["delta"] > 0  # a 2 ft overhang grows the roof line
    text = comparison_text(cmp)
    assert "Cost: $" in text and "→" in text and "+$" in text


def test_cost_delta_omitted_when_a_side_is_broken():
    a = compile_source(_COTTAGE)
    broken = compile_source("not a plan at all\n")
    cmp = compare_plans(a, broken, ("A", "broken"))
    assert "cost" not in cmp
    assert "Cost:" not in comparison_text(cmp)


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


# -- N-way change-order history (A → B → C …) --------------------------------


def test_two_file_call_keeps_exact_flat_shape():
    """No regression for scripts: the two-file result has no `steps`/`overall`."""
    cmp = _cmp()
    assert set(cmp) >= {"a", "b", "deltas", "resolved", "introduced", "fewer", "more"}
    assert "steps" not in cmp and "overall" not in cmp


def test_series_steps_are_consecutive_pairwise_shapes():
    from barndsl import compare_series

    # C grows the bed again — a three-scheme history A → B → C.
    C = B.replace('plan "B"', 'plan "C"').replace(
        "room bed: bedroom at 18,0 size 15 x 24",
        "room bed: bedroom at 18,0 size 18 x 24",
    ).replace("envelope 33 x 24", "envelope 36 x 24")
    ra, rb, rc = compile_source(A), compile_source(B), compile_source(C)
    series = compare_series([ra, rb, rc], ["A", "B", "C"])

    assert set(series) == {"steps", "overall"}
    assert len(series["steps"]) == 2
    # Each step is exactly a compare_plans dict, spanning the right pair.
    assert series["steps"][0]["a"]["name"] == "A" and series["steps"][0]["b"]["name"] == "B"
    assert series["steps"][1]["a"]["name"] == "B" and series["steps"][1]["b"]["name"] == "C"
    for step in series["steps"]:
        assert set(step) >= {"a", "b", "deltas", "resolved", "introduced", "fewer", "more"}
    # Each step matches a standalone pairwise compare (bit-for-bit).
    assert series["steps"][0] == compare_plans(ra, rb, ("A", "B"))
    assert series["steps"][1] == compare_plans(rb, rc, ("B", "C"))


def test_series_overall_is_head_to_tail():
    from barndsl import compare_series

    C = B.replace('plan "B"', 'plan "C"')
    ra, rb, rc = compile_source(A), compile_source(B), compile_source(C)
    series = compare_series([ra, rb, rc], ["A", "B", "C"])
    o = series["overall"]
    assert o["names"] == ["A", "C"]
    # Score first/last match the endpoints; delta is last − first.
    assert o["score"]["first"] == series["steps"][0]["a"]["score"]
    assert o["score"]["last"] == series["steps"][-1]["b"]["score"]
    assert o["score"]["delta"] == round(o["score"]["last"] - o["score"]["first"], 1)
    # Both ends compile cleanly here, so a head-to-tail cost is present.
    assert set(o["cost"]) == {"first", "last", "delta"}
    assert o["cost"]["delta"] == round(o["cost"]["last"] - o["cost"]["first"], 2)


def test_series_text_has_pairwise_headers_and_summary():
    from barndsl import compare_series, series_text

    C = B.replace('plan "B"', 'plan "C"')
    series = compare_series(
        [compile_source(A), compile_source(B), compile_source(C)], ["A", "B", "C"]
    )
    text = series_text(series)
    assert "=== A → B ===" in text
    assert "=== B → C ===" in text
    assert "Overall A → C:" in text


def test_cli_three_files_emits_steps_json(tmp_path, capsys):
    fa, fb, fc = (tmp_path / n for n in ("a.barn", "b.barn", "c.barn"))
    fa.write_text(A, encoding="utf-8")
    fb.write_text(B, encoding="utf-8")
    fc.write_text(B.replace('plan "B"', 'plan "C"'), encoding="utf-8")
    rc = main(["compare", str(fa), str(fb), str(fc), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"steps", "overall"}
    assert len(out["steps"]) == 2


def test_cli_two_files_json_stays_flat(tmp_path, capsys):
    fa, fb = tmp_path / "a.barn", tmp_path / "b.barn"
    fa.write_text(A, encoding="utf-8")
    fb.write_text(B, encoding="utf-8")
    assert main(["compare", str(fa), str(fb), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "steps" not in out and "a" in out and "b" in out
