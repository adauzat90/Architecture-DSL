"""Tests for the `wall` statement: declared shared-wall attributes.

`wall <a> - <b> plumbing|bearing|rated` puts attributes on the wall two abutting
rooms share, closing three loops at once: the validator can *verify* instead of
nudge (WET_GROUP, GARAGE_SEPARATION), the auto frame honours a declared interior
bearing wall as a post line, and the Revit exchange carries a per-wall `kind`
(plus a 2x6 thickness hint for plumbing) so the builder can map named wall types.
"""

from __future__ import annotations

import json

import pytest

from barndsl import (
    RoomType as T,
    barndominium,
    compile_source,
    emit_dsl,
    to_revit_model,
    validate,
)
from barndsl.constants import (
    INTERIOR_WALL_THICKNESS,
    PLUMBING_WALL_THICKNESS,
)
from barndsl.validation import clear_dimensions


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


def _vcodes(plan) -> set[str]:
    return {i.code for i in validate(plan).issues}


def _issue(plan, code):
    return next(i for i in validate(plan).issues if i.code == code)


def _plan(walls: str) -> str:
    # A clean 2-room base; the `wall` lines are injected after `ceiling`.
    return """\
plan "Wall attrs"
envelope 40 x 24
ceiling 9
{walls}
room kitchen: kitchen at 0,0  size 18 x 24
room living:  living  at 18,0 size 22 x 24
door kitchen - living width 3
entry living south width 3 offset 8
window living south width 8 offset 12
window kitchen west width 8 offset 8
""".format(walls=walls)


# --- parsing + round-trip ------------------------------------------------------


def test_wall_statement_parses_onto_the_plan():
    r = compile_source(_plan("wall kitchen - living plumbing"))
    assert r.ok, r.report()
    (ws,) = r.plan.wall_specs
    assert (ws.room_a, ws.room_b, ws.attributes) == ("kitchen", "living", ("plumbing",))


def test_multiple_attributes_dedupe_into_canonical_order():
    r = compile_source(_plan("wall kitchen - living rated bearing rated"))
    assert r.ok, r.report()
    # Stored deduplicated, in WALL_ATTRIBUTES order (plumbing, bearing, rated).
    assert r.plan.wall_specs[0].attributes == ("bearing", "rated")


def test_to_separator_is_accepted():
    r = compile_source(_plan("wall kitchen to living plumbing"))
    assert r.ok, r.report()
    assert r.plan.wall_specs[0].attributes == ("plumbing",)


def test_unknown_attribute_is_a_parse_error():
    r = compile_source(_plan("wall kitchen - living soundproof"))
    assert r.plan is None
    assert "BAD_OPTION" in _codes(r, "error")


def test_missing_attribute_is_a_syntax_error():
    r = compile_source(_plan("wall kitchen - living"))
    assert r.plan is None
    (d,) = [d for d in r.errors if d.code == "SYNTAX"]
    assert "attribute" in d.message


def test_same_room_twice_is_a_syntax_error():
    r = compile_source(_plan("wall kitchen - kitchen plumbing"))
    assert r.plan is None
    assert "SYNTAX" in _codes(r, "error")


def test_wall_round_trips_through_emit_as_a_fixed_point():
    src = _plan("wall kitchen - living plumbing rated")
    r = compile_source(src)
    text = emit_dsl(r.plan)
    assert "wall kitchen - living plumbing rated" in text
    again = compile_source(text)
    assert again.plan is not None and not again.errors, again.report()
    assert emit_dsl(again.plan) == text


# --- reference / adjacency errors -----------------------------------------------


def test_unknown_room_is_a_wall_ref_error():
    r = compile_source(_plan("wall kitchen - pantry plumbing"))
    assert not r.ok
    (d,) = [d for d in r.errors if d.code == "WALL_REF"]
    assert "unknown room 'pantry'" in d.message
    assert d.line == 4  # anchored to the `wall` line


def test_non_adjacent_pair_is_a_wall_noadj_error():
    src = """\
plan "Apart"
envelope 40 x 24
ceiling 9
wall kitchen - bed plumbing
room kitchen: kitchen at 0,0  size 12 x 12
room living:  living  at 12,0 size 16 x 24
room bed:     bedroom at 28,0 size 12 x 24
door kitchen - living width 3
door living - bed width 2.67
entry living south width 3 offset 8
window bed south width 4 offset 4
"""
    r = compile_source(src)
    (d,) = [d for d in r.errors if d.code == "WALL_NOADJ"]
    assert "doesn't exist" in d.message
    assert "don't share a wall" in d.message


