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
    assert _line(est, "window_casement")["quantity"] == 3
    assert _line(est, "door_exterior")["quantity"] == 1
    assert _line(est, "door_interior")["quantity"] == 3


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
