"""Phase 16 — Site plan v2: driveway, walkway, well, septic, service drops, grade.

Grammar + emit round-trip + fmt idempotency for the six new statements, the four
new rules (WELL_SEPTIC_CLEAR, DRIVE_DOOR, SEPTIC_SETBACK, PORCH_GUARD), the site
cost lines, the site SVG symbols + actual-setback dimensions, the packet clearance
table, and the playground/LSP statement-list integration.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl, render_site_svg
from barndsl.cost import estimate_cost
from barndsl.diagnostics import REGISTRY, explain
from barndsl.fmt import format_source
from barndsl.packet import build_packet


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# A compact valid plan on a lot; site-feature lines slot in via {extra}.
_SRC = """\
plan "Lot"
envelope 40 x 30
ceiling 9
site 120 x 90
building at 40,30
{extra}
room living: living at 0,0 size 40 x 30
entry living south width 3 offset 10
window living west width 10 offset 8
"""


def _compile(extra: str):
    return compile_source(_SRC.format(extra=extra))


# --- parsing -----------------------------------------------------------------


def test_drive_parses_with_surface_and_default():
    ss = _compile("drive at 80,10 size 20 x 40 concrete").plan.site_spec
    d = ss.drives[0]
    assert (d.x, d.y, d.width, d.length, d.surface) == (80.0, 10.0, 20.0, 40.0, "concrete")
    ss2 = _compile("drive at 80,10 size 20 x 40").plan.site_spec
    assert ss2.drives[0].surface == "gravel"  # default


def test_drive_bad_surface_is_a_bad_option():
    r = _compile("drive at 80,10 size 20 x 40 cobblestone")
    assert "BAD_OPTION" in _codes(r, "error")


def test_walk_parses_room_and_default_width():
    ss = _compile("drive at 80,10 size 20 x 40\nwalk from living to drive").plan.site_spec
    assert ss.walks[0].room == "living" and ss.walks[0].width == 4.0
    ss2 = _compile("drive at 80,10 size 20 x 40\nwalk from living to drive width 5").plan.site_spec
    assert ss2.walks[0].width == 5.0


def test_well_and_septic_parse():
    ss = _compile("well at 10,80\nseptic at 90,60 field 26 x 30").plan.site_spec
    assert (ss.wells[0].x, ss.wells[0].y) == (10.0, 80.0)
    sp = ss.septics[0]
    assert (sp.x, sp.y, sp.field_width, sp.field_length) == (90.0, 60.0, 26.0, 30.0)
    assert sp.has_field
    ss2 = _compile("septic at 90,60").plan.site_spec
    assert not ss2.septics[0].has_field


def test_service_parses_utility_and_side():
    from barndsl.elements import Direction

    ss = _compile("service electric from N\nservice water from W").plan.site_spec
    assert (ss.services[0].utility, ss.services[0].side) == ("electric", Direction.NORTH)
    assert (ss.services[1].utility, ss.services[1].side) == ("water", Direction.WEST)


def test_service_bad_utility_and_side():
    assert "BAD_OPTION" in _codes(_compile("service sewer from N"), "error")
    assert "BAD_WALL" in _codes(_compile("service gas from up"), "error")


def test_grade_parses_ft_in():
    plan = _compile("grade 2-8").plan
    assert abs(plan.grade - (2 + 8 / 12)) < 1e-9


def test_site_feature_without_a_site_is_site_required():
    src = (
        'plan "P"\nenvelope 40 x 30\nceiling 9\n'
        "well at 10,10\n"
        "room living: living at 0,0 size 40 x 30\n"
        "entry living south width 3 offset 10\n"
        "window living west width 10 offset 8\n"
    )
    assert "SITE_REQUIRED" in _codes(compile_source(src), "error")


def test_grade_needs_no_site():
    src = (
        'plan "P"\nenvelope 40 x 30\nceiling 9\ngrade 2-8\n'
        "room living: living at 0,0 size 40 x 30\n"
        "entry living south width 3 offset 10\n"
        "window living west width 10 offset 8\n"
    )
    r = compile_source(src)
    assert "SITE_REQUIRED" not in _codes(r, "error")
    assert abs(r.plan.grade - (2 + 8 / 12)) < 1e-9


# --- emit round-trip ---------------------------------------------------------


def test_all_new_statements_round_trip_to_a_fixed_point():
    extra = (
        "grade 2-8\n"
        "drive at 80,10 size 20 x 40 concrete\n"
        "drive at 5,5 size 12 x 20\n"
        "walk from living to drive width 5\n"
        "well at 10,80\n"
        "septic at 90,60 field 26 x 30\n"
        "service electric from N\n"
        "service water from W"
    )
    r = _compile(extra)
    src2 = emit_dsl(r.plan)
    for needle in (
        "drive at 80,10 size 20 x 40 concrete",
        "drive at 5,5 size 12 x 20",  # gravel default is implicit
        "walk from living to drive width 5",
        "well at 10,80",
        "septic at 90,60 field 26 x 30",
        "service electric from N",
        "service water from W",
    ):
        assert needle in src2, src2
    again = compile_source(src2, name=r.plan.name)
    assert emit_dsl(again.plan) == src2  # a fixed point


def test_default_gravel_and_default_walk_width_are_implicit_on_emit():
    r = _compile("drive at 5,5 size 12 x 20 gravel\nwalk from living to drive width 4")
    src2 = emit_dsl(r.plan)
    assert "drive at 5,5 size 12 x 20\n" in src2  # no trailing "gravel"
    assert "walk from living to drive\n" in src2  # no trailing "width 4"


def test_no_site_features_emit_nothing():
    src2 = emit_dsl(_compile("").plan)
    for kw in ("drive", "walk", "well", "septic", "service", "grade"):
        assert kw not in src2


# --- fmt idempotency ---------------------------------------------------------


def test_fmt_idempotent_on_new_statements():
    src = (
        "DRIVE at 78,8 size 22 x 46 concrete\n"
        "walk   from  living to drive  width 4\n"
        "well at 10,80\n"
        "septic at 90,55 field 26 x 25\n"
        "service electric from N\n"
        "grade 2-8\n"
    )
    once = format_source(src)
    assert once == format_source(once)  # idempotent
    assert once.splitlines()[0] == "drive at 78,8 size 22 x 46 concrete"  # head lowercased
    assert "grade 2.66667" in once  # ft-in canonicalised to decimal feet


# --- rules -------------------------------------------------------------------


def test_well_septic_clear_fires_when_too_close_and_silent_when_far():
    close = _compile("well at 60,50\nseptic at 62,52")
    assert "WELL_SEPTIC_CLEAR" in _codes(close, "warning")
    far = _compile("well at 5,5\nseptic at 118,88")
    assert "WELL_SEPTIC_CLEAR" not in _codes(far, "warning")


def test_drive_door_fires_without_a_walk_and_silent_with_one():
    no_walk = _compile("drive at 5,5 size 12 x 20")
    assert "DRIVE_DOOR" in _codes(no_walk, "info")
    with_walk = _compile("drive at 5,5 size 12 x 20\nwalk from living to drive")
    assert "DRIVE_DOOR" not in _codes(with_walk, "info")


def test_drive_door_silent_when_a_drive_edge_reaches_a_door():
    # The entry is at world (11.5, 0) -> lot (51.5, 30); a drive touching that
    # point needs no walk.
    near = _compile("drive at 45,20 size 20 x 12")  # lot x45..65, y20..32 -> reaches (51.5,30)
    assert "DRIVE_DOOR" not in _codes(near, "info")


def test_septic_setback_fires_inside_a_band_and_silent_outside():
    src = _SRC.replace("building at 40,30", "setback front 25 side 10 rear 20\nbuilding at 40,30")
    inside = compile_source(src.format(extra="septic at 50,5"))  # y 5..13 in front band
    assert "SEPTIC_SETBACK" in {d.code for d in inside.infos}
    outside = compile_source(src.format(extra="septic at 50,40"))  # clear of every band
    assert "SEPTIC_SETBACK" not in {d.code for d in outside.infos}


def test_porch_guard_fires_above_30_in_and_silent_at_exactly_30_in():
    base = (
        'plan "P"\nenvelope 40 x 30\nceiling 9\ngrade {g}\n'
        "room living: living at 0,0 size 40 x 30\n"
        "porch p at 0,-6 size 40 x 6 covered\n"
        "entry living south width 3 offset 10\n"
        "window living west width 10 offset 8\n"
    )
    assert "PORCH_GUARD" in _codes(compile_source(base.format(g="3")), "warning")  # 36 in
    assert "PORCH_GUARD" in _codes(compile_source(base.format(g="2-7")), "warning")  # 31 in
    assert "PORCH_GUARD" not in _codes(compile_source(base.format(g="2.5")), "warning")  # 30 in
    assert "PORCH_GUARD" not in _codes(compile_source(base.format(g="2-6")), "warning")  # 30 in


def test_porch_guard_fires_once_per_porch():
    src = (
        'plan "P"\nenvelope 40 x 30\nceiling 9\ngrade 3\n'
        "room living: living at 0,0 size 40 x 30\n"
        "porch p1 at 0,-6 size 20 x 6 covered\n"
        "porch p2 at 20,-6 size 20 x 6 covered\n"
        "entry living south width 3 offset 10\n"
        "window living west width 10 offset 8\n"
    )
    guards = [d for d in compile_source(src).warnings if d.code == "PORCH_GUARD"]
    assert len(guards) == 2


def test_walk_naming_unknown_room_is_site_ref():
    r = _compile("drive at 5,5 size 12 x 20\nwalk from garage to drive")
    assert "SITE_REF" in _codes(r, "error")


def test_site_rules_silent_with_no_features():
    # A plain plan (no site features, no grade) gets none of the new diagnostics.
    r = _compile("")
    new = {"WELL_SEPTIC_CLEAR", "DRIVE_DOOR", "SEPTIC_SETBACK", "PORCH_GUARD",
           "SITE_REQUIRED", "SITE_REF"}
    assert not (new & {d.code for d in r.diagnostics})


# --- cost --------------------------------------------------------------------


def test_cost_site_lines_are_hand_computable():
    r = _compile(
        "drive at 60,10 size 20 x 40 concrete\n"
        "drive at 5,5 size 10 x 10\n"  # gravel
        "walk from living to drive\n"
        "well at 10,80\n"
        "septic at 90,60 field 26 x 25"
    )
    est = estimate_cost(r)
    site = {ln["item"]: ln for ln in est["assemblies"] if ln["group"] == "Site work"}
    # Concrete drive: 20*40 = 800 sqft * $8 = $6,400.
    assert site["Driveway (concrete)"]["quantity"] == 800.0
    assert site["Driveway (concrete)"]["cost"] == 6400.0
    # Gravel drive: 10*10 = 100 sqft * $3 = $300.
    assert site["Driveway (gravel)"]["cost"] == 300.0
    # Well ($12k) + septic ($15k) allowances, one each.
    assert site["Well allowance"]["cost"] == 12000.0
    assert site["Septic allowance"]["cost"] == 15000.0
    # Walk area matches the resolved path length × width.
    wk, _p0, _p1, wlen = r.plan.walk_paths()[0]
    assert abs(site["Walkway"]["quantity"] - wlen * wk.width) < 0.01


def test_cost_site_keys_are_overridable():
    r = _compile("well at 10,80")
    est = estimate_cost(r, overrides={"well_allowance": 20000.0})
    well = next(ln for ln in est["assemblies"] if ln["item"] == "Well allowance")
    assert well["cost"] == 20000.0


def test_exclusions_no_longer_say_site_work():
    est = estimate_cost(_compile("well at 10,80"))
    assert "site work" not in est["exclusions"]
    assert "permits" in est["exclusions"]


# --- site render -------------------------------------------------------------


def test_site_svg_draws_every_symbol():
    svg = render_site_svg(_compile(
        "grade 2-8\n"
        "drive at 78,8 size 22 x 40 concrete\n"
        "walk from living to drive\n"
        "well at 10,80\n"
        "septic at 90,55 field 26 x 25\n"
        "service electric from N"
    ).plan)
    assert "DRIVE (concrete)" in svg
    assert ">W<" in svg  # the well glyph
    assert "DRAIN FIELD" in svg
    assert "electric" in svg  # the service arrow label
    assert "Legend" in svg
    assert "above grade" in svg


def test_site_svg_dimensions_the_actual_setback_distances():
    # envelope 40x30 at building 35,20 on a 120x90 lot -> west yard 35 ft, east 45 ft.
    r = compile_source(_SRC.format(extra="").replace("building at 40,30", "building at 35,20"))
    svg = render_site_svg(r.plan)
    assert "35′" in svg  # west-side actual clearance
    assert "45′" in svg  # east-side actual clearance


# --- packet ------------------------------------------------------------------


def test_packet_has_a_clearance_table_and_site_notes():
    src = _SRC.replace("building at 40,30", "setback front 25 side 10 rear 20\nbuilding at 40,30")
    html = build_packet(compile_source(src.format(
        extra="drive at 78,8 size 22 x 40\nwell at 10,80\nseptic at 90,55 field 20 x 20"
    )))
    assert "Yard clearances" in html
    assert "Site notes" in html
    assert "Driveway: 22" in html
    assert "✓" in html  # every side clears its setback here


def test_packet_clearance_marks_a_short_yard():
    # Shove the building west so the west yard (5 ft) is short of the 10 ft side.
    src = _SRC.replace("building at 40,30", "setback front 25 side 10 rear 20\nbuilding at 5,30")
    html = build_packet(compile_source(src.format(extra="")))
    assert "✗ short" in html


# --- registry + integration --------------------------------------------------


def test_new_codes_are_registered():
    for code in ("SITE_REQUIRED", "SITE_REF", "WELL_SEPTIC_CLEAR", "DRIVE_DOOR",
                 "SEPTIC_SETBACK", "PORCH_GUARD"):
        assert code in REGISTRY
        assert "Unknown" not in explain(code)


def test_new_statements_are_in_the_shared_statement_list():
    from barndsl.playground import _highlight_tokens
    from barndsl.lsp import _STATEMENT_DOCS

    stmts = set(_highlight_tokens()["statements"])
    for kw in ("drive", "walk", "well", "septic", "service", "grade"):
        assert kw in stmts  # playground autocomplete / highlighter
        assert kw in _STATEMENT_DOCS  # LSP hover docs (from DSL_REFERENCE)


def test_dsl_reference_documents_the_new_statements():
    from barndsl import DSL_REFERENCE

    for needle in ("drive at", "walk from", "well at", "septic at", "service ", "grade <ft>"):
        assert needle in DSL_REFERENCE


# --- builder API -------------------------------------------------------------


def test_builder_site_features_round_trip():
    plan = (
        barndominium("B").envelope(40, 30).ceiling(9)
        .site(120, 90).building(40, 30).set_grade(2 + 8 / 12)
        .drive(80, 10, 20, 40, "concrete")
        .well(10, 80)
        .septic(90, 60, 26, 30)
        .service("electric", "N")
        .add_room("living", "living", x=0, y=0, width=40, length=30)
        .walk("living")
    )
    ss = plan.site_spec
    assert ss.drives[0].surface == "concrete"
    assert ss.wells and ss.septics and ss.services and ss.walks
    assert "drive at 80,10 size 20 x 40 concrete" in emit_dsl(plan)
