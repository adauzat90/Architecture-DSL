"""Statement-level parser error recovery (review §1.3).

A statement that fails to parse records its diagnostic and is skipped; the
surviving statements still build a partial plan that is validated and scored, so
one typo costs its own error rather than the whole continuous score gradient the
agent hill-climbs on. The result stays FAILED (ok=False). A source where nothing
parses into a room keeps the historical ``plan is None`` / flat-zero score.
"""

from __future__ import annotations

from barndsl import compile_source
from barndsl.score import design_score


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- a bad line is skipped, survivors are kept -------------------------------


def test_bad_line_is_skipped_and_survivors_build_a_partial_plan():
    src = (
        'plan "P"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 20 x 30\n"
        "room bad: living at 0,0 size 10 x\n"   # line 5: malformed size -> skipped
        "room kitchen: kitchen at 20,0 size 20 x 30\n"
        "entry living south width 3 offset 10\n"
        "window living west width 10 offset 8\n"
    )
    r = compile_source(src)
    assert not r.ok                      # still a failed compile
    assert r.plan is not None            # but a partial plan survives
    ids = {room.id for room in r.plan.rooms}
    assert ids == {"living", "kitchen"}  # the good rooms both parsed
    # The parse error is recorded on its own line, with its original code.
    parse = [d for d in r.errors if d.line == 5]
    assert parse and parse[0].code in ("BAD_NUMBER", "SYNTAX")


def test_recovery_still_validates_the_partial_plan():
    # The survivors overlap, so validation runs on the partial plan and flags it
    # (the old behaviour dropped every semantic check on a parse failure).
    src = (
        'plan "P"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 30 x 30\n"
        "room b: bedroom at 10,0 size 20 x 20\n"   # overlaps a
        "roof wobble\n"                            # line 6: bad roof style -> skipped
        "entry a south width 3 offset 10\n"
    )
    r = compile_source(src)
    assert not r.ok
    codes = {d.code for d in r.diagnostics}
    assert "OVERLAP" in codes            # semantic check ran on the survivors
    assert any(d.line == 6 for d in r.errors)  # the parse error is still recorded


# --- the continuous score gradient is preserved ------------------------------


def test_partial_plan_keeps_the_continuous_score_gradient():
    # A parse failure with good survivors: the total is 0 (errors dominate), but
    # the continuous components are computed from the partial plan instead of
    # being dropped — the gradient the agent climbs survives the broken round.
    good = (
        'plan "P"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 40 x 30\n"   # fills the footprint
        "window living west width 20 offset 5\n"      # well-lit
        "entry living south width 3 offset 10\n"
        "totally bogus line\n"                        # a parse error
    )
    r = compile_source(good)
    report = design_score(r)
    assert report.total == 0.0                 # errors gate the total to zero
    assert report.components["errors"] == 100.0
    # ...but the continuous terms reflect the survivors (space is near-full here).
    assert report.components["space"] < 5.0


def test_worse_partial_plan_has_a_worse_gradient_than_a_better_one():
    # Two broken plans (each with the same parse error) still rank by their
    # survivors' continuous quality — a gradient a plan-None would have flattened.
    base = (
        'plan "P"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "{rooms}"
        "bogus line here\n"
    )
    full = compile_source(base.format(rooms="room a: living at 0,0 size 40 x 30\n"))
    sparse = compile_source(base.format(rooms="room a: living at 0,0 size 10 x 10\n"))
    # Both fail; both score 0 total, but the sparse plan wastes more footprint.
    assert not full.ok and not sparse.ok
    full_space = design_score(full).components["space"]
    sparse_space = design_score(sparse).components["space"]
    assert sparse_space > full_space


# --- truly-unparseable input keeps plan=None ---------------------------------


def test_no_survivors_keeps_plan_none_and_scores_zero():
    r = compile_source("not a plan at all")
    assert r.plan is None                       # nothing parsed into a room
    assert design_score(r).total == 0.0


def test_only_a_bad_envelope_with_no_rooms_is_plan_none():
    # envelope fails, and there are no rooms to survive -> unbuildable, plan None.
    r = compile_source('plan "P"\nenvelope 40 x\n')
    assert r.plan is None


