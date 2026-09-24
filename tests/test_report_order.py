"""A compile reports its diagnostics in one order (TD-6c): by line (none last),
column (none last), severity (errors first), then code; ties keep the order
they were emitted in.

Before, the list came out in whatever order the checks ran, and ``report()`` and
``to_dict()`` each sorted by line with line-less diagnostics first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from barndsl import compile_source
from barndsl.issues import Issue, Severity, report_order

ROOT = Path(__file__).resolve().parents[1]
E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO


def _is_sorted(issues: list[Issue]) -> bool:
    keys = [report_order(i) for i in issues]
    return keys == sorted(keys)


def test_the_order_is_line_column_severity_code():
    issues = [
        Issue(I, "NO_BATH", "no line"),
        Issue(W, "B_CODE", "line 2, no column", line=2),
        Issue(I, "A_CODE", "line 2 col 5", line=2, col=5),
        Issue(E, "Z_CODE", "line 2 col 5, an error", line=2, col=5),
        Issue(W, "Z_CODE", "line 1, code Z", line=1, col=9),
        Issue(W, "A_CODE", "line 1, code A", line=1, col=9),
        Issue(E, "EMPTY", "no line, an error"),
        Issue(W, "C_CODE", "a column but no line", col=3),
    ]
    assert [i.message for i in sorted(issues, key=report_order)] == [
        "line 1, code A",
        "line 1, code Z",
        "line 2 col 5, an error",
        "line 2 col 5",
        "line 2, no column",
        "a column but no line",
        "no line, an error",
        "no line",
    ]


def test_ties_keep_the_order_they_were_emitted_in():
    first = Issue(W, "ROOM_TIGHT", "first", line=3)
    second = Issue(W, "ROOM_TIGHT", "second", line=3)
    assert sorted([first, second], key=report_order) == [first, second]
    assert sorted([second, first], key=report_order) == [second, first]


_PLANS = sorted(p for p in ROOT.glob("examples/**/*.barn") if "parts" not in p.parts)
_PARTS = sorted(ROOT.glob("examples/**/parts/*.barn"))


@pytest.mark.parametrize("path", _PLANS + _PARTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_compile_comes_out_in_report_order(path):
    source = path.read_text(encoding="utf-8")
    result = compile_source(source, base_dir=str(path.parent), fragment=path in _PARTS)
    assert _is_sorted(result.diagnostics)


_NO_BATH = (
    'plan "P"\nenvelope 30 x 20\nceiling 9\n'
    "room living: living at 0,0 size 30 x 20\n"
    "entry living south width 3 offset 6\n"
)


def test_the_report_and_json_keep_the_order_after_an_append():
    # The agent appends its own feedback (DESIGN, TRUNCATED) after the compile;
    # the text report and the JSON still list everything in report order, and a
    # plan-level diagnostic with no line (NO_BATH) comes last, not first.
    result = compile_source(_NO_BATH)
    lines = [d.line for d in result.diagnostics]
    assert None in lines and lines[lines.index(None):] == [None] * (len(lines) - lines.index(None))
    assert "NO_BATH" in [d.code for d in result.diagnostics if d.line is None]
    result.diagnostics.append(Issue(E, "DESIGN", "appended late", line=1))
    expected = [(d.line, d.code) for d in sorted(result.diagnostics, key=report_order)]
    assert expected[0] == (1, "DESIGN")
    assert [(d["line"], d["code"]) for d in result.to_dict()["diagnostics"]] == expected
    headers = [ln for ln in result.report().splitlines() if ln.startswith("<plan>")]
    assert [h.split("[")[1].split("]")[0] for h in headers] == [code for _, code in expected]


def test_pragmas_apply_before_the_sort():
    # An accepted warning is reported as an INFO and sorts as one, and the
    # ACCEPT_UNUSED a pragma appends lands at the pragma's own line.
    src = (
        'plan "P"\nenvelope 30 x 20\nceiling 9\n'
        "# barndsl: accept ROOM_TIGHT\n"
        "room living: living at 0,0 size 18 x 20\n"
        "room kitchen: kitchen at 18,0 size 12 x 20  # barndsl: accept KITCHEN_FLOW\n"
        "entry living south width 3 offset 6\n"
    )
    result = compile_source(src)
    flow = next(d for d in result.diagnostics if d.code == "KITCHEN_FLOW")
    assert flow.accepted and flow.severity is Severity.INFO
    unused = next(d for d in result.diagnostics if d.code == "ACCEPT_UNUSED")
    assert unused.line == 4 and result.diagnostics[0] is unused
    assert _is_sorted(result.diagnostics)
