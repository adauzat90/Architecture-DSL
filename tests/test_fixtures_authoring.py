"""Authored fixtures & furnishings: the `fixture` statement, add/replace-seed
semantics, the teaching warnings, and the flow into the exchange / plan drawing /
3D scene / edit engine / playground payload.

The seed placement itself lives in :mod:`test_fixtures`; this pins the *authoring*
surface that lets a designer place furniture and drag it around.
"""

from barndsl import RoomType as T
from barndsl import barndominium, to_revit_model, validate
from barndsl.compiler import compile_source
from barndsl.edits import apply_edit, edit_from_json
from barndsl.fixtures import FIXTURES, FIXTURE_KINDS, resolve_room_fixtures
from barndsl.gltf import build_scene, to_glb
from barndsl.playground import compile_payload
from barndsl.render import render_svg


def _codes(src):
    return {(i.code, i.severity.value) for i in compile_source(src).diagnostics}


def _plan(*fixtures):
    body = "\n".join(fixtures)
    return (
        'plan "T"\n'
        "envelope 40 x 22\n"
        "ceiling 9\n"
        "room bed: bedroom at 0,0 size 16 x 12\n"
        "room bath: bathroom at 16,0 size 8 x 12\n"
        "room laundry: laundry at 24,0 size 8 x 12\n"
        "room rest: living at 32,0 size 8 x 22\n"
        "entry rest south width 3 offset 2\n"
        + (body + "\n" if body else "")
    )


# --- catalog -----------------------------------------------------------------


def test_catalog_entries_are_sane():
    for kind in ("bed_queen", "sofa", "dining_table", "washer", "kitchen_island",
                 "counter", "wardrobe", "desk"):
        spec = FIXTURES[kind]
        assert spec.width > 0 and spec.depth > 0 and spec.front >= 0
    assert FIXTURES["bed_queen"].width == 5.0 and FIXTURES["bed_queen"].depth == 6.67
    # free-standing pieces are flagged; a counter is resizable.
    assert FIXTURES["dining_table"].free and FIXTURES["kitchen_island"].free
    assert FIXTURES["counter"].resizable
    assert set(FIXTURE_KINDS) == set(FIXTURES)
    # the original seven are untouched.
    for kind in ("toilet", "lavatory", "tub", "shower", "sink", "range", "refrigerator"):
        assert kind in FIXTURES


def test_laundry_seeds_washer_and_dryer_only():
    from barndsl.fixtures import fixtures_for

    assert fixtures_for(T.LAUNDRY) == ["washer", "dryer"]
    assert fixtures_for(T.BEDROOM) == []  # bedrooms stay unseeded
    assert fixtures_for(T.LIVING) == []


# --- DSL parsing: every form + errors ----------------------------------------


def test_parse_all_fixture_forms():
    src = _plan(
        "fixture bed_queen in bed at 2,3",           # at
        "fixture dresser in bed wall S",             # wall
        "fixture sofa in rest wall N rotate 90",     # wall + rotate
        "fixture counter in rest at 0,0 width 5",    # width override
        "fixture dining_table in rest",              # bare (free-standing)
    )
    r = compile_source(src)
    assert r.plan is not None
    kinds = [f.kind for f in r.plan.fixtures]
    assert kinds == ["bed_queen", "dresser", "sofa", "counter", "dining_table"]
    bed = r.plan.fixtures[0]
    assert (bed.x, bed.y) == (2.0, 3.0)
    assert r.plan.fixtures[2].rotation == 90.0
    assert r.plan.fixtures[3].width == 5.0


def test_parse_errors():
    assert ("BAD_OPTION", "error") in _codes(_plan("fixture zorp in bed"))  # unknown kind
    assert ("SYNTAX", "error") in _codes(_plan("fixture bed_queen bed"))    # missing `in`
    assert ("BAD_WALL", "error") in _codes(_plan("fixture bed_queen in bed wall Q"))
    assert ("BAD_OPTION", "error") in _codes(_plan("fixture bed_queen in bed sideways"))


def test_wall_accepts_letters_and_names():
    r = compile_source(_plan("fixture bed_queen in bed wall n"))
    assert r.plan.fixtures[0].wall.value == "north"
    r = compile_source(_plan("fixture bed_queen in bed wall north"))
    assert r.plan.fixtures[0].wall.value == "north"


