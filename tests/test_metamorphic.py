"""Broad invariants that should hold across parser/formatter/emitter surfaces."""

from pathlib import Path

from barndsl.compiler import compile_file, compile_source
from barndsl.diagnostics import CATEGORY_OWNERS, REGISTRY, diagnostic_category, diagnostic_owner, explain
from barndsl.emit import emit_dsl
from barndsl.fmt import format_source

ROOT = Path(__file__).resolve().parents[1]


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


def test_every_registered_diagnostic_has_a_category_and_owner():
    categories = {info.category for info in REGISTRY.values()}
    assert categories <= set(CATEGORY_OWNERS)
    assert {"syntax", "composition", "fixtures", "electrical", "structure", "site"} <= categories
    for code, info in REGISTRY.items():
        assert diagnostic_category(code) == info.category
        assert diagnostic_owner(info.category) == info.owner
        assert f"({info.category}; owner: {info.owner})" in explain(code)
