"""`barndsl cost`: assembly cost estimate from the compiled-plan takeoff."""

from __future__ import annotations

import json

from barndsl import compile_source, cost_text, estimate_cost
from barndsl.cli import main
from barndsl.cost import DEFAULT_UNIT_COSTS, DISCLAIMER

# A plain 40x30 rectangle: footprint is exactly the envelope (1200 sqft), with a
# kitchen and a bath so plumbing fixtures appear, and a mix of openings.
SRC = """\
plan "Cost Fixture"
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


def _est(**kw):
    return estimate_cost(compile_source(SRC), **kw)


def _line(est, cost_key):
    for ln in est["assemblies"]:
        if ln["cost_key"] == cost_key:
            return ln
    raise AssertionError(f"no assembly line with cost_key {cost_key}")


def test_slab_line_is_footprint_times_unit_cost():
    est = _est()
    slab = _line(est, "slab_sqft")
    # 40x30 rectangle → 1200 sqft footprint, times the default $9/sqft slab.
    assert slab["quantity"] == 1200
    assert slab["unit_cost"] == DEFAULT_UNIT_COSTS["slab_sqft"]
    assert slab["cost"] == 1200 * DEFAULT_UNIT_COSTS["slab_sqft"]
    assert slab["source"]  # every line names its quantity source


def test_every_line_is_quantity_times_unit_cost_and_total_sums():
    est = _est()
    for ln in est["assemblies"]:
        # The printed line reads exactly: shown qty × shown unit cost = shown cost.
        assert abs(ln["cost"] - ln["quantity"] * ln["unit_cost"]) < 1e-6
    assert abs(est["total"]["expected"] - sum(l["cost"] for l in est["assemblies"])) < 1e-6
    # Band is +/-15% around expected (both ends rounded to the cent).
    assert abs(est["total"]["low"] - est["total"]["expected"] * 0.85) < 0.01
    assert abs(est["total"]["high"] - est["total"]["expected"] * 1.15) < 0.01


def test_fixtures_and_openings_are_counted():
    est = _est()
    # One bathroom → toilet+lavatory+tub; kitchen → sink+range+refrigerator.
    assert _line(est, "fixture_toilet")["quantity"] == 1
    assert _line(est, "fixture_tub")["quantity"] == 1
    assert _line(est, "fixture_sink")["quantity"] == 1
    # Windows are now two size-aware lines: a per-window base count and a total
    # glazed-area line. SRC has 3 windows (widths 6, 4, 4; default 3.67 ft glass).
    assert _line(est, "window_each")["quantity"] == 3
    glazed = round((6 + 4 + 4) * (6.67 - 3.0), 2)
    assert _line(est, "window_glazed_sqft")["quantity"] == glazed
    # One 3 ft entry door → width factor 1.0; three interior singles.
    assert _line(est, "door_exterior")["quantity"] == 1
    assert _line(est, "door_interior")["quantity"] == 3


def test_windows_priced_by_glazed_area_not_flat_per_each():
    # A picture window and a small awning of the same kind must not cost the same
    # any more — the size-aware model is the whole point.
    base = """\
plan "Glazing"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 40 x 30
entry living south width 3 offset 8
"""
    big = estimate_cost(compile_source(base + "window living south width 10 offset 2\n"))
    small = estimate_cost(compile_source(base + "window living south width 3 offset 2\n"))
    # Same per-window base (1 each × window_each), different glazing cost.
    assert _line(big, "window_each")["quantity"] == 1
    assert _line(small, "window_each")["quantity"] == 1
    # 10 ft wide vs 3 ft wide, both 3.67 ft of glass → glazing scales with width.
    assert _line(big, "window_glazed_sqft")["quantity"] == round(10 * 3.67, 2)
    assert _line(small, "window_glazed_sqft")["quantity"] == round(3 * 3.67, 2)
    assert _line(big, "window_glazed_sqft")["cost"] > _line(small, "window_glazed_sqft")["cost"]
    # A typical 3×4 window (12 sqft glazed) lands at the old flat casement price:
    # window_each 300 + window_glazed_sqft 40 × 12 = $780.
    assert DEFAULT_UNIT_COSTS["window_each"] + DEFAULT_UNIT_COSTS["window_glazed_sqft"] * 12 == 780


def test_exterior_doors_width_weighted_and_garage_by_linear_ft():
    src = """\