def test_cross_level_pair_is_wall_noadj_with_level_detail():
    plan = (
        barndominium("Levels").envelope(30, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=20)
        .add_room("loft", T.LOFT, x=0, y=0, width=30, length=10, level=1)
        .wall("living", "loft", "bearing")
    )
    (d,) = [i for i in validate(plan).errors if i.code == "WALL_NOADJ"]
    assert "different levels" in d.message


# --- the builder mirrors the statement -------------------------------------------


def test_builder_wall_validates_its_arguments():
    plan = barndominium("B").envelope(20, 20).ceiling(9)
    with pytest.raises(ValueError):
        plan.wall("a", "b")  # needs at least one attribute
    with pytest.raises(ValueError):
        plan.wall("a", "b", "soundproof")  # unknown attribute
    with pytest.raises(ValueError):
        plan.wall("a", "a", "plumbing")  # two different rooms


def test_builder_wall_emits_back_to_dsl():
    plan = (
        barndominium("B").envelope(30, 20).ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=15, length=20)
        .add_room("b", T.KITCHEN, x=15, y=0, width=15, length=20)
        .wall("a", "b", "rated", "plumbing")
    )
    assert "wall a - b plumbing rated" in emit_dsl(plan)


# --- plumbing: WET_GROUP verification + WALL_UNUSED ------------------------------


def _scattered_wet() -> "barndominium":
    # Three wet rooms (kitchen, bath, laundry) in a row, none abutting another
    # wet room: kitchen | living | bath | office | laundry.
    return (
        barndominium("Wet").envelope(64, 30).ceiling(9)
        .add_room("kitchen", T.KITCHEN, x=0, y=0, width=15, length=30)
        .add_room("living", T.LIVING, x=15, y=0, width=15, length=30)
        .add_room("bath", T.BATHROOM, x=30, y=0, width=10, length=30)
        .add_room("office", T.OFFICE, x=40, y=0, width=10, length=30)
        .add_room("laundry", T.LAUNDRY, x=50, y=0, width=14, length=30)
    )


def test_scattered_wet_rooms_fire_wet_group_and_hint_the_declaration():
    plan = _scattered_wet()
    assert "WET_GROUP" in _vcodes(plan)
    assert "plumbing" in _issue(plan, "WET_GROUP").hint


def test_declared_plumbing_wall_serving_a_wet_room_satisfies_wet_group():
    plan = _scattered_wet().wall("bath", "living", "plumbing")
    codes = _vcodes(plan)
    assert "WET_GROUP" not in codes
    assert "WALL_UNUSED" not in codes  # the bath backs onto it — it's used


def test_plumbing_wall_serving_no_wet_room_is_unused():
    # A plumbing wall declared between two dry rooms matches no fixtures.
    dry = (
        barndominium("Dry").envelope(30, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=15, length=20)
        .add_room("office", T.OFFICE, x=15, y=0, width=15, length=20)
        .wall("living", "office", "plumbing")
    )
    codes = _vcodes(dry)
    assert "WALL_UNUSED" in codes
    assert "serves no wet room" in _issue(dry, "WALL_UNUSED").message


def test_bogus_plumbing_declaration_does_not_satisfy_wet_group():
    # Declared on a NON-adjacent pair: WALL_NOADJ owns the error, and the wet
    # grouping nudge still fires (the declaration doesn't match reality).
    plan = _scattered_wet().wall("bath", "kitchen", "plumbing")
    codes = _vcodes(plan)
    assert "WALL_NOADJ" in codes
    assert "WET_GROUP" in codes


# --- plumbing: clear-dimension math ----------------------------------------------


