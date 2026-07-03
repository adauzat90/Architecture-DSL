"""Tests for the thermal-envelope guidance (review item #5).

A `climate <zone>` (IECC 1–8) turns on two INFO nudges, gated on it being
declared so ordinary plans are untouched:

- ENERGY_ENVELOPE — the zone's prescriptive R-value targets + the steel-frame
  thermal-bridge note (insulate a metal shell with continuous exterior insulation).
- WINDOW_HEAVY    — a window-to-wall-ratio *ceiling* to go with the daylight floor.
"""

from __future__ import annotations

import pytest

from barndsl import barndominium, compile_source, emit_dsl, validate
from barndsl import Direction as D, RoomType as T
from barndsl.energy import WWR_CEILING, describe_targets, envelope_targets


def _codes(result_or_report, severity: str) -> set[str]:
    bucket = {
        "error": result_or_report.errors,
        "warning": result_or_report.warnings,
        "info": result_or_report.infos,
    }[severity]
    return {d.code for d in bucket}


_BASE = """\
plan "Envelope"
envelope 40 x 30
ceiling 9
{climate}room living: living at 0,0 size 40 x 30
entry living south width 3 offset 4
window living south width 6 offset 20
"""


# --- the reference data (pure) -----------------------------------------------


def test_envelope_targets_cover_all_zones():
    for z in range(1, 9):
        t = envelope_targets(z)
        assert t["ceiling"].startswith("R-")
        assert "R-" in t["wall"]
    # Colder zones ask for continuous insulation on the wall.
    assert "c.i." in envelope_targets(5)["wall"]
    assert describe_targets(5).startswith("ceiling R-")


# --- the directive -----------------------------------------------------------


def test_climate_parses_round_trips_and_records_metric():
    plan = compile_source(_BASE.format(climate="climate 5\n")).plan
    assert plan.climate == 5
    assert "climate 5" in emit_dsl(plan)
    assert compile_source(emit_dsl(plan)).plan.climate == 5
    assert plan.metrics()["climate_zone"] == 5.0


def test_bad_zone_is_a_parse_error():
    assert "BAD_OPTION" in _codes(compile_source(_BASE.format(climate="climate 9\n")), "error")
    assert "BAD_OPTION" in _codes(compile_source(_BASE.format(climate="climate 5.5\n")), "error")


def test_builder_rejects_out_of_range_zone():
    with pytest.raises(ValueError):
        barndominium("x").envelope(width=40, length=30).set_climate(0)


# --- ENERGY_ENVELOPE ---------------------------------------------------------


def test_climate_emits_envelope_guidance():
    r = compile_source(_BASE.format(climate="climate 5\n"))
    assert r.ok  # guidance, never a blocker
    assert "ENERGY_ENVELOPE" in _codes(r, "info")
    msg = next(i.message for i in r.infos if i.code == "ENERGY_ENVELOPE")
    assert "zone 5" in msg
    assert "ceiling R-60" in msg
    assert "continuous exterior insulation" in msg  # the metal-frame note


def test_no_climate_means_no_energy_guidance():
    r = compile_source(_BASE.format(climate=""))
    assert "ENERGY_ENVELOPE" not in _codes(r, "info")
    assert "WINDOW_HEAVY" not in _codes(r, "info")
    assert r.plan.climate is None


# --- WINDOW_HEAVY ------------------------------------------------------------


def _glassy(climate: bool):
    # Windows on all four walls, tall (sill 1 → head 8): a high glazing ratio.
    plan = (
        barndominium("Glass")
        .envelope(width=24, length=20)
        .ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=24, length=20)
        .entrance("a", D.SOUTH, width=3, offset=2)
    )
    if climate:
        plan.set_climate(5)
    for wall, off in [
        (D.SOUTH, 6), (D.SOUTH, 14), (D.NORTH, 4), (D.NORTH, 14),
        (D.EAST, 4), (D.EAST, 12), (D.WEST, 4), (D.WEST, 12),
    ]:
        plan.add_window("a", wall, width=6, offset=off, sill_height=1, head_height=8)
    return plan


def test_over_glazed_plan_is_flagged():
    plan = _glassy(climate=True)
    glazing = sum(w.glazed_area for w in plan.windows)
    assert glazing / plan.metrics()["exterior_wall_area_sqft"] > WWR_CEILING
    assert "WINDOW_HEAVY" in _codes(validate(plan), "info")


def test_over_glazed_but_no_climate_is_silent():
    # The WWR ceiling is part of the energy opt-in — no climate, no nudge.
    assert "WINDOW_HEAVY" not in _codes(validate(_glassy(climate=False)), "info")


def test_normal_glazing_is_not_window_heavy():
    r = compile_source(_BASE.format(climate="climate 5\n"))
    assert "WINDOW_HEAVY" not in _codes(r, "info")