# --- semantic (build-time) errors are unchanged ------------------------------


def test_semantic_errors_still_produce_a_plan_as_before():
    # An unknown door reference is a validation error, not a parse error; it has
    # always produced a plan, and recovery must not change that.
    src = (
        'plan "P"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 40 x 30\n"
        "door living - ghost width 3\n"   # ghost doesn't exist -> DOOR_REF
        "entry living south width 3 offset 10\n"
    )
    r = compile_source(src)
    assert r.plan is not None and not r.ok
    assert "DOOR_REF" in _codes(r, "error")


# --- the `recovered` flag and what it gates -----------------------------------

GOOD = (
    'plan "P"\n'
    "envelope 40 x 30\n"
    "ceiling 9\n"
    "room living: living at 0,0 size 20 x 30\n"
    "room kitchen: kitchen at 20,0 size 20 x 30\n"
    "door living - kitchen width 3\n"
    "entry living south width 3 offset 10\n"
    "window living west width 10 offset 8\n"
)
PARSE_BAD = GOOD + "room bad: living at 0,0 size 10 x\n"
SEMANTIC_BAD = GOOD + "window bogus north width 4\n"


def test_recovered_flag_marks_parse_skips_only():
    """`recovered` is True exactly when a statement was skipped by recovery —
    a semantic error records an ERROR but skips nothing, so it stays False."""
    assert compile_source(GOOD).recovered is False
    assert compile_source(PARSE_BAD).recovered is True
    r = compile_source(SEMANTIC_BAD)
    assert r.recovered is False and r.errors


def test_semantic_error_compiles_keep_unguarded_validation(monkeypatch):
    """The recovery guard must not widen: a validator crash on a compile with
    only SEMANTIC errors (no skipped statements) still propagates."""
    import pytest

    import barndsl.compiler as compiler

    def boom(plan, profile=None):
        raise RuntimeError("validator bug")

    monkeypatch.setattr(compiler, "validate", boom)
    with pytest.raises(RuntimeError):
        compile_source(SEMANTIC_BAD)


def test_swallowed_validation_crash_is_reported_on_the_recovery_path(monkeypatch):
    """When validation crashes on a genuine partial plan the swallow is
    RECORDED — a RECOVERY_LIMIT warning says the diagnostics are incomplete."""
    import barndsl.compiler as compiler

    def boom(plan, profile=None):
        raise RuntimeError("validator bug")

    monkeypatch.setattr(compiler, "validate", boom)
    r = compile_source(PARSE_BAD)
    assert r.plan is not None and r.recovered
    assert "RECOVERY_LIMIT" in {d.code for d in r.warnings}


def test_output_commands_refuse_a_recovered_partial(tmp_path, capsys):
    """schedule/build/dxf/revit keep the pre-recovery contract: a parse error
    yields no artifact and a failing exit code."""
    from barndsl.cli import main

    f = tmp_path / "partial.barn"
    f.write_text(PARSE_BAD, encoding="utf-8")

    assert main(["schedule", str(f)]) == 1
    capsys.readouterr()

    out_svg = tmp_path / "b.svg"
    assert main(["build", str(f), "--out", str(out_svg)]) == 1
    assert not out_svg.exists()
    capsys.readouterr()

    out_dxf = tmp_path / "b.dxf"
    assert main(["dxf", str(f), "--out", str(out_dxf)]) == 1
    assert not out_dxf.exists()
    capsys.readouterr()

    out_json = tmp_path / "r.json"
    assert main(["revit", str(f), "--out", str(out_json)]) == 1
    assert not out_json.exists()
    capsys.readouterr()


def test_build_json_reports_the_skipped_render_for_a_recovered_partial(tmp_path, capsys):
    import json

    from barndsl.cli import main

    f = tmp_path / "partial.barn"
    f.write_text(PARSE_BAD, encoding="utf-8")
    out_svg = tmp_path / "b.svg"
    assert main(["build", str(f), "--json", "--out", str(out_svg)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["out"] is None
    assert "recovery" in payload["render_error"]
    assert not out_svg.exists()
