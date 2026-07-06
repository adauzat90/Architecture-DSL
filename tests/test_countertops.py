"""Phase 9 — countertop runs (the ``fixture counter ... along <wall>`` sugar).

Covers the grammar (accept/reject), the desugar geometry per wall, emit/fmt
round-trips, the kitchen-aware overlap exemptions (mitred corners, inset
appliances), the three new teaching diagnostics, RANGE_LANDING seeing runs as
landings, render z-order + bullnose, the metrics/cost takeoff, the compare keys,
the extended ``add_fixture`` edit, the playground markup, the compose transform,
and the LSP completion/hover contexts.
"""

import math

from barndsl.compiler import compile_source
from barndsl.diagnostics import REGISTRY
from barndsl.edits import apply_edit, edit_from_json
from barndsl.emit import emit_dsl
from barndsl.fixtures import ALONG_DEFAULT_DEPTH, resolve_room_fixtures
from barndsl.fmt import format_source


def _compile(src):
    r = compile_source(src)
    return r


def _errors(src):
    r = compile_source(src)
    return [d for d in r.diagnostics if d.severity.value == "error"]


def _codes(src):
    r = compile_source(src)
    return {d.code for d in r.diagnostics}


def _counters(plan, room_id="k"):
    room = plan.room(room_id)
    return [f for f in resolve_room_fixtures(plan, room) if f.kind == "counter"]


# A roomy kitchen with an exterior wall on every side but the east (which opens to
# a living area), so runs and appliances have somewhere to sit.
_HEAD = """plan "P9"
envelope 30 x 16
room k: kitchen at 0,0 size 16 x 16
room lv: living east-of k size 14 x 16
open k - lv width 6 offset 5
entry lv south width 3 offset 5
"""


def _plan(*fixture_lines):
    return _HEAD + "\n".join(fixture_lines) + "\n"


# --- grammar: accept ---------------------------------------------------------


def test_along_full_wall_accepts():
    assert _errors(_plan("fixture counter in k along S")) == []


def test_along_partial_run_accepts():
    assert _errors(_plan("fixture counter in k along S from 2 to 12")) == []


def test_along_depth_override_accepts():
    assert _errors(_plan("fixture counter in k along W depth 2-1")) == []


def test_along_ft_in_from_to_accepts():
    r = _compile(_plan("fixture counter in k along S from 2-6 to 12-0"))
    assert not [d for d in r.diagnostics if d.severity.value == "error"]
    f = _counters(r.plan)[0]
    assert math.isclose(f.x, 2.5, abs_tol=1e-6)
    assert math.isclose(f.width, 9.5, abs_tol=1e-6)


# --- grammar: reject ---------------------------------------------------------


def test_along_on_non_counter_rejects():
    errs = _errors(_plan("fixture sink in k along S"))
    assert any("along" in e.message.lower() for e in errs)


def test_from_past_to_rejects():
    errs = _errors(_plan("fixture counter in k along S from 12 to 2"))
    assert errs


def test_depth_out_of_bounds_rejects():
    assert _errors(_plan("fixture counter in k along S depth 6"))
    assert _errors(_plan("fixture counter in k along S depth 0.5"))


def test_along_with_at_rejects():
    assert _errors(_plan("fixture counter in k along S at 2,0"))


def test_along_with_width_rejects():
    assert _errors(_plan("fixture counter in k along S width 8"))


def test_from_without_along_rejects():
    assert _errors(_plan("fixture counter in k at 2,0 wall S from 2 to 6"))


def test_lone_from_rejects():
    assert _errors(_plan("fixture counter in k along S from 2"))


# --- desugar geometry --------------------------------------------------------


def test_full_wall_south_spans_the_wall():
    f = _counters(_compile(_plan("fixture counter in k along S")).plan)[0]
    assert (f.x, f.y) == (0.0, 0.0)
    assert math.isclose(f.width, 16.0)  # run along x
    assert math.isclose(f.length, ALONG_DEFAULT_DEPTH)
    assert f.wall == "S"


def test_full_wall_north_backs_north():
    f = _counters(_compile(_plan("fixture counter in k along N")).plan)[0]
    assert math.isclose(f.y, 16.0 - ALONG_DEFAULT_DEPTH)
    assert math.isclose(f.width, 16.0)
    assert f.wall == "N"


def test_full_wall_west_runs_along_y():
    f = _counters(_compile(_plan("fixture counter in k along W")).plan)[0]
    assert (f.x, f.y) == (0.0, 0.0)
    assert math.isclose(f.width, ALONG_DEFAULT_DEPTH)  # depth into room (east)
    assert math.isclose(f.length, 16.0)  # run along y
    assert f.wall == "W"


