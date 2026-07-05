"""Phase 6 — linter ergonomics + remaining IRC rules.

Covers, end to end:

* the `# barndsl: accept CODE "reason"` suppression pragma — trailing + standalone
  placement, downgrade-not-delete (severity/score/flag/reason), error denial,
  unknown code, unused pragma, the packet audit subsection, the playground token;
* `barndsl fmt` — idempotence on every gallery file, byte-exact comment/pragma
  preservation, `--check` exit codes, refusal on parse errors, normalization cases;
* the `tempered` window flag — parse/emit, predicate skip, schedule distinction;
* the `fixed` window flag + `VENT_AREA`, `DOOR_NO_LANDING`, `STAIR_HANDRAIL`,
  `WATER_HEATER_PLACEMENT` — each positive and negative.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from barndsl import compile_source, emit_dsl
from barndsl.cli import main
from barndsl.diagnostics import REGISTRY
from barndsl.fmt import format_source
from barndsl.packet import build_packet
from barndsl.playground import compile_payload
from barndsl.schedule import window_rows
from barndsl.score import design_score
from barndsl.validation import Severity

GALLERY = Path(__file__).resolve().parent.parent / "examples" / "gallery"


def _codes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


def _by_code(result, code):
    return [d for d in result.diagnostics if d.code == code]


# A small plan that raises WINDOW_TEMPERED (a window beside the entry door) and
# is otherwise buildable — the fixture for the suppression-pragma tests.
def _tempered_src(pragma: str = "") -> str:
    trail = f"   {pragma}" if pragma else ""
    return (
        'plan "Acc"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 24 x 20\n"
        "entry a south width 3 offset 12\n"
        f"window a south width 4 offset 8{trail}\n"
    )


# ===========================================================================
# 1. Suppression pragma (`accept`)
# ===========================================================================


def test_registry_has_the_new_pragma_codes():
    assert REGISTRY["ACCEPT_DENIED"].severity is Severity.WARNING
    assert REGISTRY["ACCEPT_UNKNOWN"].severity is Severity.WARNING
    assert REGISTRY["ACCEPT_UNUSED"].severity is Severity.INFO


def test_trailing_pragma_downgrades_not_deletes():
    r = compile_source(_tempered_src('# barndsl: accept WINDOW_TEMPERED "glass block"'))
    hits = _by_code(r, "WINDOW_TEMPERED")
    assert len(hits) == 1
    d = hits[0]
    # Downgraded to an accepted INFO, reason carried, flag set — not deleted.
    assert d.severity is Severity.INFO
    assert d.accepted is True
    assert d.accept_reason == "glass block"
    assert '(accepted: "glass block")' in d.message
    # It no longer counts as a warning.
    assert not any(w.code == "WINDOW_TEMPERED" for w in r.warnings)


def test_standalone_pragma_applies_to_the_following_statement():
    src = (
        'plan "Acc"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 24 x 20\n"
        "entry a south width 3 offset 12\n"
        '# barndsl: accept WINDOW_TEMPERED "standalone form"\n'
        "window a south width 4 offset 8\n"
    )
    r = compile_source(src)
    hits = _by_code(r, "WINDOW_TEMPERED")
    assert len(hits) == 1 and hits[0].accepted
    assert hits[0].accept_reason == "standalone form"


def test_accept_removes_the_score_deduction():
    plain = compile_source(_tempered_src())
    accepted = compile_source(_tempered_src('# barndsl: accept WINDOW_TEMPERED "ok"'))
    # Accepting the warning strictly raises the score (an 8-pt warning stops
    # deducting and doesn't reappear as a 2-pt info either).
    assert design_score(accepted).total > design_score(plain).total
    # The accepted diagnostic counts at NO severity in the score's counts.
    assert design_score(accepted).counts["warning"] < design_score(plain).counts["warning"]


def test_pragma_without_reason_still_downgrades():
    r = compile_source(_tempered_src("# barndsl: accept WINDOW_TEMPERED"))
    d = _by_code(r, "WINDOW_TEMPERED")[0]
    assert d.accepted and d.accept_reason is None
    assert d.message.endswith("(accepted)")


def test_errors_cannot_be_accepted():
    # A bedroom with no egress opening is a BEDROOM_EGRESS *error*.
    src = (
        'plan "E"\n'
        "envelope 30 x 24\n"
        "ceiling 9\n"
        "room bed: bedroom at 0,0 size 12 x 12   # barndsl: accept BEDROOM_EGRESS \"whatever\"\n"
        "room living: living east-of bed size 18 x 12\n"
        "entry living south width 3 offset 4\n"
        "door bed - living width 3\n"
        "window living south width 6 offset 6\n"
    )
    r = compile_source(src)
    egress = _by_code(r, "BEDROOM_EGRESS")
    assert egress and egress[0].severity is Severity.ERROR  # still an error
    assert not egress[0].accepted
    assert "ACCEPT_DENIED" in _codes(r)


def test_unknown_code_is_accept_unknown_with_candidates():
    r = compile_source(_tempered_src('# barndsl: accept WINDOW_TEMPRED "typo"'))
    denied = _by_code(r, "ACCEPT_UNKNOWN")
    assert denied and "WINDOW_TEMPERED" in denied[0].message  # did-you-mean
    # The real WINDOW_TEMPERED still fires (the typo suppressed nothing).
    assert any(not d.accepted for d in _by_code(r, "WINDOW_TEMPERED"))


def test_stale_pragma_is_accept_unused():
    # WINDOW_TEMPERED does not fire on this window (far from the door), so the
    # pragma matches nothing.
    src = (
        'plan "U"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 24 x 20\n"
        "entry a south width 3 offset 2\n"
        'window a south width 4 offset 16   # barndsl: accept WINDOW_TEMPERED "stale"\n'
    )
    r = compile_source(src)
    assert "ACCEPT_UNUSED" in _codes(r)
    assert "WINDOW_TEMPERED" not in _codes(r)


def test_packet_has_an_accepted_deviations_subsection():
    r = compile_source(_tempered_src('# barndsl: accept WINDOW_TEMPERED "glass block"'))
    html = build_packet(r)
    assert "Accepted deviations" in html
    assert "glass block" in html
    assert "WINDOW_TEMPERED" in html


def test_playground_payload_marks_accepted():
    p = compile_payload(_tempered_src('# barndsl: accept WINDOW_TEMPERED "glass block"'))
    acc = [d for d in p["diagnostics"] if d["accepted"]]
    assert acc and acc[0]["code"] == "WINDOW_TEMPERED"
    assert acc[0]["accept_reason"] == "glass block"
    assert acc[0]["severity"] == "info"


# ===========================================================================
# 2. `barndsl fmt`
# ===========================================================================

_MESSY = (
    'Plan   "Messy"\n'
    "ENVELOPE   40 x 30\n"
    "ceiling   9\n"
    "Room  a:   living   at 0,0   size 24 x 20    # a big room\n"
    "entry a south width 12-6 offset 2\n"
)


@pytest.mark.parametrize("path", sorted(GALLERY.glob("*.barn")), ids=lambda p: p.stem)
def test_fmt_is_idempotent_on_gallery(path: Path):
    src = path.read_text()
    once = format_source(src)
    assert format_source(once) == once  # fmt(fmt(x)) == fmt(x)


@pytest.mark.parametrize("path", sorted(GALLERY.glob("*.barn")), ids=lambda p: p.stem)
def test_fmt_preserves_gallery_diagnostics(path: Path):
    src = path.read_text()
    before = sorted(d.code for d in compile_source(src).diagnostics)
    after = sorted(d.code for d in compile_source(format_source(src)).diagnostics)
    assert before == after


def test_fmt_preserves_comments_and_pragmas_byte_exact():
    src = (
        'plan "P"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 24 x 20   # teaching note, kept verbatim\n"
        'window a south width 4 offset 8 tempered   # barndsl: accept WINDOW_TEMPERED "glass block"\n'
    )
    out = format_source(src)
    assert "# teaching note, kept verbatim" in out
    assert '# barndsl: accept WINDOW_TEMPERED "glass block"' in out


def test_fmt_normalizes_spacing_keyword_case_and_ft_in():
    out = format_source(_MESSY)
    lines = out.splitlines()
    assert lines[0] == 'plan "Messy"'            # keyword case
    assert lines[1] == "envelope 40 x 30"        # collapsed run whitespace
    assert lines[3] == "room a: living at 0,0 size 24 x 20 # a big room"
    assert lines[4] == "entry a south width 12.5 offset 2"  # ft-in → decimal feet


def test_fmt_is_idempotent_on_messy_source():
    once = format_source(_MESSY)
    assert format_source(once) == once


def test_fmt_check_exit_codes(tmp_path):
    p = tmp_path / "m.barn"
    p.write_text(_MESSY)
    assert main(["fmt", "--check", str(p)]) == 1     # would reformat
    assert main(["fmt", "--write", str(p)]) == 0     # rewrite in place
    assert main(["fmt", "--check", str(p)]) == 0     # now canonical
    # The rewrite preserved the trailing comment.
    assert "# a big room" in p.read_text()


def test_fmt_refuses_a_file_with_parse_errors(tmp_path, capsys):
    p = tmp_path / "bad.barn"
    p.write_text('plan "X"\nenvelope 10 x 10\nroom z: nope at 0,0 size 5 x 5\n')
    assert main(["fmt", "--check", str(p)]) == 2


# ===========================================================================
# 3. `tempered` window flag
# ===========================================================================


def test_tempered_parses_and_round_trips():
    src = _tempered_src() + ""  # baseline warns
    assert "WINDOW_TEMPERED" in _codes(compile_source(src))
    src_t = (
        'plan "T"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 24 x 20\n"
        "entry a south width 3 offset 12\n"
        "window a south width 4 offset 8 tempered\n"
    )
    r = compile_source(src_t)
    assert r.plan.windows[0].tempered is True
    # emit round-trips the flag.
    emitted = emit_dsl(r.plan)
    assert "tempered" in emitted
    again = compile_source(emitted, name="T")
    assert again.plan.windows[0].tempered is True


def test_tempered_skips_the_warning():
    src = (
        'plan "T"\n'
        "envelope 40 x 30\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 24 x 20\n"
        "entry a south width 3 offset 12\n"
        "window a south width 4 offset 8 tempered\n"
    )
    assert "WINDOW_TEMPERED" not in _codes(compile_source(src))


def test_schedule_distinguishes_declared_from_required():
    required = compile_source(_tempered_src())
    assert window_rows(required.plan)[0]["glazing"] == "tempered (required)"
    declared = compile_source(
        'plan "T"\nenvelope 40 x 30\nceiling 9\n'
        "room a: living at 0,0 size 24 x 20\n"
        "entry a south width 3 offset 12\n"
        "window a south width 4 offset 8 tempered\n"
    )
    assert window_rows(declared.plan)[0]["glazing"] == "tempered (declared)"


# ===========================================================================
# 4a. `fixed` flag + VENT_AREA
# ===========================================================================

# A room with a single window; openable area 4x3.67 = ~14.7 sqft. At 300 sqft
# floor the 4% vent floor is 12 sqft (met when openable) but a FIXED window
# opens nothing, so it fails ventilation while still passing daylight enough not
# to distract. We compare the same geometry openable vs fixed.
def _vent_src(flag: str) -> str:
    return (
        'plan "V"\n'
        "envelope 30 x 24\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 18 x 16\n"
        "entry a south width 3 offset 2\n"
        f"window a south width 8 offset 4 head 8 {flag}\n"
    )


def test_vent_area_fires_for_a_fixed_window():
    r = compile_source(_vent_src("fixed"))
    assert "VENT_AREA" in _codes(r)


def test_vent_area_clear_for_an_openable_window():
    r = compile_source(_vent_src(""))
    assert "VENT_AREA" not in _codes(r)


def test_fixed_flag_and_kind_are_equivalent():
    a = compile_source(_vent_src("fixed"))
    b = compile_source(
        'plan "V"\nenvelope 30 x 24\nceiling 9\n'
        "room a: living at 0,0 size 18 x 16\n"
        "entry a south width 3 offset 2\n"
        "window a south fixed width 8 offset 4 head 8\n"
    )
    assert a.plan.windows[0].kind == "fixed"
    assert b.plan.windows[0].kind == "fixed"
    assert ("VENT_AREA" in _codes(a)) == ("VENT_AREA" in _codes(b))


# ===========================================================================
# 4b. DOOR_NO_LANDING
# ===========================================================================

_LAND_ROOMS = (
    'plan "L"\n'
    "envelope 40 x 24\n"
    "ceiling 9\n"
    "room a: living at 0,0 size 40 x 24\n"
    "window a north width 8 offset 6\n"
)


def test_door_no_landing_is_an_info_nudge_without_porches():
    src = _LAND_ROOMS + "entry a south width 3 offset 8\nentry a east width 3 offset 8\n"
    r = compile_source(src)
    hits = _by_code(r, "DOOR_NO_LANDING")
    # No porches anywhere → a single INFO nudge on the primary entry only.
    assert len(hits) == 1 and hits[0].severity is Severity.INFO


def test_door_no_landing_warns_uncovered_entry_when_porches_exist():
    src = (
        _LAND_ROOMS
        + "entry a south width 3 offset 8\n"
        + "entry a east width 3 offset 8\n"
        + "porch p at 6,-6 size 8 x 6 covered\n"  # covers the south door only
    )
    r = compile_source(src)
    hits = _by_code(r, "DOOR_NO_LANDING")
    assert len(hits) == 1 and hits[0].severity is Severity.WARNING
    assert hits[0].room == "a" and "east" in hits[0].message


def test_door_no_landing_clear_when_all_entries_are_covered():
    src = (
        _LAND_ROOMS
        + "entry a south width 3 offset 8\n"
        + "porch p at 6,-6 size 8 x 6 covered\n"
    )
    r = compile_source(src)
    assert "DOOR_NO_LANDING" not in _codes(r)


def test_door_no_landing_ignores_overhead_doors():
    src = (
        'plan "G"\n'
        "envelope 40 x 24\n"
        "ceiling 9\n"
        "room shop: shop at 0,0 size 40 x 24\n"
        "door shop south overhead width 12 offset 8\n"
        "entry shop east width 3 offset 8\n"
        "window shop north width 8 offset 6\n"
        "porch p at 39,4 size 6 x 8 covered\n"  # covers the east entry
    )
    r = compile_source(src)
    # The overhead door needs no landing; the covered entry is fine → none.
    assert "DOOR_NO_LANDING" not in _codes(r)


# ===========================================================================
# 4c. STAIR_HANDRAIL
# ===========================================================================


def test_stair_handrail_fires_once_on_a_qualifying_flight():
    src = (
        'plan "S"\n'
        "envelope 30 x 24\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 30 x 24\n"
        "room loft: loft at 0,0 size 30 x 24 level 1\n"
        "stair s at 0,2 size 4 x 12 from 0 to 1\n"
        "entry living south width 3 offset 8\n"
        "window living south width 10 offset 2\n"
        "porch p at 6,-6 size 7 x 6 covered\n"
    )
    hits = _by_code(compile_source(src), "STAIR_HANDRAIL")
    assert len(hits) == 1 and hits[0].severity is Severity.INFO
    assert hits[0].line is not None  # anchored to the stair line (accept-able)


def test_stair_handrail_absent_without_a_stair():
    src = (
        'plan "S"\n'
        "envelope 30 x 24\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 30 x 24\n"
        "entry living south width 3 offset 8\n"
        "window living south width 10 offset 2\n"
        "porch p at 6,-6 size 7 x 6 covered\n"
    )
    assert "STAIR_HANDRAIL" not in _codes(compile_source(src))


# ===========================================================================
# 4d. WATER_HEATER_PLACEMENT
# ===========================================================================


def test_water_heater_in_garage_flags():
    src = (
        'plan "W"\n'
        "envelope 40 x 24\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 20 x 24\n"
        "room garage: garage east-of living size 20 x 24\n"
        "entry living south width 3 offset 8\n"
        "door living - garage width 3\n"
        "fixture water_heater in garage\n"
        "window living south width 8 offset 2\n"
        "porch p at 6,-6 size 7 x 6 covered\n"
    )
    hits = _by_code(compile_source(src), "WATER_HEATER_PLACEMENT")
    assert len(hits) == 1 and "garage/shop" in hits[0].message


def test_water_heater_over_habitable_flags():
    src = (
        'plan "W"\n'
        "envelope 30 x 24\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 30 x 24\n"
        "room loft: loft at 0,0 size 30 x 24 level 1\n"
        "stair s at 0,2 size 4 x 12 from 0 to 1\n"
        "entry living south width 3 offset 8\n"
        "window living south width 10 offset 2\n"
        "fixture water_heater in loft\n"
        "porch p at 6,-6 size 7 x 6 covered\n"
    )
    hits = _by_code(compile_source(src), "WATER_HEATER_PLACEMENT")
    assert len(hits) == 1 and "habitable space" in hits[0].message


def test_water_heater_in_ground_utility_is_clean():
    src = (
        'plan "W"\n'
        "envelope 40 x 24\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 24 x 24\n"
        "room util: utility east-of living size 16 x 24\n"
        "entry living south width 3 offset 8\n"
        "door living - util width 3\n"
        "fixture water_heater in util\n"
        "window living south width 8 offset 2\n"
        "porch p at 6,-6 size 7 x 6 covered\n"
    )
    assert "WATER_HEATER_PLACEMENT" not in _codes(compile_source(src))
