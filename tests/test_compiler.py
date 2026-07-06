"""Tests for the DSL compiler: parsing, diagnostics, hints, and round-trip."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

from barndsl import compile_file, compile_source, emit_dsl, render_svg

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "cedar_ridge.barn")


def test_example_file_compiles_clean():
    result = compile_file(EXAMPLE)
    assert result.ok, result.report()
    assert result.plan is not None
    assert result.plan.metrics()["bedroom_count"] == 3


def test_roundtrip_python_to_dsl_to_plan():
    from simple_barndo import build_example

    plan = build_example()
    source = emit_dsl(plan)
    result = compile_source(source)
    assert result.ok, result.report()
    # Re-emitted plan matches the original on key metrics.
    a, b = plan.metrics(), result.plan.metrics()
    assert a["assigned_sqft"] == b["assigned_sqft"]
    assert a["bedroom_count"] == b["bedroom_count"]
    assert len(plan.rooms) == len(result.plan.rooms)


def test_syntax_error_stops_compilation_with_location():
    src = "plan \"X\"\nenvelope 40 x\nroom a: living at 0,0 size 10 x 10\n"
    result = compile_source(src)
    assert not result.ok
    # Statement-level recovery (§1.3): the bad `envelope` line is skipped and its
    # error recorded, but the surviving `room` still builds a partial plan so the
    # continuous score gradient isn't lost. The result stays failed.
    assert result.plan is not None
    assert result.plan.room("a") is not None  # the survivor is present
    err = result.errors[0]  # the parse error comes first, unchanged
    assert err.line == 2
    assert err.code in ("BAD_NUMBER", "SYNTAX")


def test_unknown_room_type_is_flagged_with_hint():
    src = "envelope 20 x 20\nroom a: lounge at 0,0 size 10 x 10\n"
    result = compile_source(src)
    bad = [d for d in result.errors if d.code == "BAD_TYPE"]
    assert bad and bad[0].line == 2
    assert "living" in (bad[0].hint or "")


def test_unknown_statement_is_flagged():
    # `wall` became a real statement, so use a keyword that stays unknown.
    result = compile_source("envelope 20 x 20\nfence a north\n")
    assert any(d.code == "UNKNOWN_STMT" and d.line == 2 for d in result.errors)


def test_semantic_overlap_has_line_and_hint():
    src = (
        'plan "Overlap"\n'
        "envelope 40 x 30\n"
        "room a: living at 0,0 size 20 x 20\n"
        "room b: bedroom at 10,0 size 15 x 12\n"
        "entry a south width 3 offset 2\n"
        "window b south width 4 offset 2\n"
        "door a - b width 3\n"
    )
    result = compile_source(src)
    overlaps = [d for d in result.diagnostics if d.code == "OVERLAP"]
    assert overlaps
    assert overlaps[0].line is not None  # mapped back to a room's source line
    assert overlaps[0].hint and "move" in overlaps[0].hint.lower()


def test_unreachable_room_hint_suggests_a_door():
    src = (
        "envelope 40 x 20\n"
        "room living: living at 0,0 size 20 x 20\n"
        "room office: office at 20,0 size 20 x 20\n"
        "entry living south width 3 offset 2\n"
        "window office east width 6 offset 4\n"
    )
    result = compile_source(src)
    na = [d for d in result.diagnostics if d.code == "NO_ACCESS"]
    assert na and na[0].room == "office"
    assert "door office - living" in (na[0].hint or "")


def test_syntax_error_has_column_accurate_caret():
    src = "plan \"X\"\nroom a: lounge at 0,0 size 10 x 10\n"
    result = compile_source(src)
    bad = [d for d in result.errors if d.code == "BAD_TYPE"][0]
    # The bad token 'lounge' starts at column 9 and spans 6 chars.
    assert bad.col == 9 and bad.end_col == 15
    report = result.report("bad.barn")
    assert "bad.barn:2:9:" in report  # location carries the column
    caret_line = next(ln for ln in report.splitlines() if set(ln.strip()) <= {"^", "~"})
    # caret sits under the token (4-space indent + 8 leading spaces -> ^ at col 9)
    assert caret_line == "    " + " " * 8 + "^" + "~" * 5


def test_semantic_diagnostic_caret_points_at_room_id():
    src = (
        "envelope 40 x 20\n"
        "room living: living at 0,0 size 20 x 20\n"
        "room office: office at 20,0 size 20 x 20\n"
        "entry living south width 3 offset 2\n"
        "window office east width 6 offset 4\n"
    )
    result = compile_source(src)
    na = [d for d in result.diagnostics if d.code == "NO_ACCESS"][0]
    assert na.line == 3  # the `room office:` line
    assert na.col == 6 and na.end_col == 12  # caret under 'office'
    assert ":3:6:" in result.report("p.barn")


def test_design_quality_emits_info_without_blocking():
    src = (
        "envelope 40 x 20\n"
        "room living: living at 0,0 size 20 x 20\n"
        "room bed: bedroom at 20,0 size 20 x 20\n"
        "door living - bed width 3\n"  # bedroom opens straight onto living
        "entry living south width 3 offset 2\n"
        "window bed east width 6 offset 4\n"
    )
    result = compile_source(src)
    privacy = [d for d in result.diagnostics if d.code == "BED_PRIVACY"]
    assert privacy and privacy[0].severity.value == "info"
    assert privacy[0].room == "bed"
    # INFO is advisory — the plan still compiles clean (no errors).
    assert result.ok


def test_clean_example_has_no_design_info():
    result = compile_file(EXAMPLE)
    codes = {d.code for d in result.diagnostics}
    assert not ({"KITCHEN_FLOW", "BED_PRIVACY", "BATH_DISTANCE"} & codes)


def test_compiled_plan_renders():
    result = compile_file(EXAMPLE)
    svg = render_svg(result.plan)
    assert svg.startswith("<svg") and "Cedar Ridge" in svg


# --- Phase 14: feet-and-inches input teaching + did-you-mean ------------------


def _errors(src):
    return [d for d in compile_source(src).diagnostics if d.severity.value == "error"]


def _one(src, code):
    hits = [d for d in _errors(src) if d.code == code]
    assert hits, f"expected a {code} error in:\n{src}"
    return hits[0]


def test_ftin_ascii_inch_mark_teaches_dash_form():
    # `12'6"` — the ASCII " opens a string literal; the targeted hint translates
    # the user's input to an accepted feet-and-inches form instead of a bare
    # "unterminated string".
    d = _one('plan "x"\nenvelope 40 x 30\nroom a: living at 0,0 size 12\'6" x 14\n',
             "UNTERMINATED_STRING")
    assert d.hint == (
        "Feet-and-inches uses a dash or unicode marks — write `12-6` or `12′6″`, "
        "not `12'6\"` (the ASCII `\"` opens a string literal)."
    )


def test_ftin_words_teaches_single_token_form():
    # `12 feet 6 inches` — spelled out; the SYNTAX error's hint translates it.
    d = _one('plan "x"\nenvelope 40 x 30\nroom a: living at 0,0 size 12 feet 6 inches x 14\n',
             "SYNTAX")
    assert d.hint == (
        "Feet-and-inches is one token — write `12-6` (or `12′6″`), e.g. "
        "`size 12-6 x <length>`. `12 feet 6 inches` is several tokens the parser "
        "can't read as one length."
    )


def test_valid_ftin_still_parses_unchanged():
    # The teaching must never change what a valid source does.
    r = compile_source(
        'plan "x"\nenvelope 40 x 30\nceiling 9\n'
        "room a: living at 0,0 size 12-6 x 14\n"
        "entry a south width 3 offset 2\n"
    )
    assert not r.recovered and r.plan is not None
    assert abs(r.plan.room("a").width - 12.5) < 1e-6


def test_bad_type_prepends_did_you_mean():
    d = _one('plan "x"\nenvelope 40 x 30\nroom a: livingroom at 0,0 size 12 x 14\n',
             "BAD_TYPE")
    assert d.message.startswith("Did you mean `living`?")
    # the full list stays in the hint
    assert "kitchen" in d.hint and "bedroom" in d.hint


def test_unknown_stmt_prepends_did_you_mean():
    d = _one('plan "x"\nenvelope 40 x 30\nrom a: living at 0,0 size 12 x 14\n',
             "UNKNOWN_STMT")
    assert "Did you mean `room`" in d.message
    assert "room" in d.hint


def test_unknown_stmt_no_match_has_no_did_you_mean():
    # A word with no close keyword just names it — no misleading suggestion.
    d = _one('plan "x"\nenvelope 40 x 30\nqwertyxyz a b c\n', "UNKNOWN_STMT")
    assert "Did you mean" not in d.message
