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
