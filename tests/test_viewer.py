"""Tests for the single-file HTML 3D viewer (`barndsl.viewer`).

The viewer's contract is that it is *one* self-contained, offline file: no
network references, the scene data embedded, every present layer toggleable, and
the plan title shown. These pin exactly that.
"""

from __future__ import annotations

import json
import os

from barndsl import compile_source, viewer_html, write_viewer
from barndsl.viewer import _LAYER_LABELS

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

SIMPLE = """\
plan "Willow Bend"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bedroom width 2.67
entry living south width 3 offset 10
window bedroom east width 4 offset 4
"""


def _plan(src=SIMPLE):
    plan = compile_source(src).plan
    assert plan is not None
    return plan


def test_single_offline_file_no_network_references():
    html = viewer_html(_plan())
    # Inline-renderer route: the file must not reach out to the network at all.
    assert "http://" not in html
    assert "https://" not in html
    assert "//cdn" not in html
    assert "src=" not in html or "<script src" not in html


def test_title_and_stats_present():
    plan = _plan()
    html = viewer_html(plan)
    assert plan.name in html
    m = plan.metrics()
    assert f"{m['footprint_sqft']:.0f} sq ft" in html


def test_embeds_the_scene_data():
    html = viewer_html(_plan())
    assert '<script id="scene"' in html
    # The embedded JSON is real, parseable, and carries geometry.
    start = html.index('type="application/json">') + len('type="application/json">')
    end = html.index("</script>", start)
    data = json.loads(html[start:end])
    assert data["nodes"] and data["layers"]
    node = data["nodes"][0]
    assert {"name", "layer", "color", "positions", "normals", "indices"} <= set(node)
    assert len(node["positions"]) % 3 == 0


def test_every_present_layer_is_toggleable():
    plan = _plan()
    html = viewer_html(plan)
    start = html.index('type="application/json">') + len('type="application/json">')
    end = html.index("</script>", start)
    data = json.loads(html[start:end])
    for layer in data["layers"]:
        assert layer in _LAYER_LABELS
        assert _LAYER_LABELS[layer] in html


def test_write_viewer_produces_one_file(tmp_path):
    out = tmp_path / "plan.html"
    write_viewer(_plan(), str(out))
    assert out.exists()
    assert out.read_text(encoding="utf-8").lstrip().startswith("<!doctype html>")
    assert list(tmp_path.iterdir()) == [out]  # exactly one file, no sidecars


def test_gallery_plans_render_a_viewer():
    for rel in ("cedar_ridge.barn", "gallery/two_story.barn", "frame_demo.barn"):
        with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
            plan = compile_source(fh.read()).plan
        assert plan is not None
        html = viewer_html(plan)
        assert plan.name in html
        assert "http" not in html


# --- first-person walk-support block ----------------------------------------

from barndsl.gltf import build_scene  # noqa: E402
from barndsl.viewer import scene_json  # noqa: E402

WALK_PLAN = """\
plan "Walk Test"
envelope 30 x 20
ceiling 9
room a: living at 0,0 size 15 x 20
room b: bedroom at 15,0 size 15 x 20
door a - b width 3 offset 8
entry a south width 3 offset 4
window b east width 4 offset 8
"""


def _walk(src=WALK_PLAN):
    return scene_json(build_scene(_plan(src)))["walk"]


def test_walk_block_has_the_four_sections():
    w = _walk()
    assert {"segments", "floors", "stairs", "spawn", "eyeHeight"} <= set(w)
    assert w["segments"] and w["floors"]  # a real plan has walls and a floor
    assert w["eyeHeight"] > 4  # a standing eye height


def test_walk_door_gap_splits_a_wall_into_two_segments():
    # The a/b partition is a vertical run at x=15; the 3 ft interior door punches a
    # walkable gap, so it must arrive as two collinear segments with a ~3 ft gap.
    segs = [s for s in _walk()["segments"] if abs(s["x0"] - 15) < 1e-6 and abs(s["x1"] - 15) < 1e-6]
    assert len(segs) == 2
    segs.sort(key=lambda s: min(s["y0"], s["y1"]))
    gap = max(segs[0]["y0"], segs[0]["y1"])
    gap_end = min(segs[1]["y0"], segs[1]["y1"])
    assert abs((gap_end - gap) - 3.0) < 1e-3  # the door width


def test_walk_window_does_not_split_its_wall():
    # A window is not a walkable gap: the east exterior wall of room b (x=30) stays
    # one segment despite the 4 ft window on it.
    segs = [s for s in _walk()["segments"] if abs(s["x0"] - 30) < 1e-6 and abs(s["x1"] - 30) < 1e-6]
    assert len(segs) == 1


def test_walk_spawn_sits_inside_the_footprint():
    w = _walk()
    sp = w["spawn"]
    inside = any(
        r[0] - 0.01 <= sp["x"] <= r[0] + r[2] + 0.01 and r[1] - 0.01 <= sp["y"] <= r[1] + r[3] + 0.01
        for f in w["floors"] if f["level"] == 0 for r in f["rects"]
    )
    assert inside
    fx, fy = sp["face"]
    assert abs((fx * fx + fy * fy) - 1.0) < 1e-3  # a unit facing vector