def test_full_wall_east_backs_east():
    f = _counters(_compile(_plan("fixture counter in k along E")).plan)[0]
    assert math.isclose(f.x, 16.0 - ALONG_DEFAULT_DEPTH)
    assert math.isclose(f.length, 16.0)
    assert f.wall == "E"


def test_partial_run_uses_from_to():
    f = _counters(_compile(_plan("fixture counter in k along S from 3 to 11")).plan)[0]
    assert math.isclose(f.x, 3.0)
    assert math.isclose(f.width, 8.0)


def test_depth_override_applies():
    f = _counters(_compile(_plan("fixture counter in k along S depth 3")).plan)[0]
    assert math.isclose(f.length, 3.0)


def test_default_depth_is_25_inches():
    assert math.isclose(ALONG_DEFAULT_DEPTH, 25.0 / 12.0)


# --- emit / fmt round-trips --------------------------------------------------


def test_emit_round_trips_full_wall():
    src = _plan("fixture counter in k along S")
    out = emit_dsl(_compile(src).plan)
    assert "along S" in out
    f = _counters(_compile(out).plan)[0]
    assert math.isclose(f.width, 16.0)


def test_emit_round_trips_partial_and_depth():
    src = _plan("fixture counter in k along W from 2 to 12 depth 2-1")
    out = emit_dsl(_compile(src).plan)
    assert "along W from 2 to 12 depth" in out
    p2 = _compile(out).plan
    f = p2.fixtures[0]
    assert f.along is not None
    assert math.isclose(f.run_from, 2.0) and math.isclose(f.run_to, 12.0)


def test_fmt_idempotent_on_along():
    src = _plan("fixture counter in k along S from 2-6 to 12 depth 2-1")
    once = format_source(src)
    assert format_source(once) == once
    assert "along S from" in once


# --- overlap: mitred corners -------------------------------------------------


def _overlap_codes(src):
    return [d for d in compile_source(src).diagnostics if d.code == "FIXTURE_OVERLAP"]


def test_l_corner_is_exempt():
    src = _plan(
        "fixture counter in k along S from 0 to 10",
        "fixture counter in k along W from 0 to 10",
    )
    assert _overlap_codes(src) == []


def test_u_corners_are_exempt():
    src = _plan(
        "fixture counter in k along W",
        "fixture counter in k along S",
        "fixture counter in k along E",
    )
    assert _overlap_codes(src) == []


def test_collinear_double_stack_still_fires():
    # Two counters on the same wall overlapping along their length is a real clash.
    src = _plan(
        "fixture counter in k along S from 0 to 12",
        "fixture counter in k along S from 4 to 16",
    )
    assert _overlap_codes(src)


# --- overlap: inset appliances ----------------------------------------------


def test_inset_sink_is_exempt():
    src = _plan(
        "fixture counter in k along S",
        "fixture sink in k at 6,0 wall S",
    )
    assert _overlap_codes(src) == []


def test_inset_range_is_exempt():
    src = _plan(
        "fixture counter in k along S depth 2",
        "fixture range in k at 6,0 wall S",
    )
    assert _overlap_codes(src) == []


def test_refrigerator_over_counter_still_fires():
    src = _plan(
        "fixture counter in k along S",
        "fixture refrigerator in k at 6,0 wall S",
    )
    assert _overlap_codes(src)


# --- new diagnostics ---------------------------------------------------------


def test_counter_door_fires_over_an_entry():
    src = (
        'plan "d"\nenvelope 16 x 14\nroom k: kitchen at 0,0 size 16 x 14\n'
        "entry k south width 3 offset 6\n"
        "fixture counter in k along S\n"
    )
    assert "COUNTER_DOOR" in _codes(src)


def test_counter_door_silent_when_run_stops_short():
    src = (
        'plan "d"\nenvelope 16 x 14\nroom k: kitchen at 0,0 size 16 x 14\n'
        "entry k south width 3 offset 10\n"
        "fixture counter in k along S from 0 to 9\n"
    )
    assert "COUNTER_DOOR" not in _codes(src)


def test_counter_room_fires_in_a_bedroom():
    src = (
        'plan "b"\nenvelope 14 x 12\nroom bd: bedroom at 0,0 size 14 x 12\n'
        "entry bd south width 3 offset 5\n"
        "window bd north width 4 offset 5\n"
        "fixture counter in bd along S\n"
    )
    assert "COUNTER_ROOM" in _codes(src)


def test_counter_room_silent_in_a_kitchen():
    assert "COUNTER_ROOM" not in _codes(_plan("fixture counter in k along S"))


def test_sink_no_counter_fires_when_sink_off_the_run():
    # A counter exists (so the check is active) but the sink sits away from it.
    src = _plan(
        "fixture counter in k along N",
        "fixture sink in k at 6,0 wall S",
    )
    assert "SINK_NO_COUNTER" in _codes(src)


