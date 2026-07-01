"""The slab-on-grade foundation: slab, thickened perimeter edge, pad footings.

The barndominium foundation is the one thing every owner asks about, and it's
derivable from the footprint. These pin the pure foundation plan, its concrete
takeoff, and that it rides in the exchange.
"""

import math

import pytest

from barndsl import (
    RoomType as T,
)
from barndsl import (
    barndominium,
    compile_source,
    foundation_plan,
    to_revit_model,
)
from barndsl.constants import FOOTING_SIZE, SLAB_THICKNESS, TURNDOWN_DEPTH, TURNDOWN_WIDTH


def _plan():
    return (
        barndominium("x").envelope(40, 30).ceiling(10)
        .add_room("living", T.LIVING, x=0, y=0, width=40, length=30)
    )


def test_slab_covers_the_footprint_sections():
    plan = _plan()
    fdn = foundation_plan(plan)
    assert fdn["sections"] == [[0.0, 0.0, 40.0, 30.0]]
    assert fdn["slab_thickness"] == pytest.approx(SLAB_THICKNESS)
    assert fdn["top"] == 0.0


def test_thickened_edge_traces_the_perimeter():
    plan = _plan()
    fdn = foundation_plan(plan)
    edge = fdn["edge"]
    total = sum(
        math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in edge["segments"]
    )
    assert total == pytest.approx(2 * (40 + 30))  # rectangle perimeter
    assert edge["width"] == pytest.approx(TURNDOWN_WIDTH)
    assert edge["depth"] == pytest.approx(TURNDOWN_DEPTH)


def test_pad_footings_land_under_each_post():
    plan = _plan().frame(bay=12, span=40)
    fdn = foundation_plan(plan)
    assert len(fdn["footings"]) == len(plan.posts) > 0
    pts = {(round(f["point"][0], 3), round(f["point"][1], 3)) for f in fdn["footings"]}
    for p in plan.posts:
        assert (round(p.x, 3), round(p.y, 3)) in pts
    assert all(f["size"] == pytest.approx(FOOTING_SIZE) for f in fdn["footings"])


def test_no_footings_without_a_frame():
    assert foundation_plan(_plan())["footings"] == []


def test_concrete_takeoff_is_positive_and_in_metrics():
    plan = _plan()
    fdn = foundation_plan(plan)
    assert fdn["concrete_yd3"] > 0
    # The metrics takeoff matches the foundation plan's own total.
    assert plan.metrics()["foundation_concrete_yd3"] == pytest.approx(fdn["concrete_yd3"])


def test_exchange_carries_the_foundation():
    data = to_revit_model(_plan().frame()).to_dict()
    fdn = data["foundation"]
    assert fdn is not None
    assert fdn["sections"] and fdn["edge"]["segments"] and fdn["footings"]


def test_example_plan_gets_a_foundation():
    plan = compile_source(open("examples/cedar_ridge.barn").read()).plan
    data = to_revit_model(plan).to_dict()
    assert data["foundation"]["concrete_yd3"] > 0