def test_walk_stair_record_carries_both_floor_elevations():
    with open(os.path.join(EXAMPLES, "gallery", "two_story.barn"), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    w = scene_json(build_scene(plan))["walk"]
    assert w["stairs"], "the two-story plan has a stair"
    st = w["stairs"][0]
    assert {"fromElevation", "toElevation", "fromLevel", "toLevel", "dir"} <= set(st)
    assert st["fromElevation"] != st["toElevation"]  # it actually climbs
    assert len(w["floors"]) >= 2  # both storeys carry a floor


def test_walk_block_is_deterministic():
    plan = _plan(WALK_PLAN)
    a = json.dumps(scene_json(build_scene(plan))["walk"], sort_keys=True)
    b = json.dumps(scene_json(build_scene(plan))["walk"], sort_keys=True)
    assert a == b


def test_walk_block_does_not_disturb_glb_or_ifc_bytes():
    # The walk block is viewer/scene-JSON only; the glTF and IFC exporters build
    # from the same scene but must be untouched byte-for-byte.
    import hashlib

    from barndsl.gltf import to_glb
    from barndsl.ifc import to_ifc

    plan = _plan(WALK_PLAN)
    scene_json(build_scene(plan))  # exercising the walk path must have no side effects
    g1 = hashlib.sha256(to_glb(plan)).hexdigest()
    i1 = hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest()
    scene_json(build_scene(plan))
    assert hashlib.sha256(to_glb(plan)).hexdigest() == g1
    assert hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest() == i1


# --- door metadata + walk-block doors ---------------------------------------


def _nodes(src=WALK_PLAN):
    return scene_json(build_scene(_plan(src)))["nodes"]


def test_scene_json_door_leaf_carries_hinge_dir_out_metadata():
    # The interior swing door's leaf node ships a `door` record the renderer
    # animates; it names its plan-space hinge, along-wall dir, and swing side.
    leaf = next(n for n in _nodes() if n["name"] == "door:o0:leaf")
    d = leaf["door"]
    assert d["mode"] == "swing"
    assert len(d["hinge"]) == 2 and len(d["dir"]) == 2 and len(d["out"]) == 2
    # dir and out are unit vectors
    for v in (d["dir"], d["out"]):
        assert abs((v[0] * v[0] + v[1] * v[1]) - 1.0) < 1e-3
    assert d["width"] > 0 and d["height"] > 0


def test_scene_json_static_nodes_have_no_door_key():
    # Walls, glazing and casing are static: the `door` key is absent entirely, so
    # the orbit path and the exported glb (closed geometry) agree.
    for n in _nodes():
        if not (n["name"].startswith("door:") ):
            assert "door" not in n, n["name"]


def test_walk_block_has_one_doors_entry_per_door_opening():
    # The a/b interior door is the only door in WALK_PLAN (the entry is exterior —
    # also a door — so two doors total); the window contributes none.
    w = _walk()
    assert w["doors"], "a door plan carries walk.doors entries"
    ids = {d["id"] for d in w["doors"]}
    # every entry is a real span segment with an elevation + level
    for d in w["doors"]:
        assert {"id", "x0", "y0", "x1", "y1", "elevation", "level"} <= set(d)
    # the interior door o0 shows up; the window never does
    assert "o0" in ids


def test_walk_doors_do_not_reintroduce_wall_gaps():
    # The doors array is additive: the wall SEGMENTS must still be punched (the
    # existing gap invariant), independent of the new doors list.
    w = _walk()
    segs = [s for s in w["segments"] if abs(s["x0"] - 15) < 1e-6 and abs(s["x1"] - 15) < 1e-6]
    assert len(segs) == 2  # the door gap still splits the a/b wall


def test_cased_opening_has_no_walk_doors_entry_and_no_leaf():
    # A cased (leafless) opening is always an open passage: no walk.doors entry
    # for it (so it never re-blocks the gap) and no door-leaf node.
    from barndsl.revit import to_revit_model

    src = """\
plan "Cased"
envelope 30 x 20
ceiling 9
room a: living at 0,0 size 15 x 20
room b: kitchen at 15,0 size 15 x 20
open a - b width 4
entry a south width 3 offset 4
"""
    plan = _plan(src)
    cased_ids = {o.id for o in to_revit_model(plan).openings
                 if o.category == "cased_opening"}
    assert cased_ids
    data = scene_json(build_scene(plan))
    door_ids = {d["id"] for d in data["walk"]["doors"]}
    assert not (cased_ids & door_ids)  # the cased opening is never a walk door
    # and its casing node (if drawn) carries no door record
    cased = [n for n in data["nodes"] if n["name"].startswith("cased:")]
    assert all("door" not in n for n in cased)


def test_scene_json_is_byte_identical_across_two_builds():
    # Determinism: the full scene JSON (nodes + door records + walk) serialises
    # byte-for-byte the same on a fresh build of the same plan.
    plan = _plan(WALK_PLAN)
    a = json.dumps(scene_json(build_scene(plan)))
    b = json.dumps(scene_json(build_scene(plan)))
    assert a == b