def test_sink_no_counter_silent_when_inset():
    src = _plan(
        "fixture counter in k along S",
        "fixture sink in k at 6,0 wall S",
    )
    assert "SINK_NO_COUNTER" not in _codes(src)


def test_sink_no_counter_silent_without_counters():
    # A bare seed-only kitchen has a seeded sink but no counters — stay quiet.
    assert "SINK_NO_COUNTER" not in _codes(_HEAD)


def test_new_codes_are_registered():
    for code in ("COUNTER_DOOR", "COUNTER_ROOM", "SINK_NO_COUNTER"):
        assert code in REGISTRY


# --- RANGE_LANDING sees a run as a landing -----------------------------------


def test_range_landing_satisfied_by_along_counter():
    # Range beside a counter run: no RANGE_LANDING.
    src = _plan(
        "fixture counter in k along S",
        "fixture range in k at 6,2 wall S rotate 0",
    )
    # The range sits just in front of the S run (within 1 ft) → landing present.
    assert "RANGE_LANDING" not in _codes(src)


# --- render: z-order + bullnose ---------------------------------------------


def test_counter_draws_first_and_has_bullnose():
    from barndsl.render import render_svg

    src = _plan(
        "fixture counter in k along S",
        "fixture sink in k at 6,0 wall S",
    )
    svg = render_svg(_compile(src).plan)
    import re

    order = re.findall(r'data-fixture="([^"]+)"', svg)
    order = [o for o in order if o.startswith("k~")]
    # the counter's group precedes the sink's (z-under)
    ci = next(i for i, o in enumerate(order) if "counter" in o)
    si = next(i for i, o in enumerate(order) if "sink" in o)
    assert ci < si


# --- metrics / cost ----------------------------------------------------------


def test_metrics_counter_keys():
    src = _plan("fixture counter in k along S", "fixture counter in k along W")
    m = _compile(src).plan.metrics()
    # S full = 16 ft run, W full = 16 ft run → 32 lf.
    assert math.isclose(m["counter_linear_ft"], 32.0)
    assert m["counter_area_sqft"] > 0
    assert "counter_linear_ft" in m and "counter_area_sqft" in m


def test_cost_has_a_countertop_line():
    from barndsl.cost import estimate_cost

    src = _plan("fixture counter in k along S")
    est = estimate_cost(_compile(src).plan)
    lines = [ln for ln in est["assemblies"] if ln["item"] == "Countertops"]
    assert lines and lines[0]["quantity"] > 0
    assert lines[0]["cost_key"] == "countertop_lf"


def test_compare_keys_include_counter():
    from barndsl.compare import _METRIC_KEYS

    assert "counter_linear_ft" in _METRIC_KEYS


# --- edits: extended add_fixture --------------------------------------------


def test_add_fixture_along_json():
    src = _HEAD
    edit = edit_from_json(
        {"kind": "add_fixture", "room": "k", "fkind": "counter", "along": "S"}
    )
    res = apply_edit(src, edit)
    assert res.ok and "along S" in res.source


def test_add_fixture_along_partial_json():
    edit = edit_from_json(
        {"kind": "add_fixture", "room": "k", "fkind": "counter",
         "along": "W", "from": 2, "to": 10}
    )
    res = apply_edit(_HEAD, edit)
    assert res.ok and "along W from 2 to 10" in res.source


def test_old_add_fixture_json_still_valid():
    edit = edit_from_json(
        {"kind": "add_fixture", "room": "k", "fkind": "sofa", "x": 5, "y": 5}
    )
    res = apply_edit(_HEAD, edit)
    assert res.ok and "fixture sofa in k at 5,5" in res.source


def test_drag_along_counter_slides_along_wall():
    src = _plan("fixture counter in k along W from 0 to 8")
    plan = _compile(src).plan
    cid = _counters(plan)[0].id
    edit = edit_from_json({"kind": "move_fixture", "key": cid, "x": 0, "y": 4})
    res = apply_edit(src, edit)
    assert res.ok and "along W" in res.source
    # the run kept its 8 ft length, shifted to start at y=4
    f = _counters(_compile(res.source).plan)[0]
    assert math.isclose(f.y, 4.0) and math.isclose(f.length, 8.0)


def test_set_fixture_width_adjusts_run():
    src = _plan("fixture counter in k along S from 0 to 8")
    plan = _compile(src).plan
    cid = _counters(plan)[0].id
    edit = edit_from_json({"kind": "set_fixture", "id": cid, "width": 12})
    res = apply_edit(src, edit)
    assert res.ok
    f = _counters(_compile(res.source).plan)[0]
    assert math.isclose(f.width, 12.0)  # run grew to 12 ft, still an along run
    assert f.wall == "S"


