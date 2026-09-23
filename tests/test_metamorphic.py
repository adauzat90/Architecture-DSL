"""Broad invariants that should hold across parser/formatter/emitter surfaces."""

import dataclasses
import enum
from pathlib import Path

import pytest

from barndsl.compiler import compile_file, compile_source, read_source_file
from barndsl.diagnostics import CATEGORY_OWNERS, REGISTRY, diagnostic_category, diagnostic_owner, explain
from barndsl.edits import apply_edit, edit_from_json
from barndsl.emit import emit_dsl, instance_emitter_types
from barndsl.fmt import format_source

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").rglob("*.barn"))

#: Every statement and option in one whole plan, so a field the parser sets but
#: ``emit_dsl`` forgets (a room's ``floor "<finish>"``, say) breaks the model
#: round-trip below instead of vanishing silently from every re-emit caller.
KITCHEN_SINK = '''plan "Kitchen sink"
envelope 60 x 40
wing 20 x 16 at 60,0
ceiling 10
floor 1.5
accessible
electrical
orientation 15
street south
finish siding "board and batten" roof "standing seam"
overhang 1.5
climate 4
roof gable pitch 0.5
site 200 x 150
setback front 25 side 10 rear 20
building at 60,40
drive at 10,10 size 12 x 40 concrete
walk from great to drive width 5
well at 180,130
septic at 20,120 field 30 x 20
service electric from W
grade 2
program 2 bed 2 bath 1 kitchen area 1500 storage 20
require adjacent great kitchen
require separate bed garage
require exterior great south
require area great >= 300
wall kitchen - bath plumbing
wall great - kitchen bearing
suite primary: bed bath
zone private: primary bed2
note "free text note"
note "callout" at 5,5 level 0
frame bay 12 span 40 post 8 no-ridge
room great: living at 0,0 size 28 x 24 ceiling 12 vaulted floor "wood plank"
room kitchen: kitchen east-of great size 16 x 24 floor "tile"
room bath: bathroom east-of kitchen size 8 x 12
room bed: bedroom north-of great size 14 x 16
room bed2: bedroom east-of bed size 14 x 16
room hall: hallway east-of bed2 size 6 x 16
room garage: garage at 60,0 size 20 x 16
room loft: loft at 0,0 size 14 x 12 level 1
door great - kitchen cased width 8
door kitchen - bath pocket width 2.5 offset 2
door great - bed width 3 offset 4 into bed hinge far
door bed - bed2 sliding width 3
door bed2 - hall bifold width 4
open kitchen - hall width 3
entry great south double width 6 offset 10
door kitchen south exterior french width 5 offset 4 no-egress
door garage south overhead width 16 height 8 offset 2
window great south slider width 6 offset 2 sill 2.5 head 7 tempered
window bed north double-hung width 4 offset 5
window bed2 north width 4 offset 5
window kitchen south fixed width 3 offset 12
porch front at 0,-8 size 28 x 8 covered
porch back at 30,40 size 10 x 6 open
stair up at 0,12 size 4 x 12 from 0 to 1
fixture sofa in great at 4,4 rotate 90
fixture counter in kitchen along N from 1 to 12 depth 2
fixture kitchen_island in kitchen at 6,8 width 6
fixture toilet in bath wall S offset 1
outlet in great wall S offset 3 gfci
switch in great wall W offset 2
light in great at 10,10 kind pendant
light in bed at 7,8
alarm smoke in bed at 7,8
alarm smoke_co in hall
alarm co in great
'''

#: A part carrying every statement a part may hold, for the stamped-type checks.
EVERYTHING_PART = '''room a: bedroom at 0,0 size 12 x 12
room b: bathroom east-of a size 8 x 12
room c: closet north-of a size 12 x 4
room l: loft at 0,0 size 12 x 12 level 1
wall a - b plumbing
suite s: a b c
zone z: s
door a - b width 2.5
open a - c width 3
window a south width 4 offset 4
fixture toilet in b wall S offset 1
outlet in a wall W offset 3
switch in a wall W offset 1
light in a at 6,6
alarm smoke in a
note "part callout" at 2,2
porch p at 0,-6 size 12 x 6 covered
stair st at 8,0 size 4 x 12 from 0 to 1
'''


