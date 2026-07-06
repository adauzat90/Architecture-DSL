"""CLI: new, fmt, --strict, build --json/--format, watch helper, and SVG dims."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys

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


# -- I/O errors (clean messages, exit 2, no traceback) --------------------


def test_missing_file_is_a_clean_error(capsys):
    assert main(["compile", "/no/such/file.barn"]) == 2
    err = capsys.readouterr().err
    assert "error: /no/such/file.barn: no such file" in err
    assert "Traceback" not in err


def test_directory_argument_is_a_clean_error(tmp_path, capsys):
    assert main(["compile", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "is a directory" in err
    assert "Traceback" not in err


def test_invalid_utf8_is_a_clean_error(tmp_path, capsys):
    p = tmp_path / "latin.barn"
    p.write_bytes(b'plan "x"\n\xff\xfe not utf-8\n')
    assert main(["compile", str(p)]) == 2
    err = capsys.readouterr().err
    assert "not valid UTF-8 text" in err
    assert "Traceback" not in err


def test_io_error_prints_no_traceback_via_subprocess(tmp_path):
    """End-to-end: a real process invocation for a missing file exits 2 with a
    one-line message and *no* Python traceback on stderr."""
    env = dict(os.environ)
    src_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "barndsl.cli", "compile", str(tmp_path / "gone.barn")],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "no such file" in proc.stderr


# -- stdin (`-`) and BOM --------------------------------------------------


_STDIN_PLAN = (
    'plan "Stdin"\nenvelope 30 x 24\nceiling 9\n'
    "room a: living at 0,0 size 20 x 16\nentry a south width 3 offset 4\n"
)


def test_compile_reads_stdin_dash(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(_STDIN_PLAN))
    assert main(["compile", "-"]) == 0
    assert "COMPILE OK" in capsys.readouterr().out


def test_score_reads_stdin_dash(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(_STDIN_PLAN))
    assert main(["score", "-"]) == 0
    assert "Design score:" in capsys.readouterr().out


def test_fmt_reads_stdin_and_writes_stdout(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('plan   "F"\nenvelope 30 x 24\n' + _STDIN_PLAN.split("\n", 2)[2]))
    assert main(["fmt", "-"]) == 0
    out = capsys.readouterr().out
    assert out.startswith('plan "F"')  # normalised spacing, emitted to stdout


def test_bom_prefixed_file_compiles(tmp_path, capsys):
    p = tmp_path / "bom.barn"
    p.write_text("﻿" + _STDIN_PLAN, encoding="utf-8")
    assert main(["compile", str(p)]) == 0
    assert "COMPILE OK" in capsys.readouterr().out


def test_compile_source_strips_bom_for_api_callers():
    r = compile_source("﻿" + _STDIN_PLAN)
    assert r.plan is not None
    assert not any(d.code == "SYNTAX" for d in r.diagnostics)


# -- implausible dimensions (no inf leak) ---------------------------------


def test_huge_envelope_is_rejected_without_leaking_inf(tmp_path, capsys):
    p = _write(
        tmp_path, "huge.barn",
        'plan "Huge"\nenvelope 1e308 x 1e308\nceiling 9\n'
        "room a: living at 0,0 size 20 x 16\nentry a south width 3 offset 4\n",
    )
    rc = main(["compile", str(p), "--metrics"])
    out = capsys.readouterr().out
    assert rc == 1  # a hard error → non-zero
    assert "DIM_IMPLAUSIBLE" in out
    # No `inf` as a standalone token (would be `inf sq ft` etc.) — but "info(s)"
    # in the diagnostic tally is fine, hence the word-boundary match.
    import re
    assert not re.search(r"\binf\b", out.lower())


def test_huge_room_is_rejected():
    r = compile_source(
        'plan "R"\nenvelope 40 x 30\nceiling 9\n'
        "room a: living at 0,0 size 5000 x 16\n"
    )
    codes = [d.code for d in r.diagnostics]
    assert "DIM_IMPLAUSIBLE" in codes
    # Metrics stay finite (clamped) — no inf anywhere in the takeoff.
    import math
    for v in r.plan.metrics().values():
        if isinstance(v, float):
            assert math.isfinite(v)


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
