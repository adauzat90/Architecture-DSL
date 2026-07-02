"""Tests for window kinds and double/french doors (§2.2).

`window <id> <wall> [casement|slider|fixed|double-hung]` makes egress honest:
the default is **casement** (the one kind whose full glazed size is its clear
opening — exactly the compiler's historical math, so old plans are unchanged),
a `fixed` window is never an escape opening, a slider clears ~half its width and
a double-hung ~half its height. `door ... double|french` (interior and exterior)
is a pair of half-width leaves whose egress clear width counts ONE leaf
(IRC R311.2). Kinds carry through the exchange, schedules, and emit round-trip.
"""

from __future__ import annotations

import pytest

from barndsl import (
    RoomType as T,
    barndominium,
    compile_source,
    emit_dsl,
    exchange_to_plan,
    save_svg,
    to_revit_model,
    validate,
)
from barndsl.elements import DEFAULT_DOUBLE_DOOR_WIDTH
from barndsl.schedule import door_rows, window_rows


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


def _bedroom_plan(window_line: str) -> str:
    return """\
plan "Egress kinds"
envelope 30 x 22
ceiling 9
room living: living  at 0,0  size 18 x 22
room bed:    bedroom at 18,0 size 12 x 22
door living - bed width 2.67
entry living south width 3 offset 6
window living west width 12 offset 4
{window_line}
""".format(window_line=window_line)


# --- window grammar --------------------------------------------------------------


def test_window_kind_parses_and_defaults_to_casement():
    r = compile_source(_bedroom_plan("window bed east slider width 4 offset 4"))
    assert r.plan is not None, r.report()
    kinds = {w.room: w.kind for w in r.plan.windows}
    assert kinds["bed"] == "slider"
    assert kinds["living"] == "casement"  # no keyword -> the default


@pytest.mark.parametrize("kind", ["casement", "slider", "fixed", "double-hung"])
def test_every_window_kind_parses(kind):
    r = compile_source(_bedroom_plan(f"window bed east {kind} width 4 offset 4"))
    assert r.plan is not None, r.report()
    assert r.plan.windows[-1].kind == kind


def test_unknown_window_kind_reads_as_a_bad_option():
    r = compile_source(_bedroom_plan("window bed east hopper width 4 offset 4"))
    assert r.plan is not None and not r.ok  # §1.3: bad line skipped, survivors kept
    (d,) = [d for d in r.errors if d.code == "BAD_OPTION"]
    assert "hopper" in d.message
    assert "casement" in (d.hint or "")


def test_window_kind_round_trips_and_default_stays_terse():
    src = _bedroom_plan("window bed east double-hung width 4 offset 4 head 8")
    r = compile_source(src)
    text = emit_dsl(r.plan)
    assert "window bed east double-hung width 4 offset 4 head 8" in text
    assert "window living west width 12 offset 4" in text  # casement not spelled
    again = compile_source(text)
    assert again.plan is not None and not again.errors, again.report()
    assert emit_dsl(again.plan) == text


def test_builder_add_window_validates_the_kind():
    plan = barndominium("B").envelope(20, 20).ceiling(9)
    with pytest.raises(ValueError):
        plan.add_window("a", "south", kind="hopper")


# --- egress semantics per kind ----------------------------------------------------


def test_fixed_window_is_not_an_escape_opening():
    r = compile_source(_bedroom_plan("window bed east fixed width 4 offset 4"))
    assert "BEDROOM_EGRESS" in _codes(r, "error")
    d = next(d for d in r.errors if d.code == "BEDROOM_EGRESS")
    assert "fixed glass" in d.message
    assert "doesn't open" in (d.hint or "") or "operable" in (d.hint or "")


def test_fixed_glass_still_counts_for_natural_light():
    # The same window as casement passes NAT_LIGHT; fixed must too (it still
    # daylights) — only the egress checks ignore it.
    r = compile_source(_bedroom_plan("window bed east fixed width 12 offset 4"))
    nat = [d for d in r.warnings if d.code == "NAT_LIGHT" and d.room == "bed"]
    assert not nat, r.report()


def test_slider_clears_only_half_its_width():
    # 3 ft casement: clear 3 x 3.67 — passes R310. As a slider only ~1.5 ft
    # (18 in) of width is clear, under the 20 in minimum -> EGRESS_SIZE.
    ok = compile_source(_bedroom_plan("window bed east width 3 offset 4"))
    assert "EGRESS_SIZE" not in _codes(ok, "warning")
    r = compile_source(_bedroom_plan("window bed east slider width 3 offset 4"))
    assert "EGRESS_SIZE" in _codes(r, "warning")
    d = next(d for d in r.warnings if d.code == "EGRESS_SIZE")
    assert "slider" in d.message  # the detail names the kind honestly


