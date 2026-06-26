"""Tests for the pure engine: DSL, validation, geometry, rendering.

These run with no API key and no `anthropic` dependency.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

from barndsl import Direction as D
from barndsl import RoomType as T
from barndsl import barndominium, render_svg, validate
from barndsl.geometry import shared_edge


def _example():
    from simple_barndo import build_example

    return build_example()


def test_example_has_no_errors():
    report = validate(_example())
    assert report.is_valid, "\n".join(str(i) for i in report.errors)


def test_metrics_match_geometry():
    plan = _example()
    m = plan.metrics()
    assert m["footprint_sqft"] == 60 * 40
    # The example tiles the footprint exactly.
    assert abs(m["assigned_sqft"] - 2400) < 1e-6
    assert m["bedroom_count"] == 3
    assert m["bathroom_count"] == 1


def test_shared_edge_detection():
    a = barndominium("t").add_room("a", T.LIVING, x=0, y=0, width=10, length=10).rooms[0]
    b = barndominium("t").add_room("b", T.KITCHEN, x=10, y=0, width=10, length=10).rooms[0]
    edge = shared_edge(a, b)
    assert edge is not None and edge.orientation == "v"
    assert abs(edge.length - 10) < 1e-6

    far = barndominium("t").add_room("c", T.KITCHEN, x=30, y=0, width=5, length=5).rooms[0]
    assert shared_edge(a, far) is None


def test_overlap_is_an_error():
    plan = (
        barndominium("Overlap")
        .envelope(width=30, length=20)
        .add_room("a", T.LIVING, x=0, y=0, width=20, length=20)
        .add_room("b", T.BEDROOM, x=10, y=0, width=15, length=12)
        .entrance("a", D.SOUTH, width=3, offset=2)
        .add_window("b", D.SOUTH, width=4, offset=2)
        .connect("a", "b")
    )
    report = validate(plan)
    assert any(i.code == "OVERLAP" for i in report.errors)


def test_out_of_bounds_is_an_error():
    plan = (
        barndominium("OOB")
        .envelope(width=20, length=20)
        .add_room("a", T.LIVING, x=0, y=0, width=30, length=10)
        .entrance("a", D.SOUTH, width=3, offset=2)
    )
    report = validate(plan)
    assert any(i.code == "OUT_OF_BOUNDS" for i in report.errors)


def test_bedroom_without_egress_is_an_error():
    plan = (
        barndominium("NoEgress")
        .envelope(width=30, length=20)
        .add_room("living", T.LIVING, x=0, y=0, width=15, length=20)
        .add_room("bedroom", T.BEDROOM, x=15, y=0, width=15, length=20)
        .connect("living", "bedroom")
        .entrance("living", D.SOUTH, width=3, offset=2)
        # bedroom has no window or exterior door
    )
    report = validate(plan)
    assert any(i.code == "BEDROOM_EGRESS" for i in report.errors)


def test_unreachable_room_is_an_error():
    plan = (
        barndominium("Stranded")
        .envelope(width=40, length=20)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=20)
        .add_room("office", T.OFFICE, x=20, y=0, width=20, length=20)
        .entrance("living", D.SOUTH, width=3, offset=2)
        .add_window("office", D.EAST, width=6, offset=4)
        # no interior door connecting office to living
    )
    report = validate(plan)
    assert any(i.code == "NO_ACCESS" and i.room == "office" for i in report.errors)


def test_small_bedroom_is_an_error():
    plan = (
        barndominium("Tiny")
        .envelope(width=30, length=20)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=20)
        .add_room("bedroom", T.BEDROOM, x=24, y=0, width=6, length=6)
        .connect("living", "bedroom")
        .entrance("living", D.SOUTH, width=3, offset=2)
        .add_window("bedroom", D.EAST, width=3, offset=1)
    )
    report = validate(plan)
    assert any(i.code in ("BEDROOM_AREA", "BEDROOM_DIM") for i in report.errors)


def test_render_produces_svg():
    svg = render_svg(_example())
    assert svg.startswith("<svg")
    assert svg.strip().endswith("</svg>")
    assert "Cedar Ridge" in svg
    assert "PROJECT SUMMARY" in svg
    # one <rect> per room (plus envelope, panel, legend swatches, background)
    assert svg.count("<rect") >= len(_example().rooms)
