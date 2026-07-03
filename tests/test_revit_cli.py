"""CLI-level hardening for the Revit-free commands (`revit`, `revit-import`).

These cover the failure paths a user hits first — a missing or malformed input
file, an unwritable destination, and (the regression that motivated this) a
stdout that can't encode the report's non-ASCII glyphs. Each must degrade to a
clean ``error:`` message or complete anyway, never a raw traceback.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from barndsl.cli import main

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "cedar_ridge.barn"


def test_revit_missing_source_errors_cleanly(tmp_path, capsys):
    rc = main(["revit", str(tmp_path / "nope.barn"), "--out", str(tmp_path / "x.json")])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "nope.barn" in err


def test_revit_unwritable_out_errors_cleanly(tmp_path, capsys):
    out = tmp_path / "no_such_dir" / "x.json"  # parent doesn't exist
    rc = main(["revit", str(EXAMPLE), "--out", str(out)])
    assert rc == 2
    assert capsys.readouterr().err.startswith("error:")


def test_revit_import_missing_file_errors_cleanly(tmp_path, capsys):
    rc = main(["revit-import", str(tmp_path / "gone.json")])
    assert rc == 2
    assert capsys.readouterr().err.startswith("error:")


def test_revit_import_bad_json_errors_cleanly(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json {", encoding="utf-8")
    rc = main(["revit-import", str(bad)])
    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_revit_import_wrong_schema_errors_cleanly(tmp_path, capsys):
    doc = tmp_path / "other.json"
    doc.write_text(json.dumps({"schema": "something/else"}), encoding="utf-8")
    rc = main(["revit-import", str(doc)])
    assert rc == 2
    assert capsys.readouterr().err.startswith("error:")


def test_revit_exchange_survives_non_utf8_stdout(tmp_path):
    """The report carries glyphs like em dash and ``≥``; a legacy/piped stdout
    (cp1252, strict) used to crash mid-print with UnicodeEncodeError. The CLI
    now forces UTF-8 on its streams, so the command completes and writes the
    exchange regardless of the ambient encoding.
    """
    out = tmp_path / "cedar.json"
    boot = "import sys; from barndsl.cli import main; sys.exit(main())"
    env = dict(os.environ, PYTHONIOENCODING="cp1252")  # force the strict codec
    proc = subprocess.run(
        [sys.executable, "-c", boot, "revit", str(EXAMPLE), "--out", str(out)],
        env=env, capture_output=True, cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"Traceback" not in proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "barndsl.revit/1"
