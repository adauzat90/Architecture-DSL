"""CLI: new, fmt, --strict, build --json/--format, watch helper, and SVG dims."""

from __future__ import annotations

import argparse
import json

from barndsl import compile_source, render_svg
from barndsl.cli import _render_pass, main
from barndsl.render import RenderConfig
from barndsl.scaffold import starter_dsl

# A clean plan with a warning-only nudge is hard to guarantee; use the scaffold,
# which is verified 0/0/0.
CLEAN = starter_dsl("CLI Test")

WARNED = """\
plan "Warned"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,0 size 12 x 24
door living - bed width 1.5
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""


def _write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src)
    return p


# -- new ------------------------------------------------------------------


def test_new_writes_a_clean_plan(tmp_path, capsys):
    out = tmp_path / "scaffold.barn"
    rc = main(["new", "Cedar", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    # The scaffold must itself compile clean.
    assert compile_source(out.read_text()).ok
    assert "COMPILE OK" in capsys.readouterr().out


def test_new_refuses_to_clobber(tmp_path):
    out = tmp_path / "x.barn"
    out.write_text("existing")
    assert main(["new", "X", "--out", str(out)]) == 2
    assert out.read_text() == "existing"
    # --force overwrites.
    assert main(["new", "X", "--out", str(out), "--force"]) == 0
    assert out.read_text() != "existing"


# -- fmt ------------------------------------------------------------------


def test_fmt_check_then_write_is_idempotent(tmp_path):
    p = _write(tmp_path, "p.barn", CLEAN)
    # The scaffold has comments/alignment emit_dsl normalises → needs reformat.
    assert main(["fmt", "--check", str(p)]) == 1
    assert main(["fmt", "--write", str(p)]) == 0
    # Now it's canonical; --check passes and a second write is a no-op.
    assert main(["fmt", "--check", str(p)]) == 0


def test_fmt_rejects_a_file_with_errors(tmp_path, capsys):
    p = _write(tmp_path, "bad.barn", "plan \"X\"\nenvelope 10 x 10\nroom z: nope at 0,0 size 5 x 5\n")
    assert main(["fmt", "--check", str(p)]) == 2


# -- strict ---------------------------------------------------------------


def test_strict_fails_on_warnings(tmp_path):
    p = _write(tmp_path, "w.barn", WARNED)
    result = compile_source(WARNED)
    assert result.ok and result.warnings  # clean compile, but warned
    assert main(["compile", str(p)]) == 0
    assert main(["compile", str(p), "--strict"]) == 1


def test_strict_info_fails_on_warnings_too(tmp_path):
    p = _write(tmp_path, "w.barn", WARNED)
    assert main(["compile", str(p), "--strict-info"]) == 1


def test_clean_plan_passes_strict(tmp_path):
    # The scaffold is 0/0/0, so even --strict-info passes.
    p = _write(tmp_path, "c.barn", CLEAN)
    assert main(["compile", str(p), "--strict-info"]) == 0


# -- build json / format --------------------------------------------------


def test_build_json_has_metrics_and_out(tmp_path, capsys):
    p = _write(tmp_path, "b.barn", CLEAN)
    out = tmp_path / "b.svg"
    rc = main(["build", str(p), "--json", "--out", str(out)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "metrics" in payload and payload["metrics"]["footprint_sqft"] > 0
    assert payload["out"] == str(out)
    assert out.exists()


def test_build_default_out_follows_format(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _write(tmp_path, "b.barn", CLEAN)
    # No --out: default name is barndo.svg.
    assert main(["build", str(p)]) == 0
    assert (tmp_path / "barndo.svg").exists()


def test_build_unknown_format_errors(tmp_path):
    p = _write(tmp_path, "b.barn", CLEAN)
    # argparse rejects an out-of-choice format.
    try:
        main(["build", str(p), "--format", "bmp"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("expected argparse to reject the format")


# -- watch helper ---------------------------------------------------------


def test_render_pass_compiles_and_renders(tmp_path, capsys):
    p = _write(tmp_path, "w.barn", CLEAN)
    out = tmp_path / "w.svg"
    ns = argparse.Namespace(file=str(p), out=str(out))
    result = _render_pass(ns)
    assert result.ok
    assert out.exists()
    assert "COMPILE OK" in capsys.readouterr().out


# -- dimensioned svg ------------------------------------------------------


def test_room_dimension_labels_render():
    plan = compile_source(CLEAN).plan
    svg = render_svg(plan)
    # The 18×14 living room shows its W × L dimension line (the envelope's own
    # 33×24 extent label is a different string, so this is room-specific).
    assert "18′ × 14′" in svg


def test_room_dims_can_be_disabled():
    plan = compile_source(CLEAN).plan
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    assert "18′ × 14′" not in svg
