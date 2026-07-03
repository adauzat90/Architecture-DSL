"""`barndsl revit-log`: a Revit build log becomes compile-style diagnostics.

The buildlog schema is what `barndsl_revit.report.BuildReport.to_dict()`
writes (records with kind/source/status/message, plus notes); these tests
build logs through that real reporter so the two sides can't drift.
"""

from __future__ import annotations

import json
import os
import sys


sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "revit", "barndsl.extension", "lib",
    ),
)

from barndsl_revit import report as revit_report  # noqa: E402

from barndsl.cli import main  # noqa: E402
from barndsl.diagnostics import REGISTRY  # noqa: E402
from barndsl.revitlog import buildlog_issues, issues_to_dict  # noqa: E402
from barndsl.validation import Severity  # noqa: E402


def _log(**kw):
    rep = revit_report.BuildReport()
    rep.created("wall", "w1", revit_id=1)
    if kw.get("failed", True):
        rep.failed("window", "win1", "forced NewFamilyInstance failure")
    if kw.get("skipped", True):
        rep.skipped("door", "d2", "no garage-door family loaded")
    if kw.get("note", True):
        rep.note("overhead door d2: the sized standard door family stands in")
    rep.note("constrained 4 wall top(s) to the level above")  # chatter: not surfaced
    return rep.to_dict()


def test_failures_and_skips_are_warnings_and_notes_are_infos():
    issues = buildlog_issues(_log())
    by_code = {i.code: i for i in issues}
    assert by_code["REVIT_FAIL"].severity is Severity.WARNING
    assert "win1" in by_code["REVIT_FAIL"].message
    assert by_code["REVIT_SKIP"].severity is Severity.WARNING
    assert "family" in by_code["REVIT_SKIP"].hint  # actionable fix hint
    assert by_code["REVIT_NOTE"].severity is Severity.INFO
    assert "stands in" in by_code["REVIT_NOTE"].message


def test_progress_chatter_is_not_surfaced():
    issues = buildlog_issues(_log(failed=False, skipped=False, note=False))
    assert issues == []  # created records and constraint chatter say nothing


def test_codes_are_registered():
    for code in ("REVIT_FAIL", "REVIT_SKIP", "REVIT_NOTE"):
        assert code in REGISTRY


def test_order_is_failures_then_skips_then_notes():
    codes = [i.code for i in buildlog_issues(_log())]
    assert codes == ["REVIT_FAIL", "REVIT_SKIP", "REVIT_NOTE"]


def test_malformed_records_are_ignored():
    log = _log()
    log["records"].append("not-a-record")
    log["records"].append({"status": "failed"})  # minimal: still reported
    issues = buildlog_issues(log)
    assert sum(1 for i in issues if i.code == "REVIT_FAIL") == 2


def test_json_view_matches_compile_shape():
    d = issues_to_dict(buildlog_issues(_log()))
    assert set(d) == {"diagnostics"}
    first = d["diagnostics"][0]
    assert set(first) == {"severity", "code", "message", "room", "line", "hint"}
    json.dumps(d)  # serializable


def test_cli_revit_log_reports_and_gates(tmp_path, capsys):
    path = tmp_path / "plan.buildlog.json"
    path.write_text(json.dumps(_log()), encoding="utf-8")
    rc = main(["revit-log", str(path)])
    out = capsys.readouterr().out
    assert rc == 1  # warnings gate, like compile --strict
    assert "REVIT_FAIL" in out and "REVIT_SKIP" in out

    clean = tmp_path / "clean.buildlog.json"
    clean.write_text(json.dumps(_log(failed=False, skipped=False, note=False)))
    assert main(["revit-log", str(clean)]) == 0
    assert "clean" in capsys.readouterr().out


def test_cli_revit_log_json_and_bad_file(tmp_path, capsys):
    path = tmp_path / "plan.buildlog.json"
    path.write_text(json.dumps(_log()), encoding="utf-8")
    assert main(["revit-log", str(path), "--json"]) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["diagnostics"][0]["code"] == "REVIT_FAIL"

    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    assert main(["revit-log", str(bad)]) == 2
    assert "error" in capsys.readouterr().err