# --- wall + offset: the along-wall pin (the door/window convention) -----------
# `wall <W> offset <n>` pins a piece n ft along the wall from its S/W start
# corner — the same convention as a door, window, outlet or switch. Added
# because the design agent kept (reasonably) inventing exactly this syntax.


def test_offset_pins_the_piece_along_each_wall():
    # bed is at 0,0 size 16 x 12, so its walls run x 0..16 and y 0..12.
    r = compile_source(_plan(
        "fixture dresser in bed wall S offset 3",
        "fixture desk in bed wall N offset 2",
        "fixture wardrobe in bed wall W offset 4",
        "fixture bed_queen in bed wall E offset 1",
    ))
    assert r.plan is not None
    by_kind = {f.kind: f for f in resolve_room_fixtures(r.plan, r.plan.room("bed"))}
    dresser = by_kind["dresser"]  # 5 x 1.67, south wall, 3 ft from the west corner
    assert (dresser.x, dresser.y) == (3.0, 0.0)
    assert (dresser.width, dresser.length) == (5.0, 1.67)
    desk = by_kind["desk"]  # 4 x 2, north wall: y = 12 - depth
    assert (desk.x, desk.y) == (2.0, 10.0)
    ward = by_kind["wardrobe"]  # 4 x 2, west wall: depth runs in x, width in y
    assert (ward.x, ward.y) == (0.0, 4.0)
    assert (ward.width, ward.length) == (2.0, 4.0)
    bedq = by_kind["bed_queen"]  # 5 x 6.67, east wall: x = 16 - depth
    assert (bedq.x, bedq.y) == (16.0 - 6.67, 1.0)


def test_offset_grammar_errors():
    # offset without a wall - nothing to measure along.
    assert ("BAD_OPTION", "error") in _codes(_plan("fixture desk in bed offset 3"))
    # offset and `at` both pin the position - mutually exclusive.
    assert ("BAD_OPTION", "error") in _codes(
        _plan("fixture desk in bed at 2,2 wall S offset 3")
    )
    # a negative offset is nonsense.
    assert ("BAD_OPTION", "error") in _codes(
        _plan("fixture desk in bed wall S offset -1")
    )


def test_offset_past_the_room_warns_oob():
    # desk is 4 ft wide; offset 14 on a 16 ft wall runs 2 ft past the room.
    assert ("FIXTURE_OOB", "warning") in _codes(
        _plan("fixture desk in bed wall S offset 14")
    )


def test_offset_round_trips_through_emit_dsl():
    from barndsl.emit import emit_dsl

    r = compile_source(_plan("fixture desk in bed wall N offset 2"))
    out = emit_dsl(r.plan)
    assert "fixture desk in bed wall N offset 2" in out
    r2 = compile_source(out)
    d1 = next(f for f in resolve_room_fixtures(r.plan, r.plan.room("bed"))
              if f.kind == "desk")
    d2 = next(f for f in resolve_room_fixtures(r2.plan, r2.plan.room("bed"))
              if f.kind == "desk")
    assert (d1.x, d1.y, d1.width, d1.length) == (d2.x, d2.y, d2.width, d2.length)


# --- wall auto-slot: beds and sofas centre on a clear wall ---------------------


def test_wall_slotted_bed_centres_on_a_clear_wall():
    # A bed reads best centred (nightstand room both sides), not parked in the
    # first corner the walk reaches.
    from barndsl.validation import clear_box

    r = compile_source(_plan("fixture bed_queen in bed wall N"))
    bed_room = r.plan.room("bed")
    x0, _y0, cw, _cl = clear_box(r.plan, bed_room)
    f = next(x for x in resolve_room_fixtures(r.plan, bed_room)
             if x.kind == "bed_queen")
    assert f.wall == "N"
    assert abs((f.x + f.width / 2.0) - (x0 + cw / 2.0)) < 1e-6


