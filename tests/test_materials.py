"""Tests for the 3D material palette (`barndsl.materials`) and its DSL surface.

Pin the fuzzy hint → material resolution (walls/roof/floors), the barndominium
defaults, the per-room `floor "..."` attribute (parsing + the friendly warning on
an unrecognised name), and that the resolved materials flow through to both the
glTF export (PBR factors, deterministically) and the viewer scene JSON.
"""

from __future__ import annotations

import hashlib

from barndsl import compile_source
from barndsl.elements import Room, RoomType
from barndsl.gltf import build_scene, to_glb, to_gltf
from barndsl.materials import (
    PALETTE,
    floor_material,
    known_floor_names,
    match_floor,
    roof_material,
    wall_material,
)
from barndsl.viewer import scene_json


class _Plan:
    """A tiny stand-in carrying just the finish hints the resolvers read."""

    def __init__(self, siding=None, roofing=None):
        self.siding = siding
        self.roofing = roofing


# --- wall / roof hint resolution --------------------------------------------


def test_wall_hint_keywords():
    assert wall_material(_Plan(siding="metal siding")).pattern == "rib"
    assert wall_material(_Plan(siding="board and batten")) is PALETTE["board_batten"]
    assert wall_material(_Plan(siding="lap siding")) is PALETTE["lap_siding"]
    assert wall_material(_Plan(siding="cedar clapboard")) is PALETTE["lap_siding"]


def test_wall_default_is_metal_when_unset_or_unmatched():
    assert wall_material(_Plan()) is PALETTE["metal_siding"]
    assert wall_material(_Plan(siding="unobtanium")) is PALETTE["metal_siding"]


def test_roof_hint_keywords_and_default():
    assert roof_material(_Plan(roofing="standing-seam")) is PALETTE["standing_seam"]
    assert roof_material(_Plan(roofing="asphalt shingle")) is PALETTE["asphalt_shingle"]
    assert roof_material(_Plan(roofing="metal")) is PALETTE["standing_seam"]
    assert roof_material(_Plan()) is PALETTE["standing_seam"]  # barndominium default


def test_metal_materials_are_actually_metallic():
    assert wall_material(_Plan(siding="metal")).metallic > 0.5
    assert roof_material(_Plan(roofing="standing seam")).metallic > 0.5
    # Wood/tile finishes are dielectric.
    assert PALETTE["wood_plank"].metallic == 0.0
    assert PALETTE["tile_floor"].metallic == 0.0


# --- floor hint resolution + defaults ---------------------------------------


def _room(rtype, floor=None):
    return Room("r", RoomType(rtype), 0, 0, 10, 10, floor=floor)


def test_floor_hint_matches():
    assert floor_material(_room("living", "tile")) is PALETTE["tile_floor"]
    assert floor_material(_room("living", "polished concrete")) is PALETTE["concrete_slab"]
    assert floor_material(_room("living", "wood plank")) is PALETTE["wood_plank"]
    assert floor_material(_room("living", "carpet")) is PALETTE["carpet"]


def test_floor_defaults_by_room_type():
    assert floor_material(_room("bathroom")) is PALETTE["tile_floor"]      # wet room
    assert floor_material(_room("laundry")) is PALETTE["tile_floor"]
    assert floor_material(_room("garage")) is PALETTE["concrete_slab"]
    assert floor_material(_room("shop")) is PALETTE["concrete_slab"]
    assert floor_material(_room("living")) is PALETTE["wood_plank"]        # default


def test_match_floor_returns_none_for_unknown():
    assert match_floor(None) is None
    assert match_floor("shag moon-dust") is None
    assert known_floor_names()  # non-empty vocabulary for the hint


# --- DSL: the `floor` attribute ---------------------------------------------

_BASE = """\
plan "Finish Test"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 24 x 30 floor "wood plank"
room bath: bathroom at 24,0 size 16 x 15 floor "tile"
room util: utility at 24,15 size 16 x 15
"""


def test_floor_attribute_parses():
    plan = compile_source(_BASE).plan
    assert plan is not None
    by_id = {r.id: r for r in plan.rooms}
    assert by_id["living"].floor == "wood plank"
    assert by_id["bath"].floor == "tile"
    assert by_id["util"].floor is None


def test_floor_attribute_combines_with_other_suffixes():
    src = _BASE + 'room loft: loft at 0,0 size 24 x 18 level 1 vaulted floor "carpet"\n'
    plan = compile_source(src).plan
    assert plan is not None
    loft = next(r for r in plan.rooms if r.id == "loft")
    assert loft.level == 1 and loft.vaulted and loft.floor == "carpet"


def test_unknown_floor_name_warns_but_does_not_block():
    src = _BASE.replace('floor "tile"', 'floor "moon dust"')
    result = compile_source(src)
    assert result.plan is not None  # still builds
    codes = {d.code for d in result.diagnostics}
    assert "FLOOR_FINISH" in codes
    warn = next(d for d in result.diagnostics if d.code == "FLOOR_FINISH")
    assert warn.severity.value == "warning"
    assert warn.room == "bath"


def test_known_floor_name_does_not_warn():
    codes = {d.code for d in compile_source(_BASE).diagnostics}
    assert "FLOOR_FINISH" not in codes


# --- glTF PBR + determinism --------------------------------------------------


def test_gltf_materials_carry_metallic_roughness():
    gltf = to_gltf(compile_source(_BASE).plan)
    for m in gltf["materials"]:
        pbr = m["pbrMetallicRoughness"]
        assert "metallicFactor" in pbr and "roughnessFactor" in pbr
        assert 0.0 <= pbr["metallicFactor"] <= 1.0
        assert 0.0 <= pbr["roughnessFactor"] <= 1.0
        assert len(pbr["baseColorFactor"]) == 4
    # The metal wall shell must emit a metallic material.
    assert any(m["pbrMetallicRoughness"]["metallicFactor"] > 0.5 for m in gltf["materials"])


def test_gltf_export_is_deterministic():
    src = _BASE
    a = hashlib.sha256(to_glb(compile_source(src).plan)).hexdigest()
    b = hashlib.sha256(to_glb(compile_source(src).plan)).hexdigest()
    assert a == b


def test_finish_changes_alter_the_export():
    plain = to_glb(compile_source(_BASE).plan)
    finished = _BASE + 'finish siding "board and batten" roof "asphalt shingle"\n'
    changed = to_glb(compile_source(finished).plan)
    assert plain != changed  # the wall/roof materials actually changed


# --- viewer scene JSON carries the material fields ---------------------------


def test_scene_json_carries_material_fields():
    scene = build_scene(compile_source(_BASE).plan)
    data = scene_json(scene)
    for node in data["nodes"]:
        assert {"color", "roughness", "metallic", "pattern", "patternScale"} <= set(node)
        assert node["pattern"] in (
            "rib", "batten", "shingle", "plank", "tile", "speckle", "none",
        )
        assert node["patternScale"] > 0
    # A ribbed metal wall and a tiled bath floor are both present and distinguishable.
    patterns = {n["name"].split(":")[0]: n["pattern"] for n in data["nodes"]}
    assert patterns.get("wall") == "rib"
    assert any(n["name"] == "room:bath" and n["pattern"] == "tile" for n in data["nodes"])