def test_double_hung_clears_only_half_its_height():
    # Default glass is 3.67 ft tall: half is 22 in, under the 24 in minimum.
    short = compile_source(_bedroom_plan("window bed east double-hung width 4 offset 4"))
    assert "EGRESS_SIZE" in _codes(short, "warning")
    # A taller double-hung (head 8 -> 5 ft of glass, 2.5 clear) passes.
    tall = compile_source(
        _bedroom_plan("window bed east double-hung width 4 offset 4 head 8")
    )
    assert "EGRESS_SIZE" not in _codes(tall, "warning")


def test_wide_slider_still_satisfies_egress():
    # Honest, not punitive: a 6 ft slider clears ~3 x 3.67 ft — plenty.
    r = compile_source(_bedroom_plan("window bed east slider width 6 offset 3"))
    assert "EGRESS_SIZE" not in _codes(r, "warning")
    assert "BEDROOM_EGRESS" not in _codes(r, "error")


# --- interior double / french doors -----------------------------------------------


def _double_src(door_line: str) -> str:
    return """\
plan "Doubles"
envelope 40 x 24
ceiling 9
room living: living at 0,0  size 22 x 24
room office: office at 22,0 size 18 x 24
{door_line}
entry living south width 3 offset 6
window living south width 10 offset 10
window office east width 8 offset 8
""".format(door_line=door_line)


def test_interior_double_parses_with_the_stock_pair_default():
    r = compile_source(_double_src("door living - office double"))
    assert r.plan is not None, r.report()
    d = r.plan.interior_doors[0]
    assert d.kind == "double" and d.leaf
    assert d.width == pytest.approx(DEFAULT_DOUBLE_DOOR_WIDTH)  # 60 in pair


def test_french_is_a_double_variant_and_round_trips():
    src = _double_src("door living - office french width 6")
    r = compile_source(src)
    assert r.plan.interior_doors[0].kind == "french"
    text = emit_dsl(r.plan)
    assert "door living - office french width 6" in text
    again = compile_source(text)
    assert emit_dsl(again.plan) == text


def test_double_checks_against_stock_pair_widths():
    # 4.5 ft = 54 in — not a stock pair (48/60/64/72) -> DOOR_SIZE info.
    off = compile_source(_double_src("door living - office double width 4.5"))
    assert "DOOR_SIZE" in _codes(off, "info")
    # 5 ft = 60 in — a stock pair, silent (a single-leaf list would call 54 fine).
    ok = compile_source(_double_src("door living - office double width 5"))
    assert "DOOR_SIZE" not in _codes(ok, "info")
    # 4 ft = 48 in — a stock pair, though no stock *single* leaf is 48 in.
    pair48 = compile_source(_double_src("door living - office double width 4"))
    assert "DOOR_SIZE" not in _codes(pair48, "info")


def test_double_renders_two_leaves():
    single = _double_src("door living - office width 3")
    double = _double_src("door living - office double width 6")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        s, d = Path(tmp) / "s.svg", Path(tmp) / "d.svg"
        save_svg(compile_source(single).plan, str(s))
        save_svg(compile_source(double).plan, str(d))
        # Two swing arcs where the single door draws one.
        assert d.read_text().count(" A ") == s.read_text().count(" A ") + 1


# --- exterior double / french doors -----------------------------------------------


def _entry_src(entry_line: str) -> str:
    return """\
plan "Front pair"
envelope 30 x 20
ceiling 9
room living: living at 0,0 size 30 x 20
{entry_line}
window living west width 12 offset 4
""".format(entry_line=entry_line)


def test_entry_double_parses_with_the_stock_pair_default():
    r = compile_source(_entry_src("entry living south double offset 6"))
    assert r.plan is not None, r.report()
    xd = r.plan.exterior_doors[0]
    assert xd.kind == "double"
    assert xd.width == pytest.approx(DEFAULT_DOUBLE_DOOR_WIDTH)


def test_entry_double_round_trips_through_emit():
    src = _entry_src("entry living south double width 6 offset 6")
    r = compile_source(src)
    text = emit_dsl(r.plan)
    assert "entry living south double width 6 offset 6" in text
    again = compile_source(text)
    assert again.plan.exterior_doors[0].kind == "double"
    assert emit_dsl(again.plan) == text


def test_exterior_door_form_accepts_double_too():
    r = compile_source(_entry_src("door living south exterior french width 6 offset 6"))
    assert r.plan is not None, r.report()
    assert r.plan.exterior_doors[0].kind == "french"


def test_egress_door_counts_one_leaf_of_a_double():
    # A 5 ft pair is two 30 in leaves — under the 32 in egress clear width.
    narrow = compile_source(_entry_src("entry living south double width 5 offset 6"))
    assert "EGRESS_DOOR" in _codes(narrow, "warning")
    # A 6 ft pair (two 36 in leaves) satisfies it.
    wide = compile_source(_entry_src("entry living south double width 6 offset 6"))
    assert "EGRESS_DOOR" not in _codes(wide, "warning")
    # And the same 5 ft as a single leaf always did satisfy it.
    single = compile_source(_entry_src("entry living south width 5 offset 6"))
    assert "EGRESS_DOOR" not in _codes(single, "warning")