def test_centred_bed_falls_back_to_the_walk_when_blocked():
    from barndsl.fixtures import _rects_overlap

    r = compile_source(_plan(
        "fixture dresser in bed wall N offset 6",  # parked over the wall's middle
        "fixture bed_queen in bed wall N",
    ))
    fixtures = resolve_room_fixtures(r.plan, r.plan.room("bed"))
    dresser = next(f for f in fixtures if f.kind == "dresser")
    bed = next(f for f in fixtures if f.kind == "bed_queen")
    assert bed.wall == "N"
    assert not _rects_overlap(
        (bed.x, bed.y, bed.width, bed.length),
        (dresser.x, dresser.y, dresser.width, dresser.length),
    )


# --- add-vs-replace seed semantics -------------------------------------------


def test_explicit_adds_to_seeds_but_replaces_its_own_kind():
    # an explicit toilet moves the toilet; the lavatory/tub seeds stay; a desk adds.
    r = compile_source(_plan(
        "fixture toilet in bath at 1,1",
        "fixture desk in bath at 4,4",
    ))
    bath = r.plan.room("bath")
    fixtures = resolve_room_fixtures(r.plan, bath)
    kinds = sorted(f.kind for f in fixtures)
    assert kinds == ["desk", "lavatory", "toilet", "tub"]  # one toilet, not two
    toilet = next(f for f in fixtures if f.kind == "toilet")
    assert not toilet.seed  # the authored one replaced the seed
    assert next(f for f in fixtures if f.kind == "tub").seed  # seed survives


def test_ids_are_positional_and_stable():
    r = compile_source(_plan("fixture bed_queen in bed at 2,3"))
    bed = r.plan.room("bed")
    ids = {f.id for f in resolve_room_fixtures(r.plan, bed)}
    assert "bed~bed_queen~0" in ids


# --- teaching warnings -------------------------------------------------------


def test_out_of_room_warns():
    codes = _codes(_plan("fixture bed_queen in bed at 14,10"))  # runs off a 16x12 room
    assert ("FIXTURE_OOB", "warning") in codes


def test_overlap_warns():
    codes = _codes(_plan(
        "fixture dresser in bed at 1,1",
        "fixture desk in bed at 1,1",
    ))
    assert ("FIXTURE_OVERLAP", "warning") in codes


def test_unknown_room_errors():
    codes = _codes(_plan("fixture bed_queen in nowhere"))
    assert ("FIXTURE_ROOM", "error") in codes


def test_a_fitting_fixture_is_silent():
    codes = _codes(_plan("fixture bed_queen in bed wall N"))
    fixture_codes = {c for c, _ in codes if c.startswith("FIXTURE")}
    assert not fixture_codes


# --- exchange / render / scene / determinism ---------------------------------


def test_exchange_carries_explicit_fixtures_with_levels():
    src = (
        'plan "T"\nenvelope 30 x 20\nceiling 9\n'
        "room bed: bedroom at 0,0 size 16 x 12 level 1\n"
        "room rest: living at 16,0 size 14 x 20\n"
        "fixture bed_queen in bed at 2,3\n"
    )
    plan = compile_source(src).plan
    fx = [f for f in to_revit_model(plan).to_dict()["fixtures"] if f["room"] == "bed"]
    assert len(fx) == 1
    bed = fx[0]
    assert bed["kind"] == "bed_queen" and bed["level"] == 1
    assert bed["seed"] is False and bed["source_line"] == 6


def test_plan_svg_has_fixture_glyphs():
    plan = compile_source(_plan("fixture bed_queen in bed wall N")).plan
    svg = render_svg(plan)
    assert 'data-fixture="bed~bed_queen~0"' in svg
    # the seeded bath/laundry fixtures render too.
    assert 'data-fixture="bath~toilet~0"' in svg
    assert 'data-fixture="laundry~washer~0"' in svg


def test_scene_has_fixtures_layer_with_materials():
    from barndsl.viewer import scene_json

    plan = compile_source(_plan("fixture bed_queen in bed wall N")).plan
    scene = scene_json(build_scene(plan))
    assert "fixtures" in scene["layers"]
    fixture_nodes = [n for n in scene["nodes"] if n["layer"] == "fixtures"]
    assert fixture_nodes
    names = {tuple(round(c, 3) for c in n["color"]) for n in fixture_nodes}
    assert len(names) > 1  # porcelain/stainless/fabric/wood are distinct