plan "Doors"
envelope 50 x 30
ceiling 9
room shop: shop at 0,0 size 50 x 30
entry shop south width 3 offset 2
entry shop south double width 6 offset 10
door shop south overhead width 16 offset 20
door shop north overhead width 9 offset 2
"""
    est = estimate_cost(compile_source(src))
    # People doors: a 3 ft single (factor 1.0) + a 6 ft pair (factor 2.0) = 3.0
    # standard leaves at the base $1,500 → $4,500.
    doors = _line(est, "door_exterior")
    assert doors["quantity"] == 3.0
    assert doors["cost"] == round(3.0 * DEFAULT_UNIT_COSTS["door_exterior"], 2)
    # Overhead doors by width: 16 + 9 = 25 lf × garage_door_lf.
    garage = _line(est, "garage_door_lf")
    assert garage["quantity"] == 25
    assert garage["cost"] == round(25 * DEFAULT_UNIT_COSTS["garage_door_lf"], 2)
    # A 16 ft overhead door now costs ~1.8× a 9 ft one (was flat/equal before).
    assert 1.7 < (16 * DEFAULT_UNIT_COSTS["garage_door_lf"]) / (9 * DEFAULT_UNIT_COSTS["garage_door_lf"]) < 1.9
    # Continuity: a 9 ft single overhead ≈ the old $1,600 flat price.
    assert abs(9 * DEFAULT_UNIT_COSTS["garage_door_lf"] - 1600) < 30


def test_overrides_replace_only_the_named_unit_cost():
    base = _est()
    over = _est(overrides={"slab_sqft": 100.0})
    assert _line(over, "slab_sqft")["unit_cost"] == 100.0
    assert _line(over, "slab_sqft")["cost"] == 1200 * 100.0
    # An untouched key keeps its default.
    assert _line(over, "roof_sqft")["unit_cost"] == _line(base, "roof_sqft")["unit_cost"]


def test_multiplier_scales_the_whole_sheet():
    one = _est()
    two = _est(multiplier=2.0)
    assert abs(two["total"]["expected"] - 2.0 * one["total"]["expected"]) < 1e-6
    # unit_cost is the EFFECTIVE rate (base x multiplier) so every printed
    # line still reads qty x unit = cost; the multiplier is also carried.
    assert _line(two, "slab_sqft")["unit_cost"] == 2.0 * _line(one, "slab_sqft")["unit_cost"]
    assert _line(two, "slab_sqft")["cost"] == 2.0 * _line(one, "slab_sqft")["cost"]
    assert two["multiplier"] == 2.0


def test_lines_reconcile_at_a_fractional_multiplier():
    # The review's failure case: at x1.5 the shown columns must still multiply
    # out exactly — the unit cost shown is the effective (multiplied) rate.
    est = _est(multiplier=1.5)
    for ln in est["assemblies"]:
        assert abs(ln["cost"] - ln["quantity"] * ln["unit_cost"]) < 1e-6
    slab = _line(est, "slab_sqft")
    assert slab["unit_cost"] == round(DEFAULT_UNIT_COSTS["slab_sqft"] * 1.5, 2)


def test_unknown_override_key_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="slab_sqftt"):
        _est(overrides={"slab_sqftt": 100.0})


def test_interior_double_doors_priced_as_a_pair():
    src = SRC + "door bed - bath double width 5\n"
    est = estimate_cost(compile_source(src))
    assert _line(est, "door_interior_double")["quantity"] == 1
    assert _line(est, "door_interior")["quantity"] == 3  # singles unchanged
    assert (
        _line(est, "door_interior_double")["unit_cost"]
        == DEFAULT_UNIT_COSTS["door_interior_double"]
    )


# A plan with a laundry (washer+dryer), a covered porch, and an explicit gable
# roof — the completeness additions. Envelope 40x30, pitch defaults to 4:12.
_COMPLETE = """\
plan "Complete"
envelope 40 x 30
ceiling 9
roof gable
room living: living at 0,0 size 24 x 30
room laundry: laundry at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
porch front at 0,-8 size 24 x 8 covered
entry living south width 3 offset 4
"""


def _named_line(est, item):
    for ln in est["assemblies"]:
        if ln["item"] == item:
            return ln
    raise AssertionError(f"no assembly line named {item!r}")


def test_washer_and_dryer_are_costed_for_a_laundry():
    est = estimate_cost(compile_source(_COMPLETE))
    w, d = _named_line(est, "Washer"), _named_line(est, "Dryer")
    assert w["quantity"] == 1 and d["quantity"] == 1
    assert w["cost"] == DEFAULT_UNIT_COSTS["fixture_washer"]
    assert d["cost"] == DEFAULT_UNIT_COSTS["fixture_dryer"]
    assert w["cost"] > 0 and d["cost"] > 0  # no longer $0


def test_porch_slab_and_covered_porch_roof_lines():
    import math

    est = estimate_cost(compile_source(_COMPLETE))
    slab = _named_line(est, "Porch slab")
    # 24 x 8 porch platform = 192 sqft at the $9 slab rate.
    assert slab["quantity"] == 192
    assert slab["cost"] == 192 * DEFAULT_UNIT_COSTS["slab_sqft"]
    roof = _named_line(est, "Porch roof")
    # Covered porch roof is the flat area sloped by sec(atan(pitch)), pitch 4:12.
    slope = math.hypot(1.0, 4.0 / 12.0)
    assert abs(roof["quantity"] - round(192 * slope, 2)) < 0.01
    assert roof["cost_key"] == "roof_sqft"


def test_gable_end_triangles_added_only_for_a_gable_roof():
    est = estimate_cost(compile_source(_COMPLETE))
    gable = _named_line(est, "Gable-end walls")
    # Two triangles, base = envelope width 40, rise = 20 * (4/12): total
    # 40**2 * (4/12) / 2 = 266.67 sqft, sheathed at the exterior-wall rate.
    assert abs(gable["quantity"] - round(40 * 40 * (4.0 / 12.0) / 2.0, 2)) < 0.01
    assert gable["cost_key"] == "exterior_wall_sqft"
    # A shed roof has no gable ends → no such line.
    shed = estimate_cost(compile_source(_COMPLETE.replace("roof gable", "roof shed")))
    assert not any(ln["item"] == "Gable-end walls" for ln in shed["assemblies"])


def test_exclusions_footer_present_and_overridable_costs_still_work():
    est = estimate_cost(compile_source(_COMPLETE))
    # Phase 16 reworded the footer: it no longer says "site work" (drive/walk/
    # well/septic are itemised when declared); it names what's still excluded.
    assert "Excludes: site work" not in est["exclusions"]
    assert "permits" in est["exclusions"]
    assert "GC overhead & profit" in cost_text(est)
    # HVAC IS itemised, so the footer says so.
    assert any(ln["item"] == "HVAC allowance" for ln in est["assemblies"])
    assert "itemized" in est["exclusions"]
    # Overrides still reach the new keys.
    bumped = estimate_cost(compile_source(_COMPLETE), overrides={"fixture_washer": 1000.0})
    assert _named_line(bumped, "Washer")["cost"] == 1000.0


def test_estimate_is_json_safe_and_deterministic():
    a, b = _est(), _est()
    assert a == b
    assert json.loads(json.dumps(a)) == a


def test_disclaimer_present_in_dict_and_text():
    est = _est()
    assert est["disclaimer"] == DISCLAIMER
    text = cost_text(est)
    assert "budgeting only" in text
    assert "Estimated total" in text
    assert "Slab-on-grade" in text


def test_estimate_rejects_planless_result():
    result = compile_source("not a plan at all\n")
    assert result.plan is None
    try:
        estimate_cost(result)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on a plan-less compile")


def test_cli_cost_human_json_and_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.barn"
    good.write_text(SRC, encoding="utf-8")
    assert main(["cost", str(good)]) == 0
    out = capsys.readouterr().out
    assert "Cost estimate — Cost Fixture" in out
    assert "budgeting only" in out

    assert main(["cost", str(good), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["plan"] == "Cost Fixture"
    assert parsed["total"]["expected"] > 0

    # Uncompilable file → exit 2 (a cost with no plan is meaningless).
    bad = tmp_path / "bad.barn"
    bad.write_text("not a plan at all\n", encoding="utf-8")
    assert main(["cost", str(bad)]) == 2

    # Unreadable / missing file → exit 2.
    assert main(["cost", str(tmp_path / "nope.barn")]) == 2


def test_cli_cost_refuses_a_partial_recovery_with_errors(tmp_path, capsys):
    """Parser recovery can yield a plan alongside ERRORS; a deliverable must
    not price a half-parsed building — exit 2, report on stderr."""
    from barndsl import compile_source as _cs

    src = SRC + "window bogus north width 4\n"
    partial = _cs(src)
    if partial.plan is None or not partial.errors:  # fixture sanity
        raise AssertionError("fixture no longer yields a partial plan with errors")
    f = tmp_path / "partial.barn"
    f.write_text(src, encoding="utf-8")
    assert main(["cost", str(f)]) == 2
    assert "error" in capsys.readouterr().err.lower()


def test_cli_cost_overrides_and_multiplier(tmp_path, capsys):
    good = tmp_path / "good.barn"
    good.write_text(SRC, encoding="utf-8")
    costs = tmp_path / "costs.json"
    costs.write_text(json.dumps({"slab_sqft": 50.0}), encoding="utf-8")
    assert main(["cost", str(good), "--json", "--costs", str(costs), "--multiplier", "1.5"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    slab = next(l for l in parsed["assemblies"] if l["cost_key"] == "slab_sqft")
    assert slab["unit_cost"] == 75.0  # effective: 50 override x 1.5 multiplier
    assert slab["cost"] == 1200 * 50.0 * 1.5
    assert parsed["multiplier"] == 1.5

    # A bad --costs file → exit 2.
    costs.write_text("{ not json", encoding="utf-8")
    assert main(["cost", str(good), "--costs", str(costs)]) == 2
