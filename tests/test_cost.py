"""Tests for the cost estimate (review item #8 — the roadmap's open item).

A transparent, deterministic $/quantity takeoff with an overridable rate table.
Same plan in / same number out; every rate is stated and replaceable.
"""

from __future__ import annotations

from barndsl import DEFAULT_RATES, compile_source, estimate_cost
from barndsl.cost import CostLine


def _line(report, item):
    return next((line for line in report.lines if line.item == item), None)


_PLAN = """\
plan "Cost"
envelope 40 x 30
ceiling 9
frame bay 12 span 40
room living:  living  at 0,0   size 24 x 30
room kitchen: kitchen at 24,0  size 16 x 18
room bath:    bathroom at 24,18 size 16 x 12
open living - kitchen width 8
door kitchen - bath width 2.67 offset 2
entry living south width 3 offset 8
window living south width 6 offset 16
window kitchen east width 4 offset 6
window bath east width 3 offset 2
porch front at 0,-8 size 24 x 8 covered
"""


def test_cost_line_math():
    line = CostLine("x", 10.0, "sqft", 7.0)
    assert line.cost == 70.0


def test_estimate_has_lines_and_positive_subtotal():
    report = estimate_cost(compile_source(_PLAN).plan)
    assert report.lines
    assert report.subtotal > 0
    # the subtotal is exactly the sum of the lines (no hidden terms).
    assert report.subtotal == sum(line.cost for line in report.lines)


def test_zero_quantity_lines_are_omitted():
    # This plan has no half-bath and no overhead door → those lines don't appear.
    report = estimate_cost(compile_source(_PLAN).plan)
    assert _line(report, "Half baths") is None
    assert _line(report, "Overhead doors") is None
    # …but it does have a frame, a porch, and a kitchen.
    assert _line(report, "Structural frame") is not None
    assert _line(report, "Porch") is not None
    assert _line(report, "Kitchen") is not None


def test_frameless_plan_has_no_frame_lines():
    frameless = _PLAN.replace("frame bay 12 span 40\n", "")
    report = estimate_cost(compile_source(frameless).plan)
    assert _line(report, "Structural frame") is None
    assert _line(report, "Structural posts") is None


def test_openings_are_counted_by_kind():
    src = _PLAN.replace(
        "porch front at 0,-8 size 24 x 8 covered",
        "door living east overhead width 9 offset 2\nporch front at 0,-8 size 24 x 8 covered",
    )
    # (the overhead door needs a garage/shop wall; use a shop instead of bath edge)
    src = src.replace("room bath:    bathroom at 24,18 size 16 x 12",
                      "room shop:    shop at 24,18 size 16 x 12").replace(
        "door kitchen - bath width 2.67 offset 2", "door kitchen - shop width 2.67 offset 2"
    ).replace("window bath east", "window shop east").replace(
        "door living east overhead width 9 offset 2",
        "door shop east overhead width 9 offset 2",
    )
    report = estimate_cost(compile_source(src).plan)
    oh = _line(report, "Overhead doors")
    assert oh is not None and oh.qty == 1
    # an overhead door is not counted as an exterior people-door
    ext = _line(report, "Exterior doors")
    assert ext is not None and ext.qty == 1


def test_rates_override_and_fallback():
    plan = compile_source(_PLAN).plan
    base = estimate_cost(plan).subtotal
    # Doubling the interior rate raises the total; unspecified rates fall back.
    bumped = estimate_cost(plan, rates={"interior": DEFAULT_RATES["interior"] * 2})
    assert bumped.subtotal > base
    assert _line(bumped, "Foundation (slab)").rate == DEFAULT_RATES["foundation"]


def test_deterministic():
    plan = compile_source(_PLAN).plan
    assert estimate_cost(plan).subtotal == estimate_cost(plan).subtotal


def test_to_dict_shape():
    d = estimate_cost(compile_source(_PLAN).plan).to_dict()
    assert "lines" in d and "subtotal" in d
    assert all({"item", "qty", "unit", "rate", "cost"} <= set(line) for line in d["lines"])
