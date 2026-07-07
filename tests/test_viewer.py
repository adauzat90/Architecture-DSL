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


# --- Phase 2: fixture collision block ----------------------------------------

FURN_PLAN = """\
plan "Furnished"
envelope 30 x 20
ceiling 9
room a: living at 0,0 size 15 x 20
room b: bedroom at 15,0 size 15 x 20
door a - b width 3 offset 8
entry a south width 3 offset 4
fixture sofa in a at 5,10
fixture bed_queen in b at 20,10
"""


def test_walk_block_ships_a_fixtures_entry_per_model_fixture():
    from barndsl.revit import to_revit_model

    plan = _plan(FURN_PLAN)
    model = to_revit_model(plan)
    w = scene_json(build_scene(plan))["walk"]
    assert "fixtures" in w
    # One walk.fixtures entry per model fixture, in model order.
    assert len(w["fixtures"]) == len(model.fixtures)
    for fx, jf in zip(model.fixtures, w["fixtures"]):
        assert jf["kind"] == fx.kind
        assert jf["level"] == fx.level


def test_walk_fixture_carries_footprint_and_elevation_fields():
    w = scene_json(build_scene(_plan(FURN_PLAN)))["walk"]
    assert w["fixtures"], "a furnished plan carries fixture rects"
    for jf in w["fixtures"]:
        assert {"x", "y", "w", "l", "elevation", "level", "kind"} <= set(jf)
        assert jf["w"] > 0 and jf["l"] > 0          # a real footprint rect
        # coordinates rounded to 4 places (deterministic serialisation)
        for k in ("x", "y", "w", "l", "elevation"):
            assert round(jf[k], 4) == jf[k]


def test_walk_fixture_rect_matches_the_model_footprint():
    from barndsl.revit import to_revit_model

    plan = _plan(FURN_PLAN)
    model = to_revit_model(plan)
    w = scene_json(build_scene(plan))["walk"]
    fx0, jf0 = model.fixtures[0], w["fixtures"][0]
    assert abs(jf0["x"] - fx0.x) < 1e-3 and abs(jf0["y"] - fx0.y) < 1e-3
    assert abs(jf0["w"] - fx0.width) < 1e-3 and abs(jf0["l"] - fx0.length) < 1e-3


def test_walk_block_still_ships_eyeheight_and_is_deterministic():
    # The standing eye height stays the shipped authority, and adding fixtures
    # keeps the whole walk block byte-identical across two builds.
    from barndsl.viewer import WALK_EYE_HEIGHT

    plan = _plan(FURN_PLAN)
    w = scene_json(build_scene(plan))["walk"]
    assert w["eyeHeight"] == WALK_EYE_HEIGHT
    a = json.dumps(scene_json(build_scene(plan)), sort_keys=True)
    b = json.dumps(scene_json(build_scene(plan)), sort_keys=True)
    assert a == b


def test_fixtures_do_not_leak_into_glb_or_ifc_bytes():
    # The fixtures walk data is viewer-only: the exported glb/ifc bytes are
    # untouched by building (and re-building) the scene JSON.
    import hashlib

    from barndsl.gltf import to_glb
    from barndsl.ifc import to_ifc

    plan = _plan(FURN_PLAN)
    g1 = hashlib.sha256(to_glb(plan)).hexdigest()
    i1 = hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest()
    scene_json(build_scene(plan))  # exercises the fixtures walk path
    assert hashlib.sha256(to_glb(plan)).hexdigest() == g1
    assert hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest() == i1


# --- Phase 2: renderer control hooks (JS is exercised in a node harness) ------

from barndsl.viewer import RENDERER_JS  # noqa: E402


def test_renderer_has_the_touch_joystick_hooks():
    # The virtual thumbstick: a base + knob element, coarse-pointer detection, and
    # a per-pointer stick id so one thumb steers while another looks.
    assert "stickBase" in RENDERER_JS and "stickKnob" in RENDERER_JS
    assert "pointer: coarse" in RENDERER_JS      # touch detection
    assert "stickId" in RENDERER_JS              # per-pointer tracking
    assert "updateStick" in RENDERER_JS          # folds the stick into move keys


def test_renderer_has_eye_height_presets_and_the_c_key():
    # Three presets (5.5 / 4.0 / 3.5), a cycle entry point, and the C binding.
    assert "EYE_PRESETS" in RENDERER_JS
    assert "cycleEye" in RENDERER_JS
    assert "4.0" in RENDERER_JS and "3.5" in RENDERER_JS
    assert "KeyC" in RENDERER_JS                 # C cycles the eye height
    assert "walkEyeTarget" in RENDERER_JS        # eased, not snapped


def test_renderer_has_the_furniture_collision_toggle():
    assert "furniture" in RENDERER_JS
    assert "toggleFurniture" in RENDERER_JS
    assert "walkFixtures" in RENDERER_JS         # the flattened rects it collides