def test_bedroom_escape_door_counts_one_leaf():
    src = """\
plan "Patio pair"
envelope 30 x 20
ceiling 9
room living: living  at 0,0  size 18 x 20
room bed:    bedroom at 18,0 size 12 x 20
door living - bed width 2.67
entry living south width 3 offset 6
window living west width 12 offset 4
entry bed east {kind} width {w} offset 6 no-egress
"""
    # A 3 ft double = two 18 in leaves, under the 20 in escape width; the
    # bedroom's only opening fails R310 -> EGRESS_SIZE.
    r = compile_source(src.format(kind="double", w=3))
    assert "EGRESS_SIZE" in _codes(r, "warning")
    # A 6 ft french pair (36 in leaves) is a fine escape door.
    ok = compile_source(src.format(kind="french", w=6))
    assert "EGRESS_SIZE" not in _codes(ok, "warning")


def test_builder_entrance_validates_the_kind():
    plan = barndominium("B").envelope(20, 20).ceiling(9)
    with pytest.raises(ValueError):
        plan.entrance("a", "south", kind="dutch")


# --- exchange ---------------------------------------------------------------------


def test_window_kind_carries_through_the_exchange_and_back():
    r = compile_source(_bedroom_plan("window bed east slider width 6 offset 3"))
    model = to_revit_model(r.plan)
    wins = {o.rooms[0]: o for o in model.openings if o.category == "window"}
    assert wins["bed"].kind == "slider"
    assert wins["living"].kind == "casement"
    back = exchange_to_plan(model.to_dict())
    kinds = {w.room: w.kind for w in back.windows}
    assert kinds == {"bed": "slider", "living": "casement"}


def test_old_exchange_documents_import_windows_as_casement():
    r = compile_source(_bedroom_plan("window bed east width 4 offset 4"))
    doc = to_revit_model(r.plan).to_dict()
    for o in doc["openings"]:
        if o["category"] == "window":
            o["kind"] = "window"  # what pre-kind documents wrote
    back = exchange_to_plan(doc)
    assert all(w.kind == "casement" for w in back.windows)


def test_door_kinds_carry_through_the_exchange_and_back():
    src = """\
plan "Kinds"
envelope 40 x 24
ceiling 9
room living: living at 0,0  size 22 x 24
room office: office at 22,0 size 18 x 24
door living - office double width 5
entry living south french width 6 offset 6
window living west width 12 offset 4
window office east width 8 offset 8
"""
    r = compile_source(src)
    model = to_revit_model(r.plan)
    door_kinds = {o.kind for o in model.openings if o.category == "door"}
    assert {"double", "french"} <= door_kinds
    back = exchange_to_plan(model.to_dict())
    assert back.interior_doors[0].kind == "double"
    assert back.interior_doors[0].width == pytest.approx(5)
    assert back.exterior_doors[0].kind == "french"
    assert back.exterior_doors[0].width == pytest.approx(6)
    # The reconstruction re-emits the kinds (a centred door legitimately comes
    # back with its offset made explicit, so full-text equality isn't asserted).
    text = emit_dsl(back)
    assert "door living - office double width 5" in text
    assert "entry living south french width 6 offset 6" in text


def test_single_doors_keep_the_historical_exchange_kinds():
    r = compile_source(_entry_src("entry living south width 3 offset 6"))
    model = to_revit_model(r.plan)
    (door,) = [o for o in model.openings if o.category == "door"]
    assert door.kind == "exterior"


# --- schedules --------------------------------------------------------------------


def test_window_schedule_shows_the_kind():
    r = compile_source(_bedroom_plan("window bed east fixed width 4 offset 4"))
    rows = window_rows(r.plan)
    assert [row["kind"] for row in rows] == ["casement", "fixed"]


def test_door_schedule_shows_double_and_french():
    src = """\
plan "Door sched"
envelope 40 x 24
ceiling 9
room living: living at 0,0  size 22 x 24
room office: office at 22,0 size 18 x 24
door living - office double width 5
entry living south french width 6 offset 6
window living west width 12 offset 4
window office east width 8 offset 8
"""
    r = compile_source(src)
    rows = door_rows(r.plan)
    assert rows[0]["kind"] == "double"
    assert rows[1]["kind"] == "exterior french"


# --- the default keeps old plans untouched ------------------------------------------


def test_default_kind_matches_the_historical_egress_math():
    # The same plan written before window kinds existed must produce the same
    # diagnostics: the casement default's clear opening IS the glazed size.
    src = _bedroom_plan("window bed east width 4 offset 4")
    r = compile_source(src)
    plain = validate(r.plan)
    assert "EGRESS_SIZE" not in {d.code for d in plain.warnings}
    assert "BEDROOM_EGRESS" not in {d.code for d in plain.errors}
    w = r.plan.windows[-1]
    assert w.kind == "casement"
    assert w.clear_opening == (
        pytest.approx(w.width),
        pytest.approx(w.head_height - w.sill_height),
    )
