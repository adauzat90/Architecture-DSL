"""Tests for the Revit builder's pure report/options layer (`barndsl_revit.report`).

Revit-free, so it runs in the normal suite. Covers the bookkeeping (counts by
kind/status), the dry-run vs. live wording, JSON/markdown rendering, and the
``BuildOptions`` round-trip used by the sidecar config file.
"""

from __future__ import annotations

import json
import os
import sys

_LIB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "revit",
    "barndsl.extension",
    "lib",
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import report  # noqa: E402


def _sample():
    rep = report.BuildReport()
    rep.created("wall", "w0", revit_id=1001)
    rep.created("wall", "w1", revit_id=1002)
    rep.created("door", "o0", revit_id=1003)
    rep.skipped("door", "o1", message="no host wall")
    rep.failed("room", "bath", message="point not enclosed")
    rep.note("using default door family")
    rep.problems.append("opening o9 references missing wall")
    rep.resources["exterior_wall"] = "Generic - 8\""
    return rep


def test_counts_by_kind_and_status():
    rep = _sample()
    assert rep.count(status=report.CREATED) == 3
    assert rep.count(status=report.CREATED, kind="wall") == 2
    assert rep.count(status=report.SKIPPED) == 1
    assert rep.count(status=report.FAILED) == 1
    by_kind = rep.counts_by_kind()
    assert by_kind["wall"] == {"created": 2}
    assert by_kind["door"] == {"created": 1, "skipped": 1}
    assert by_kind["room"] == {"failed": 1}


def test_summary_line_live_vs_dry():
    rep = _sample()
    line = rep.summary_line()
    assert line.startswith("created")
    assert "2 wall" in line and "1 door" in line
    assert "1 failed" in line and "1 skipped" in line

    dry = report.BuildReport(dry_run=True)
    dry.created("wall", "w0")
    assert dry.summary_line().startswith("[dry run] would create")


def test_to_dict_and_json_round_trip():
    rep = _sample()
    doc = rep.to_dict()
    assert doc["counts"]["wall"]["created"] == 2
    assert doc["problems"] and doc["notes"]
    assert len(doc["records"]) == 5
    # JSON serialises cleanly and preserves the records.
    parsed = json.loads(rep.to_json())
    assert parsed["summary"] == rep.summary_line()
    assert parsed["resources"]["exterior_wall"].startswith("Generic")


def test_markdown_lists_attention_items():
    rep = _sample()
    md = rep.to_markdown()
    assert "Build report" in md
    assert "need attention" in md
    assert "no host wall" in md  # skipped reason surfaced
    assert "point not enclosed" in md  # failure reason surfaced
    # created elements are summarised, not listed, unless verbose
    assert "id 1001" not in md
    assert "id 1001" in rep.to_markdown(include_created=True)


def test_markdown_dry_run_title():
    dry = report.BuildReport(dry_run=True)
    dry.created("wall", "w0")
    assert "Dry run" in dry.to_markdown()


def test_options_round_trip():
    opts = report.BuildOptions(
        stairs=False, dry_run=True, exterior_wall_type='Generic - 8"', door_family="Single-Flush"
    )
    d = opts.to_dict()
    assert d["stairs"] is False
    assert d["dry_run"] is True
    assert d["exterior_wall_type"] == 'Generic - 8"'
    again = report.BuildOptions.from_dict(d)
    assert again.to_dict() == d


def test_options_from_dict_ignores_unknown_keys():
    opts = report.BuildOptions.from_dict({"stairs": False, "comment": "ignore me", "_note": 1})
    assert opts.stairs is False
    assert opts.structure is True  # default preserved