def test_renderer_has_the_eye_attached_fill_uniform():
    # uFill is the eye-attached fill; it must be a real uniform the shader folds in.
    assert "uFill" in RENDERER_JS
    assert "WALK_FILL" in RENDERER_JS


def test_renderer_does_not_request_pointer_lock_on_touch():
    # Entering walk on a coarse pointer must skip pointer lock (no Esc on iPad).
    assert "!coarse && canvas.requestPointerLock" in RENDERER_JS


# --- Phase 3: walk-block rooms + mini-map / room-toast hooks ------------------


def test_walk_block_ships_a_rooms_entry_per_model_room():
    from barndsl.revit import to_revit_model

    plan = _plan(WALK_PLAN)
    model = to_revit_model(plan)
    w = scene_json(build_scene(plan))["walk"]
    assert "rooms" in w
    # One walk.rooms entry per model room, in model order (deterministic).
    assert len(w["rooms"]) == len(model.rooms)
    for r, jr in zip(model.rooms, w["rooms"]):
        assert jr["id"] == r.id
        assert jr["name"] == r.name          # the schedule display name
        assert jr["level"] == r.level


def test_walk_room_carries_rect_name_area_fields_rounded():
    w = scene_json(build_scene(_plan(WALK_PLAN)))["walk"]
    assert w["rooms"], "a plan with rooms carries room rects"
    for jr in w["rooms"]:
        assert {"id", "name", "x", "y", "w", "l", "elevation", "level", "area"} <= set(jr)
        assert jr["w"] > 0 and jr["l"] > 0 and jr["area"] > 0
        assert isinstance(jr["name"], str) and jr["name"]
        # coordinates/area rounded to 4 places (deterministic serialisation)
        for k in ("x", "y", "w", "l", "elevation", "area"):
            assert round(jr[k], 4) == jr[k]


def test_walk_room_name_is_the_display_name_not_the_id():
    # The name is the schedule *display* name, which title-cases a multi-word id
    # ("primary_bedroom" -> "Primary Bedroom") rather than shipping the raw id —
    # the same string revit.py's RevitRoom.name resolves.
    src = """\
plan "Named"
envelope 30 x 20
ceiling 9
room primary_bedroom: bedroom at 0,0 size 15 x 20
room kitchen: kitchen at 15,0 size 15 x 20
entry primary_bedroom south width 3 offset 4
"""
    w = scene_json(build_scene(_plan(src)))["walk"]
    names = {jr["id"]: jr["name"] for jr in w["rooms"]}
    assert names["primary_bedroom"] == "Primary Bedroom"   # title-cased id fallback
    assert names["kitchen"] == "Kitchen"


def test_walk_room_rect_matches_the_model_room():
    from barndsl.revit import to_revit_model

    plan = _plan(WALK_PLAN)
    model = to_revit_model(plan)
    w = scene_json(build_scene(plan))["walk"]
    r0, jr0 = model.rooms[0], w["rooms"][0]
    assert abs(jr0["x"] - r0.x) < 1e-3 and abs(jr0["y"] - r0.y) < 1e-3
    assert abs(jr0["w"] - r0.width) < 1e-3 and abs(jr0["l"] - r0.length) < 1e-3
    assert abs(jr0["area"] - r0.area) < 1e-3


def test_walk_rooms_block_is_deterministic_across_two_builds():
    plan = _plan(WALK_PLAN)
    a = json.dumps(scene_json(build_scene(plan))["walk"]["rooms"], sort_keys=True)
    b = json.dumps(scene_json(build_scene(plan))["walk"]["rooms"], sort_keys=True)
    assert a == b


def test_rooms_do_not_leak_into_glb_or_ifc_bytes():
    # The rooms walk data is viewer-only: exported glb/ifc bytes are untouched by
    # building (and re-building) the scene JSON — the pinned-determinism invariant.
    import hashlib

    from barndsl.gltf import to_glb
    from barndsl.ifc import to_ifc

    plan = _plan(WALK_PLAN)
    g1 = hashlib.sha256(to_glb(plan)).hexdigest()
    i1 = hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest()
    scene_json(build_scene(plan))  # exercises the rooms walk path
    assert hashlib.sha256(to_glb(plan)).hexdigest() == g1
    assert hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest() == i1


def test_renderer_has_the_minimap_hud_hooks():
    # The mini-map inset: its own canvas + 2D context, an M-key toggle, a session
    # flag, and the draw entry point wired into the walk step.
    assert "minimap" in RENDERER_JS              # the inset canvas element
    assert "drawMinimap" in RENDERER_JS          # the per-frame draw
    assert "toggleMinimap" in RENDERER_JS        # the M toggle
    assert "minimapOn" in RENDERER_JS            # remembered for the session
    assert "KeyM" in RENDERER_JS                 # M binds the toggle
    assert "walkRooms" in RENDERER_JS            # room rects the map + toast read