def test_plumbing_wall_thickens_the_flanking_rooms_clear_dimensions():
    base = _scattered_wet()
    bath = base.room("bath")
    before_w, before_l = clear_dimensions(base, bath)
    base.wall("bath", "living", "plumbing")  # the bath's WEST wall
    after_w, after_l = clear_dimensions(base, bath)
    delta = (PLUMBING_WALL_THICKNESS - INTERIOR_WALL_THICKNESS) / 2.0
    assert after_l == before_l  # north/south untouched
    assert before_w - after_w == pytest.approx(delta)
    # The dry room on the other side pays the same half.
    living = base.room("living")
    lw, _ = clear_dimensions(base, living)
    assert lw == pytest.approx(15 - INTERIOR_WALL_THICKNESS / 2 - PLUMBING_WALL_THICKNESS / 2)


# --- rated: the garage-separation reminder becomes verifiable ---------------------


def _garage_and_mudroom():
    return (
        barndominium("x").envelope(40, 20).ceiling(9)
        .add_room("garage", T.GARAGE, x=0, y=0, width=20, length=20)
        .add_room("mud", T.MUDROOM, x=20, y=0, width=8, length=20)
        .add_room("living", T.LIVING, x=28, y=0, width=12, length=20)
        .connect("garage", "mud", width=3)
        .connect("mud", "living", width=6)
    )


def test_separation_reminder_tells_the_author_to_declare_the_rated_wall():
    plan = _garage_and_mudroom()
    hint = _issue(plan, "GARAGE_SEPARATION").hint
    assert "wall garage - mud rated" in hint


def test_declared_rated_wall_silences_the_separation_reminder():
    plan = _garage_and_mudroom().wall("garage", "mud", "rated")
    assert "GARAGE_SEPARATION" not in _vcodes(plan)


def test_rated_declaration_covers_either_naming_order():
    plan = _garage_and_mudroom().wall("mud", "garage", "rated")
    assert "GARAGE_SEPARATION" not in _vcodes(plan)


def test_habitable_space_above_still_reminds_despite_a_rated_wall():
    # A rated *wall* can't be the garage *ceiling* — the Type X reminder stays.
    plan = (
        barndominium("x").envelope(28, 20).ceiling(9)
        .add_room("garage", T.GARAGE, x=0, y=0, width=20, length=20)
        .add_room("mud", T.MUDROOM, x=20, y=0, width=8, length=20)
        .add_room("bonus", T.LOFT, x=0, y=0, width=20, length=20, level=1)
        .wall("garage", "mud", "rated")
    )
    sep = _issue(plan, "GARAGE_SEPARATION")
    assert "above" in sep.message and "Type X" in sep.message


def test_rated_wall_elsewhere_draws_no_complaint():
    plan = (
        barndominium("x").envelope(30, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=20)
        .add_room("kit", T.KITCHEN, x=20, y=0, width=10, length=20)
        .wall("living", "kit", "rated")
    )
    codes = _vcodes(plan)
    assert "WALL_UNUSED" not in codes
    assert "WALL_NOADJ" not in codes and "WALL_REF" not in codes


# --- bearing: the frame honours the wall as a post line ---------------------------


def _bearing_src(wall_line: str, frame: str = "frame bay 12 span 40") -> str:
    # 60 x 40 envelope, two 60 x 20 bands sharing the horizontal wall y=20 —
    # a wall running along the long (x) axis, exactly a post line's direction.
    return """\
plan "Bearing"
envelope 60 x 40
ceiling 10
{wall_line}
{frame}
room shop:  shop   at 0,0  size 60 x 20
room great: living at 0,20 size 60 x 20
door shop - great width 3
entry great north width 3 offset 6
window great north width 20 offset 20
door shop south overhead width 10 offset 4
entry shop south width 3 offset 40
""".format(wall_line=wall_line, frame=frame)


def _interior_posts_on(plan, *, y=None, x=None):
    posts = [p for p in plan.posts if p.role == "interior"]
    if y is not None:
        return [p for p in posts if abs(p.y - y) < 1e-6]
    return [p for p in posts if abs(p.x - x) < 1e-6]


def test_bearing_wall_becomes_an_interior_post_line():
    without = compile_source(_bearing_src(""))
    assert without.plan is not None
    assert not _interior_posts_on(without.plan, y=20)  # span 40 needs no support

    r = compile_source(_bearing_src("wall shop - great bearing"))
    assert r.plan is not None, r.report()
    line = _interior_posts_on(r.plan, y=20)
    # An interior post at every *interior* bent crossing the wall; at the two
    # gable ends a perimeter post already stands at (0,20)/(60,20) and wins the
    # dedupe, so the beam bears there too.
    assert sorted(round(p.x, 4) for p in line) == [12, 24, 36, 48]
    perimeter = {(round(p.x, 4), round(p.y, 4)) for p in r.plan.posts if p.role == "post"}
    assert (0, 20) in perimeter and (60, 20) in perimeter
    assert "WALL_BEARING_AXIS" not in {d.code for d in r.diagnostics}