def test_glb_is_deterministic_with_fixtures():
    plan = compile_source(_plan(
        "fixture bed_queen in bed wall N",
        "fixture sofa in rest wall S",
    )).plan
    assert to_glb(plan) == to_glb(plan)


def test_ifc_emits_furnishing_elements():
    from barndsl.ifc import to_ifc

    plan = compile_source(_plan("fixture bed_queen in bed wall N")).plan
    ifc = to_ifc(plan)
    assert "IFCFURNISHINGELEMENT" in ifc
    assert to_ifc(plan) == ifc  # deterministic


# --- edit engine: move + materialise -----------------------------------------


def test_move_fixture_rewrites_at_on_one_line():
    src = _plan("fixture bed_queen in bed at 2,3")
    e = edit_from_json({"kind": "move_fixture", "key": "bed~bed_queen~0", "x": 5, "y": 4})
    res = apply_edit(src, e)
    assert res.ok and res.changed
    lines_before, lines_after = src.split("\n"), res.source.split("\n")
    assert len(lines_after) == len(lines_before)  # no line inserted
    changed = [i for i in range(len(lines_after)) if lines_after[i] != lines_before[i]]
    assert len(changed) == 1
    assert "at 5,4" in lines_after[changed[0]]


def test_move_fixture_converts_an_offset_pin_to_at():
    # Dragging a `wall N offset 2` piece rewrites its pin as `at x,y` and drops
    # the offset clause - `at` and `offset` are mutually exclusive, so leaving
    # both would make the edited line uncompilable.
    src = _plan("fixture desk in bed wall N offset 2")
    e = edit_from_json({"kind": "move_fixture", "key": "bed~desk~0", "x": 5, "y": 4})
    res = apply_edit(src, e)
    assert res.ok and res.changed
    line = res.source.split("\n")[res.line - 1]
    assert "at 5,4" in line and "offset" not in line and "wall N" in line
    assert compile_source(res.source).plan is not None  # still parses


def test_move_fixture_seed_is_not_editable():
    src = _plan()  # bath toilet is a seed
    e = edit_from_json({"kind": "move_fixture", "key": "bath~toilet~0", "x": 1, "y": 1})
    res = apply_edit(src, e)
    assert not res.ok and res.error.kind == "not_editable"


def test_add_fixture_materialises_a_seed_as_one_new_line():
    src = _plan()
    e = edit_from_json({"kind": "add_fixture", "room": "bath", "fkind": "toilet",
                        "wall": "S", "x": 1, "y": 1})
    res = apply_edit(src, e)
    assert res.ok and res.changed
    assert len(res.source.split("\n")) == len(src.split("\n")) + 1  # exactly one inserted
    assert "fixture toilet in bath at 1,1 wall S" in res.source
    # and it re-compiles to a real, authored fixture (replacing the seed).
    plan = compile_source(res.source).plan
    toilet = next(f for f in resolve_room_fixtures(plan, plan.room("bath"))
                  if f.kind == "toilet")
    assert not toilet.seed


# --- playground payload ------------------------------------------------------


def test_payload_carries_a_fixtures_array():
    payload = compile_payload(_plan("fixture bed_queen in bed at 2,3"))
    fixtures = payload["fixtures"]
    assert fixtures
    bed = next(f for f in fixtures if f["kind"] == "bed_queen")
    assert bed["id"] == "bed~bed_queen~0"
    assert bed["seed"] is False and bed["line"] == 9  # the `fixture` line in _plan
    assert {"x", "y", "w", "l", "wall", "room", "level"} <= set(bed)
    # a seeded fixture carries a null source line.
    seed = next(f for f in fixtures if f["kind"] == "toilet")
    assert seed["seed"] is True and seed["line"] is None


# --- python builder API ------------------------------------------------------


def test_builder_add_fixture():
    plan = (
        barndominium("b").envelope(20, 16).ceiling(9)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=14, length=12)
        .add_room("rest", T.LIVING, x=14, y=0, width=6, length=16)
        .add_fixture("bed_queen", "bed", wall="north")
    )
    assert validate(plan)  # no crash
    fixtures = resolve_room_fixtures(plan, plan.room("bed"))
    assert any(f.kind == "bed_queen" and not f.seed for f in fixtures)
