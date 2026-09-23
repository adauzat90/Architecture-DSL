"""Developer/harness helpers used by pi and `barndsl dev`."""

from pathlib import Path

from barndsl import devtools


def test_repo_audit_has_no_wiring_drift():
    out = devtools.repo_audit()
    assert out["ok"], out["problems"]
    assert out["statement_keywords"]["missing_in_dsl_reference"] == []
    assert out["statement_keywords"]["unclassified_host_or_part"] == []
    assert out["diagnostics"]["missing_registry"] == []
    assert out["diagnostics"]["severity_drift"] == []
    assert out["diagnostics"]["unclassified_category"] == []
    assert out["modules"]["private_imports"] == []


def test_repo_audit_checks_can_fail(monkeypatch):
    """The audit's checks must be able to fail — a drift check comparing a list
    with a copy of itself is no check at all."""
    import ast

    from barndsl import compiler, diagnostics

    monkeypatch.setattr(devtools, "PART_STATEMENTS", compiler.PART_STATEMENTS - {"room"})
    monkeypatch.setattr(diagnostics, "_VARYING", diagnostics._VARYING - {"FOYER_FLOW"})
    monkeypatch.setattr(diagnostics, "_EXPLICIT_CATEGORIES",
                        {k: v for k, v in diagnostics._EXPLICIT_CATEGORIES.items() if k != "MECH_ACCESS"})
    real_trees = devtools._src_trees
    planted = devtools.SRC / "barndsl" / "planted.py"
    monkeypatch.setattr(devtools, "_src_trees", lambda: {
        **real_trees(),
        planted: ast.parse("from .compiler import _PLACEMENT, PLACEMENT_ANCHORS\n"
                           "from barndsl.render import __all__\n"),
    })
    out = devtools.repo_audit()
    assert not out["ok"]
    assert out["statement_keywords"]["unclassified_host_or_part"] == ["room"]
    assert any(d.startswith("FOYER_FLOW") for d in out["diagnostics"]["severity_drift"])
    assert out["diagnostics"]["unclassified_category"] == ["MECH_ACCESS"]
    # Only the private name is flagged — not the public one, nor a dunder.
    assert out["modules"]["private_imports"] == ["src/barndsl/planted.py:1 imports compiler._PLACEMENT"]


def test_pi_extension_confines_file_tools_to_the_workspace():
    root = Path(__file__).resolve().parents[1]
    source = (root / ".pi" / "extensions" / "barndsl-harness.ts").read_text(
        encoding="utf-8"
    )

    assert "function assertContained" in source
    assert "function workspaceArg" in source
    assert "function artifactArg" in source
    assert "workspaceArg(ctx.cwd, params.path)" in source
    assert "const out = artifactArg(ctx.cwd" in source
    assert "resolve(ctx.cwd, cleanPath(params.path))" not in source


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
    assert "tests/test_lsp.py" in devtools.impact_targets(["src/barndsl/lsp.py"])


def test_impact_targets_cover_every_module_beyond_the_fallback():
    """A change to any barndsl module runs at least one test that imports it,
    not just the generic ``test_compiler.py`` fallback."""
    src = devtools.ROOT / "src" / "barndsl"
    thin = []
    for path in sorted(src.glob("*.py")):
        if path.name == "__init__.py":
            continue
        targets = devtools.impact_targets([f"src/barndsl/{path.name}"])
        if targets == ["tests/test_compiler.py"] and path.stem != "compiler":
            thin.append(path.stem)
    assert thin == []


def test_impact_targets_add_metamorphic_suite_for_core_modules():
    for mod in ("emit", "fmt", "compose", "elements"):
        assert "tests/test_metamorphic.py" in devtools.impact_targets([f"src/barndsl/{mod}.py"])


def test_feature_check_known_statement_is_wired():
    out = devtools.feature_check("room")
    assert out["ok"], out["missing"]
    assert out["statement"] is True


def test_locate_finds_diagnostic_breadcrumbs():
    out = devtools.locate("BEDROOM_EGRESS", max_results=20)
    assert out["ok"]
    assert "diagnostic" in out["kind"]
    assert out["exact"]["diagnostic"]["registry"]["line"] is not None
    assert any(hit["path"].replace("\\", "/").startswith("src/barndsl/") for hit in out["matches"]["source"])


def test_locate_finds_statement_breadcrumbs():
    out = devtools.locate("room", max_results=20)
    assert out["ok"]
    assert "statement" in out["kind"]
    assert out["exact"]["statement"]["compiler_keywords"]["line"] is not None


def test_diagnostic_matrix_indexes_registry_emitters_and_tests():
    out = devtools.diagnostic_matrix(["examples"], max_hits=3)
    row = next(r for r in out["rows"] if r["code"] == "BEDROOM_EGRESS")
    assert out["ok"]
    assert row["registry"]["line"] is not None
    assert row["category"] == "access_egress"
    assert row["owner"]
    assert row["emitters"]
    assert row["tests"]
    assert "no_literal_emitter" not in row["gaps"]


def test_fixture_catalog_has_composed_and_multilevel_roles():
    out = devtools.fixture_catalog(["examples"])
    roles = {r["role"] for r in out["roles"]}
    assert out["ok"]
    assert {"minimal_valid_inline", "composed_smoke", "multi_level", "fragment_part"} <= roles
    assert out["summary"]["fragments"] >= 1
    assert any("multi_level" in f["features"] for f in out["files"])


def test_doctor_default_gate_passes_without_export():
    out = devtools.doctor(run_impact=False)
    assert out["ok"], out["next_steps"]
    assert [c["name"] for c in out["checks"]] == ["audit", "gallery_gate", "lsp_smoke", "impact_targets"]
