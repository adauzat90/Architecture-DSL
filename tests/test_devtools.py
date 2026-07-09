"""Developer/harness helpers used by pi and `barndsl dev`."""

from barndsl import devtools


def test_repo_audit_has_no_wiring_drift():
    out = devtools.repo_audit()
    assert out["ok"], out["problems"]
    assert out["statement_keywords"]["missing_in_playground"] == []
    assert out["diagnostics"]["missing_registry"] == []


def test_rule_probe_asserts_codes():
    src = (
        'plan "Probe"\n'
        'envelope 24 x 24\n'
        'ceiling 9\n'
        'room living: living at 0,0 size 12 x 12\n'
        'entry living south width 3\n'
    )
    out = devtools.rule_probe(
        src,
        {"warning": ["NAT_LIGHT"], "codes": ["NO_BATH"], "absent": ["BEDROOM_EGRESS"]},
    )
    assert out["ok"], out["failures"]


def test_diagnostic_diff_treats_headerless_parts_as_fragments():
    out = devtools.diagnostic_diff(["examples/composed/parts/master_suite.barn"])
    only = next(iter(out["files"]["after"].values()))
    assert only["fragment"] is True
    # These are false positives if a part is compiled as a whole plan.
    assert "ENVELOPE" not in only["codes"]
    assert "NO_ENTRY" not in only["codes"]


def test_lsp_smoke_basic_passes_even_if_composed_fixture_regresses():
    out = devtools.lsp_smoke()
    assert out["basic_ok"] is True
    assert out["ok"] is True


def test_impact_targets_maps_lsp_change():
    assert devtools.impact_targets(["src/barndsl/lsp.py"]) == ["tests/test_lsp.py"]


def test_feature_check_known_statement_is_wired():
    out = devtools.feature_check("room")
    assert out["ok"], out["missing"]
    assert out["statement"] is True


def test_doctor_default_gate_passes_without_export():
    out = devtools.doctor(run_impact=False)
    assert out["ok"], out["next_steps"]
    assert [c["name"] for c in out["checks"]] == ["audit", "gallery_gate", "lsp_smoke", "impact_targets"]
