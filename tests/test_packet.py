"""`barndsl packet`: one self-contained HTML permit-sketch deliverable."""

from __future__ import annotations

from barndsl import build_packet, compile_source
from barndsl.cli import main

SRC = """\
plan "Packet Fixture"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 20 x 18
room kitchen: kitchen at 20,0 size 20 x 18
room bed: bedroom at 0,18 size 20 x 12
room bath: bathroom at 20,18 size 12 x 12
door living - kitchen width 3
door living - bed width 2.67
door kitchen - bath width 2.5
entry living south width 3 offset 8
window living south width 6 offset 2
window bed north width 4 offset 4
window kitchen east width 4 offset 4
"""

# A two-level plan (loft over the ground floor) — the render stacks one block
# per level, so the packet's floor-plan SVG must show both.
SRC_MULTI = """\
plan "Two Story"
envelope 30 x 24
ceiling 9
room great: living at 0,0 size 30 x 24
room loft: loft at 0,0 size 30 x 12 level 1
stair main from 0 to 1 at 0,0 size 4 x 10
window great south width 6 offset 4
window loft south width 6 offset 4 level 1
entry great south width 3 offset 12
"""


def _html():
    return build_packet(compile_source(SRC))


def test_cover_page_has_name_program_metrics_and_score():
    html = _html()
    assert "Packet Fixture" in html
    assert "Permit-sketch packet" in html
    assert "Design score" in html
    assert "/ 100" in html
    # Key metrics table carries the takeoff.
    assert "Footprint" in html
    assert "Estimated cost" in html


def test_floor_plan_section_inlines_the_dimensioned_svg():
    html = _html()
    assert "<h2>Floor Plan</h2>" in html
    assert "<svg" in html and "</svg>" in html
    # The dimensioned render prints per-room W x L; the × marker proves it.
    assert "×" in html


def test_multi_level_plan_renders_a_block_per_level():
    html = build_packet(compile_source(SRC_MULTI))
    # render_svg labels each stacked level block.
    assert "LEVEL 0" in html
    assert "LEVEL 1" in html


def test_schedules_section_has_rows():
    html = _html()
    assert "Room Schedule" in html
    assert "Door Schedule" in html
    assert "Window Schedule" in html
    # A room mark and a door mark from the schedules.
    assert "living" in html
    assert "W1" in html and "D1" in html


def test_cost_section_and_disclaimer():
    html = _html()
    assert "Cost Estimate" in html
    assert "Slab-on-grade" in html
    assert "Estimated total" in html
    assert "budgeting only" in html


def test_diagnostics_appendix_present():
    html = _html()
    assert "Diagnostics Appendix" in html
    assert "error(s)" in html and "warning(s)" in html


def test_packet_is_self_contained_and_paginated():
    html = _html()
    assert html.lstrip().startswith("<!doctype html>")
    # No external requests: nothing fetches a remote asset. (The inline SVG's
    # xmlns="http://www.w3.org/2000/svg" is a namespace name, not a request.)
    assert "src=" not in html
    assert "<link" not in html
    assert 'href="http' not in html
    assert "@import" not in html
    # Print pagination between sections.
    assert "page-break-after" in html


def test_multiplier_and_overrides_flow_into_the_cost_section():
    html = build_packet(compile_source(SRC), costs={"slab_sqft": 999.0}, multiplier=2.0)
    assert "regional multiplier x2" in html
    assert "$1,998" in html  # effective rate: 999 override x 2 multiplier


def test_build_packet_rejects_planless_result():
    result = compile_source("not a plan at all\n")
    try:
        build_packet(result)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on a plan-less compile")


def test_cli_packet_writes_html_and_exit_codes(tmp_path):
    good = tmp_path / "good.barn"
    good.write_text(SRC, encoding="utf-8")
    out = tmp_path / "packet.html"
    # No cairosvg needed — the packet is always HTML.
    assert main(["packet", str(good), "-o", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert "<svg" in html
    assert "Packet Fixture" in html
    assert "Cost Estimate" in html

    # Uncompilable file → exit 2.
    bad = tmp_path / "bad.barn"
    bad.write_text("not a plan at all\n", encoding="utf-8")
    assert main(["packet", str(bad), "-o", str(tmp_path / "x.html")]) == 2

    # Missing file → exit 2.
    assert main(["packet", str(tmp_path / "nope.barn")]) == 2

    # A partial recovery with ERRORS must not ship as a client deliverable.
    partial = tmp_path / "partial.barn"
    partial.write_text(SRC + "window bogus north width 4\n", encoding="utf-8")
    assert main(["packet", str(partial), "-o", str(tmp_path / "y.html")]) == 2
    assert not (tmp_path / "y.html").exists()