def _model(obj):
    """A comparable value for a model object, ignoring source positions (they
    move whenever emission reflows a line) and the non-serialised ``placement``
    diagnostics hint."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return tuple(
            (f.name, _model(getattr(obj, f.name)))
            for f in dataclasses.fields(obj)
            if f.name not in {"line", "col", "placement"}
            and not f.name.endswith(("_line", "_lines", "_col"))
        )
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, (list, tuple)):
        return tuple(_model(x) for x in obj)
    if isinstance(obj, dict):
        return tuple(sorted((str(k), _model(v)) for k, v in obj.items()))
    return obj


def _model_diff(before, after) -> list[str]:
    """Plan attributes whose model value changed. Composition bookkeeping
    (``uses``/``instances``/``stamped_rooms``) is expected to go on flattening."""
    skip = {"uses", "instances", "stamped_rooms"}
    return [
        name for name in vars(before)
        if not name.startswith("_") and name not in skip
        and _model(getattr(before, name)) != _model(getattr(after, name, None))
    ]


def _room_signature(plan):
    return sorted(
        (
            r.id,
            r.type.value,
            r.level,
            round(r.x, 4),
            round(r.y, 4),
            round(r.width, 4),
            round(r.length, 4),
        )
        for r in plan.rooms
    )


def test_formatter_is_idempotent_for_curated_fixtures():
    fixtures = [
        ROOT / "examples/gallery/lshape.barn",
        ROOT / "examples/gallery/two_story.barn",
        ROOT / "examples/composed/cedar_ridge_v2.barn",
        ROOT / "examples/composed/parts/master_suite.barn",
    ]
    messy = 'PLAN   "Messy"\nENVELOPE  20 x 16\nroom living: living at 0,0 size 20 x 16  # keep me\nentry living south width 3\n'
    for source in [messy, *(p.read_text(encoding="utf-8") for p in fixtures)]:
        once = format_source(source)
        assert format_source(once) == once


def test_emit_is_a_fixed_point_for_whole_plan_fixtures():
    for rel in ["examples/gallery/cottage.barn", "examples/gallery/lshape.barn", "examples/gallery/two_story.barn"]:
        result = compile_file(str(ROOT / rel))
        assert result.plan is not None, result.summary()
        emitted = emit_dsl(result.plan)
        again = compile_source(emitted, name=result.plan.name)
        assert again.plan is not None, again.summary()
        assert emit_dsl(again.plan) == emitted


def test_fragment_emit_is_a_fixed_point_when_compiled_as_fragment():
    text = (ROOT / "examples/composed/parts/master_suite.barn").read_text(encoding="utf-8")
    result = compile_source(text, fragment=True, base_dir=str(ROOT / "examples/composed/parts"))
    assert result.plan is not None, result.summary()
    emitted = emit_dsl(result.plan, fragment=True)
    again = compile_source(emitted, fragment=True, base_dir=str(ROOT / "examples/composed/parts"))
    assert again.plan is not None, again.summary()
    assert emit_dsl(again.plan, fragment=True) == emitted


def test_flattened_composition_preserves_resolved_room_geometry():
    result = compile_file(str(ROOT / "examples/composed/cedar_ridge_v2.barn"))
    assert result.plan is not None, result.summary()
    flat = emit_dsl(result.plan, flatten=True)
    again = compile_source(flat, name=result.plan.name)
    assert again.plan is not None, again.summary()
    assert _room_signature(again.plan) == _room_signature(result.plan)
    assert emit_dsl(again.plan, flatten=True) == flat


def test_emit_preserves_every_statement_option():
    """``compile(emit(plan))`` rebuilds the same *model*, not just the same text —
    a text fixed point can't see a field the first emit already dropped."""
    result = compile_source(KITCHEN_SINK)
    assert result.ok, result.summary()
    again = compile_source(emit_dsl(result.plan))
    assert again.ok, again.summary()
    assert _model_diff(result.plan, again.plan) == []


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_flattened_emit_preserves_the_model_for_every_example(path):
    text, base_dir = read_source_file(str(path))
    fragment = not any(line.strip().lower().startswith("plan ") for line in text.splitlines())
    result = compile_source(text, fragment=fragment, base_dir=base_dir, self_path=str(path))
    assert result.plan is not None, result.summary()
    emitted = emit_dsl(result.plan, flatten=True, fragment=fragment)
    again = compile_source(emitted, fragment=fragment, name=result.plan.name)
    assert again.plan is not None, again.summary()
    assert _model_diff(result.plan, again.plan) == []


def _everything_host(tmp_path):
    (tmp_path / "everything.barn").write_text(EVERYTHING_PART, encoding="utf-8")
    src = (
        'plan "Host"\nenvelope 40 x 30\nceiling 9\n'
        'room great: living at 20,0 size 20 x 30\n'
        'use "everything.barn" as q at 0,6\n'
        'entry great south width 3 offset 8\n'
    )
    return src, str(tmp_path)


def test_every_stamped_element_type_has_an_instance_emitter(tmp_path):
    src, base_dir = _everything_host(tmp_path)
    result = compile_source(src, base_dir=base_dir)
    assert result.plan is not None and not result.recovered, result.summary()
    (inst,) = result.plan.instances
    stamped_types = {type(o) for o in inst.objects}
    assert stamped_types - set(instance_emitter_types()) == set()


def test_inline_use_keeps_every_stamped_element(tmp_path):
    src, base_dir = _everything_host(tmp_path)
    before = compile_source(src, base_dir=base_dir)
    edit = apply_edit(src, edit_from_json({"kind": "inline_use", "alias": "q"}), base_dir=base_dir)
    assert edit.changed and "use " not in edit.source
    after = compile_source(edit.source, base_dir=base_dir)
    assert after.plan is not None and not after.recovered, after.summary()
    assert _model_diff(before.plan, after.plan) == []


def test_explicit_category_sets_are_disjoint_and_cover_every_code():
    from barndsl import diagnostics as d

    explicit = [d._SYNTAX_CODES, d._PROGRAM_CODES, d._ACCESS_EGRESS_CODES, d._CIRCULATION_CODES,
                d._GEOMETRY_CODES, d._OPENING_CODES, d._STRUCTURE_CODES, d._FIXTURE_CODES,
                d._QUALITY_CODES]
    # An overlap would let whichever set merges last silently win.
    assert sum(len(s) for s in explicit) + 1 == len(d._EXPLICIT_CATEGORIES)  # +1: FLOOR_FINISH
    assert d.unclassified_codes() == []
    # Code minimums and hard errors are never filed as soft coaching.
    for code in ("ROOM_SIZE", "ROOM_HABITABLE", "ROOM_CLEAR"):
        assert diagnostic_category(code) == "geometry"


def test_every_registered_diagnostic_has_a_category_and_owner():
    categories = {info.category for info in REGISTRY.values()}
    assert categories <= set(CATEGORY_OWNERS)
    assert {"syntax", "composition", "fixtures", "electrical", "structure", "site"} <= categories
    for code, info in REGISTRY.items():
        assert diagnostic_category(code) == info.category
        assert diagnostic_owner(info.category) == info.owner
        assert f"({info.category}; owner: {info.owner})" in explain(code)