def test_renderer_has_the_room_name_toast_hooks():
    # The room-name toast: a second pill (not the hint), a room-crossing detector
    # with smallest-area tie-break, and the "name - dims - area" label builder.
    assert "roomToast" in RENDERER_JS            # the toast element (distinct from walkHint)
    assert "showRoomToast" in RENDERER_JS        # fades it in on a crossing
    assert "roomAt" in RENDERER_JS               # point-in-rect on the current storey
    assert "curRoomIdx" in RENDERER_JS           # tracks the current room (no re-toast)
    assert "sq ft" in RENDERER_JS                # the ASCII area label
    assert "fmtFt" in RENDERER_JS                # one-decimal dimension formatting


# --- Phase 4: sun-study block + section-cut / level-isolation hooks -----------

SUN_PLAN = """\
plan "Sun Sited"
envelope 40 x 30
orientation 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 30
entry living south width 3 offset 10
window bedroom east width 4 offset 4
"""


def test_scene_json_ships_a_sun_block_with_orientation_and_latitude():
    # scene_json ships `sun` carrying the plan's compass orientation and a fixed
    # default latitude, so the renderer's solar-position model honours plan north.
    data = scene_json(build_scene(_plan(SUN_PLAN)))
    assert "sun" in data
    sun = data["sun"]
    assert {"orientation", "latitude"} <= set(sun)
    assert sun["orientation"] == 30.0        # the plan's declared orient 30
    assert sun["latitude"] == 35.0           # the fixed default latitude


def test_sun_orientation_defaults_to_zero_when_plan_is_unsited():
    # A plan with no `orient` statement ships orientation 0.0 (a declared 0, not
    # None), so the sun model always has a definite compass anchor.
    data = scene_json(build_scene(_plan(WALK_PLAN)))  # WALK_PLAN has no orient
    assert _plan(WALK_PLAN).orientation is None
    assert data["sun"]["orientation"] == 0.0


def test_sun_block_is_deterministic_across_two_builds():
    plan = _plan(SUN_PLAN)
    a = json.dumps(scene_json(build_scene(plan))["sun"], sort_keys=True)
    b = json.dumps(scene_json(build_scene(plan))["sun"], sort_keys=True)
    assert a == b


def test_full_scene_json_stays_byte_identical_with_the_sun_block():
    # The whole blob (nodes + walk + sun) serialises byte-for-byte the same twice.
    plan = _plan(SUN_PLAN)
    a = json.dumps(scene_json(build_scene(plan)))
    b = json.dumps(scene_json(build_scene(plan)))
    assert a == b


def test_sun_block_does_not_leak_into_glb_or_ifc_bytes():
    # The sun block is viewer-JSON only: exported glb/ifc bytes are untouched by
    # building (and re-building) the scene JSON — the pinned-determinism invariant.
    import hashlib

    from barndsl.gltf import to_glb
    from barndsl.ifc import to_ifc

    plan = _plan(SUN_PLAN)
    g1 = hashlib.sha256(to_glb(plan)).hexdigest()
    i1 = hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest()
    scene_json(build_scene(plan))  # exercises the sun path
    assert hashlib.sha256(to_glb(plan)).hexdigest() == g1
    assert hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest() == i1


def test_renderer_has_the_sun_study_hooks():
    # The sun model: a uLightDir uniform replacing the old constant, a warmth term,
    # the setSun test hook, and the three season labels driving the 3-way.
    assert "uLightDir" in RENDERER_JS            # sun direction uniform (was a constant)
    assert "uSunWarmth" in RENDERER_JS           # low-sun warm-tint term
    assert "setSun" in RENDERER_JS               # the test/UI hook
    assert "computeSun" in RENDERER_JS           # the solar-position model
    for label in ("Winter", "Equinox", "Summer"):
        assert label in RENDERER_JS              # the season 3-way labels
    # The old hard-coded light direction is gone (now driven by the uniform).
    assert "normalize(vec3(0.4,0.9,0.5))" not in RENDERER_JS


def test_renderer_has_the_section_cut_and_level_isolation_hooks():
    # The section cut: a uClipY uniform + the discard, the setSection/setLevel hooks,
    # and a Section pill.
    assert "uClipY" in RENDERER_JS               # section-cut plane uniform
    assert "vWorld.y > uClipY" in RENDERER_JS    # the fragment discard
    assert "setSection" in RENDERER_JS           # the slider hook
    assert "setLevel" in RENDERER_JS             # the level-isolation hook
    assert "Section" in RENDERER_JS              # the pill label
    assert "sunState" in RENDERER_JS             # the state reporter for tests