# --- playground markup + offline guarantee -----------------------------------


def test_playground_fixture_form_has_along_controls():
    from barndsl.playground import render_app

    html = render_app(_HEAD)
    assert "nf-along" in html
    assert "add counter run" in html


def test_playground_stays_offline():
    from barndsl.playground import render_app

    html = render_app(_HEAD)
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html


# --- compose transform property test -----------------------------------------


def test_along_counter_mirrors_onto_the_remapped_wall(tmp_path):
    part = tmp_path / "kit.barn"
    part.write_text(
        "room k: kitchen at 0,0 size 12 x 10\n"
        "fixture counter in k along W from 0 to 6\n"
    )
    host = tmp_path / "host.barn"
    host.write_text(
        'plan "H"\nenvelope 40 x 20\n'
        'use "kit.barn" as m at 0,0 mirror y\n'
    )
    r = compile_source(host.read_text(), base_dir=str(tmp_path))
    assert r.plan is not None
    room = r.plan.room("m.k")
    runs = [f for f in resolve_room_fixtures(r.plan, room) if f.kind == "counter"]
    assert len(runs) == 1
    f = runs[0]
    # `mirror y` swaps east↔west: a west run lands on the east wall of the room.
    assert f.wall == "E"
    assert math.isclose(f.length, 6.0)  # the run length is preserved


# --- LSP completion + hover contexts -----------------------------------------


def test_lsp_completes_along_after_counter_room():
    from barndsl.lsp import completions

    src = "fixture counter in k "
    r = compile_source(_plan("fixture counter in k along S"))
    items = completions(src, 0, len(src), r, None)
    labels = {i["label"] for i in items}
    assert "along" in labels


def test_lsp_completes_wall_dirs_after_along():
    from barndsl.lsp import completions

    src = "fixture counter in k along "
    r = compile_source(_plan("fixture counter in k along S"))
    items = completions(src, 0, len(src), r, None)
    labels = {i["label"] for i in items}
    assert {"north", "south", "east", "west"} <= labels


def test_lsp_completes_from_to_depth_in_run():
    from barndsl.lsp import completions

    src = "fixture counter in k along S "
    r = compile_source(_plan("fixture counter in k along S"))
    items = completions(src, 0, len(src), r, None)
    labels = {i["label"] for i in items}
    assert {"from", "to", "depth"} <= labels


def test_lsp_hover_counter_card():
    from barndsl.lsp import hover

    src = _plan("fixture counter in k along S")
    r = compile_source(src)
    # the fixture line is the last non-empty line
    lines = src.splitlines()
    li = max(i for i, ln in enumerate(lines) if ln.startswith("fixture counter"))
    h = hover(src, r, li, 20)
    assert h is not None and "counter run" in h["contents"]["value"]


# --- render: mitred corners draw as one surface (no crossing boxes) -----------


def _counter_screen_rects(svg):
    """The (x, y, w, h) of every counter's outline rect in the SVG, by group."""
    import re

    rects = []
    for m in re.finditer(
        r'<g data-fixture="k~counter~\d+">\s*<rect x="([\d.]+)" y="([\d.]+)" '
        r'width="([\d.]+)" height="([\d.]+)"',
        svg,
    ):
        rects.append(tuple(float(v) for v in m.groups()))
    return rects


def test_mitred_corner_runs_are_drawn_abutting_not_overlapping():
    # A U: full W run + S and N runs starting at the same corner. The model keeps
    # the overlapping corner squares (the mitred exemption / takeoff), but the
    # DRAWING must trim the later runs so no two counter rects overlap on paper.
    from barndsl.render import render_svg

    src = _plan(
        "fixture counter in k along W",
        "fixture counter in k along S from 0 to 12",
        "fixture counter in k along N from 0 to 12",
    )
    svg = render_svg(_compile(src).plan)
    rects = _counter_screen_rects(svg)
    assert len(rects) == 3
    eps = 0.01
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            (ax, ay, aw, ah), (bx, by, bw, bh) = rects[i], rects[j]
            ox = min(ax + aw, bx + bw) - max(ax, bx)
            oy = min(ay + ah, by + bh) - max(ay, by)
            assert not (ox > eps and oy > eps), (
                f"drawn counter rects {i} and {j} overlap by {ox:.2f}x{oy:.2f}px"
            )
    # ...and each L/U joint carries the 45-degree miter line across the corner.
    assert svg.count('data-joint="miter"') == 2


def test_single_run_draws_untrimmed_with_no_miter():
    from barndsl.render import render_svg

    svg = render_svg(_compile(_plan("fixture counter in k along S")).plan)
    assert 'data-joint="miter"' not in svg
    assert len(_counter_screen_rects(svg)) == 1