def test_bearing_post_line_dedupes_with_the_auto_interior_line():
    # span 20 puts an auto interior line at y=20 already; the declaration must
    # not double the posts there.
    auto = compile_source(_bearing_src("", frame="frame bay 12 span 20"))
    declared = compile_source(
        _bearing_src("wall shop - great bearing", frame="frame bay 12 span 20")
    )
    assert len(auto.plan.posts) == len(declared.plan.posts)


def test_bearing_wall_across_the_span_gets_an_axis_info():
    # Two side-by-side rooms share the vertical wall x=30 — parallel to the
    # bents, so it can't split their span: info, and no posts on it.
    src = """\
plan "Across"
envelope 60 x 40
ceiling 10
wall west_bay - east_bay bearing
frame bay 12 span 40
room west_bay: shop   at 0,0  size 30 x 40
room east_bay: living at 30,0 size 30 x 40
door west_bay - east_bay width 3
entry east_bay south width 3 offset 10
window east_bay south width 16 offset 14
entry west_bay south width 3 offset 10
"""
    r = compile_source(src)
    assert r.plan is not None, r.report()
    (d,) = [d for d in r.infos if d.code == "WALL_BEARING_AXIS"]
    assert "parallel to the bents" in d.message
    assert d.line == 4  # anchored to the `wall` statement
    assert not _interior_posts_on(r.plan, x=30)


def test_bearing_wall_without_a_frame_is_silent():
    r = compile_source(_bearing_src("wall shop - great bearing", frame=""))
    assert r.plan is not None, r.report()
    assert "WALL_BEARING_AXIS" not in {d.code for d in r.diagnostics}


# --- exchange: per-wall kind + plumbing thickness hint ----------------------------


def test_exchange_tags_the_declared_wall_segments_with_a_kind():
    plan = _scattered_wet().wall("bath", "living", "plumbing")
    model = to_revit_model(plan)
    tagged = [w for w in model.walls if w.kind == "plumbing"]
    assert tagged, "no wall segment carried the plumbing kind"
    for w in tagged:
        assert w.orientation == "v" and abs(w.const_coord - 30) < 1e-6
        assert not w.exterior
        assert w.thickness == pytest.approx(PLUMBING_WALL_THICKNESS)
    # Undeclared walls carry no kind — and no `kind` key in the JSON at all.
    doc = json.loads(model.to_json())
    for w in doc["walls"]:
        if w.get("kind") is None:
            assert "kind" not in w
        else:
            assert w["kind"] == "plumbing"


def test_exchange_kind_precedence_is_rated_then_bearing_then_plumbing():
    plan = (
        barndominium("P").envelope(30, 20).ceiling(9)
        .add_room("a", T.KITCHEN, x=0, y=0, width=15, length=20)
        .add_room("b", T.BATHROOM, x=15, y=0, width=15, length=20)
        .wall("a", "b", "plumbing", "bearing", "rated")
    )
    model = to_revit_model(plan)
    tagged = [w for w in model.walls if w.kind is not None]
    assert tagged and all(w.kind == "rated" for w in tagged)
    # The plumbing attribute still carries the thickness hint.
    assert all(w.thickness == pytest.approx(PLUMBING_WALL_THICKNESS) for w in tagged)


def test_exchange_without_wall_specs_is_unchanged():
    plan = _scattered_wet()
    doc = json.loads(to_revit_model(plan).to_json())
    assert all("kind" not in w for w in doc["walls"])


# --- registry ---------------------------------------------------------------------


def test_wall_codes_are_registered():
    from barndsl.diagnostics import REGISTRY

    assert REGISTRY["WALL_REF"].severity.value == "error"
    assert REGISTRY["WALL_NOADJ"].severity.value == "error"
    assert REGISTRY["WALL_UNUSED"].severity.value == "info"
    assert REGISTRY["WALL_BEARING_AXIS"].severity.value == "info"
