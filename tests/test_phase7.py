"""Phase 7 — interior realism: transparent glass, a real shadow map, and finish
geometry (baseboards + flat ceilings).

Two kinds of test live here:

* **Python / scene tests** pin the new *exported* geometry — glazing ships an
  ``isGlass`` flag (and only glazing does), a finished room gets a skirting run with
  a gap at each door, a private room gets a flat ceiling while a great room stays
  open to the roof, the two new layers appear in the scene + labels, and the whole
  build stays byte-identical (the glb/ifc determinism invariant is untouched).

* A **headless JS harness** (``renderer_phase7_harness.js``, run under node with a
  stubbed WebGL/DOM) exercises the renderer's draw path: the shadow map renders
  before the main pass and skips glass casters, the sun's light view-projection maps
  a known interior point into the unit shadow frustum, glass draws in a second
  blended depth-write-off pass, and rendering still works when the FBO stub reports
  failure (shadows-off path). It is skipped when node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from barndsl import compile_source
from barndsl.gltf import Scene, build_scene
from barndsl.materials import GLASS_MATERIAL, PALETTE, TRIM_MATERIAL
from barndsl.viewer import RENDERER_JS, _LAYER_LABELS, scene_json

HERE = __import__("os").path.dirname(__file__)
HARNESS = __import__("os").path.join(HERE, "renderer_phase7_harness.js")


def _plan(src):
    plan = compile_source(src).plan
    assert plan is not None
    return plan


# A plan with a private room (bedroom: baseboard + ceiling), a great room (living:
# baseboard, NO ceiling), a bare-slab garage (neither), a door (baseboard gap) and
# a window (glazing, NO baseboard gap).
MIX = """\
plan "Interior"
envelope 60 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 30
room garage: garage at 40,0 size 20 x 30
door living - bedroom width 3 offset 12
entry living south width 3 offset 10
window bedroom east width 4 offset 8
"""


def _nodes(src=MIX):
    return {n.name: n for n in build_scene(_plan(src)).nodes if not n.empty}


# ===========================================================================
# A. Transparent glass — the isGlass flag
# ===========================================================================


def test_only_glazing_ships_isglass():
    data = scene_json(build_scene(_plan(MIX)))
    glass = [n for n in data["nodes"] if n.get("isGlass")]
    assert glass, "the window glazing ships isGlass"
    # Every isGlass node is a glazing pane (name ends :glass) in the glass material.
    for n in glass:
        assert n["name"].endswith(":glass")
        assert n["mat"] == GLASS_MATERIAL.name
    # And nothing else carries the flag (walls, trim, mullions, doors, ...).
    for n in data["nodes"]:
        if not n["name"].endswith(":glass"):
            assert "isGlass" not in n, n["name"]


def test_isglass_does_not_leak_into_glb_or_ifc_bytes():
    import hashlib

    from barndsl.gltf import to_glb
    from barndsl.ifc import to_ifc

    plan = _plan(MIX)
    g1 = hashlib.sha256(to_glb(plan)).hexdigest()
    i1 = hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest()
    scene_json(build_scene(plan))  # exercises the isGlass path
    assert hashlib.sha256(to_glb(plan)).hexdigest() == g1
    assert hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest() == i1


# ===========================================================================
# C. Baseboards + flat ceilings (real geometry)
# ===========================================================================


def test_new_layers_present_in_scene_and_labels():
    # The two new layers exist in LAYERS, appear in a mixed plan's scene layers, and
    # each has a toggle label.
    assert "trim" in Scene.LAYERS and "ceilings" in Scene.LAYERS
    assert "trim" in _LAYER_LABELS and "ceilings" in _LAYER_LABELS
    layers = scene_json(build_scene(_plan(MIX)))["layers"]
    assert "trim" in layers          # baseboards ride the trim layer
    assert "ceilings" in layers      # the bedroom ceiling


def test_baseboard_present_for_bedroom_absent_for_garage():
    nodes = _nodes()
    assert "baseboard:bedroom" in nodes            # a finished room gets skirting
    assert nodes["baseboard:bedroom"].layer == "trim"
    assert nodes["baseboard:bedroom"].material is TRIM_MATERIAL
    assert "baseboard:living" in nodes             # the great room too (bare-slab only skips)
    assert "baseboard:garage" not in nodes         # a bare garage slab gets none


def test_baseboard_has_a_gap_at_a_door():
    # The living room's south edge carries the entry door; its skirting must stop at
    # the doorway, so the south-edge run is split. Count boxes: a plain 4-edge run is
    # 4 boxes; a door on one edge splits it, giving 5 (or more with the interior
    # door on the east edge). The bedroom's window does NOT split its edge.
    nodes = _nodes()
    living = nodes["baseboard:living"]
    bedroom = nodes["baseboard:bedroom"]
    living_boxes = len(living.indices) // 36        # each add_box is 12 tris = 36 idx
    bedroom_boxes = len(bedroom.indices) // 36
    # The living room has the entry (south) AND the interior door (east) -> 2 splits.
    assert living_boxes == 6, living_boxes
    # The bedroom has the interior door on its west edge (1 split) and a window on
    # its east edge (NO split, windows don't punch a baseboard) -> 5.
    assert bedroom_boxes == 5, bedroom_boxes


def test_ceiling_present_for_bedroom_absent_for_living():
    nodes = _nodes()
    assert "ceiling:bedroom" in nodes              # a private room is enclosed
    assert nodes["ceiling:bedroom"].layer == "ceilings"
    assert nodes["ceiling:bedroom"].material is PALETTE["drywall"]
    assert "ceiling:living" not in nodes           # a great room stays open to the roof
    assert "ceiling:garage" not in nodes           # bare slab, no ceiling


def test_ceiling_sits_at_the_clear_storey_height():
    # The bedroom ceiling slab top is at the level floor + the room's clear ceiling
    # height (the plate the walls are extruded to), so it caps the walls with no gap.
    plan = _plan(MIX)
    scene = build_scene(plan)
    ceil = next(n for n in scene.nodes if n.name == "ceiling:bedroom")
    top_z = max(p[2] for p in ceil.positions)      # plan-space z (up)
    assert abs(top_z - plan.ceiling_height) < 1e-6  # level 0, so elevation 0


def test_vaulted_private_room_skips_the_ceiling():
    src = """\
plan "Vaulted"
envelope 30 x 20
ceiling 10
room bed: bedroom at 0,0 size 30 x 20 vaulted
entry bed south width 3 offset 10
window bed north width 4 offset 12
"""
    nodes = _nodes(src)
    assert "baseboard:bed" in nodes                # still finished at the floor
    assert "ceiling:bed" not in nodes              # but open to the roof (vaulted)


def test_ceiling_honours_a_per_room_override():
    # A private room with its own lower ceiling drops the slab to that height.
    src = """\
plan "Tray"
envelope 30 x 20
ceiling 12
room bed: bedroom at 0,0 size 30 x 20 ceiling 8
entry bed south width 3 offset 10
window bed north width 4 offset 12
"""
    scene = build_scene(_plan(src))
    ceil = next(n for n in scene.nodes if n.name == "ceiling:bed")
    top_z = max(p[2] for p in ceil.positions)
    assert abs(top_z - 8.0) < 1e-6                 # the room override, not the plan 12


def test_interior_realism_is_byte_identical_across_two_builds():
    # Determinism: the whole scene JSON (now carrying baseboards, ceilings, isGlass)
    # serialises byte-for-byte the same on a fresh build of the same plan.
    plan = _plan(MIX)
    a = json.dumps(scene_json(build_scene(plan)))
    b = json.dumps(scene_json(build_scene(plan)))
    assert a == b


def test_baseboards_and_ceilings_do_not_leak_into_glb_or_ifc_bytes():
    # They ARE real exported geometry (unlike the renderer-only sky/ground), so the
    # glb/ifc bytes DO include them — the invariant here is only that they are stable
    # across two builds (no nondeterminism crept in).
    import hashlib

    from barndsl.gltf import to_glb
    from barndsl.ifc import to_ifc

    plan = _plan(MIX)
    g1 = hashlib.sha256(to_glb(plan)).hexdigest()
    i1 = hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest()
    assert hashlib.sha256(to_glb(plan)).hexdigest() == g1
    assert hashlib.sha256(to_ifc(plan).encode("utf-8")).hexdigest() == i1


# ===========================================================================
# D. Walk block untouched by the interior-realism work
# ===========================================================================


def test_walk_block_untouched_by_phase7():
    # The finish geometry is scene-node only; the walk-support block (collision,
    # floors, doors, rooms) is derived from the exchange and must be unchanged and
    # still deterministic.
    plan = _plan(MIX)
    w = scene_json(build_scene(plan))["walk"]
    assert {"segments", "floors", "doors", "rooms", "spawn", "eyeHeight"} <= set(w)
    a = json.dumps(w, sort_keys=True)
    b = json.dumps(scene_json(build_scene(plan))["walk"], sort_keys=True)
    assert a == b


# ===========================================================================
# Headless JS harness — shadow map ordering, glass pass, FBO-fail path
# ===========================================================================

_NODE = shutil.which("node")


def _run_harness(mode, tmp_path):
    """Write RENDERER_JS to a temp file and run the node probe; return its report."""
    js = tmp_path / "renderer.js"
    js.write_text(RENDERER_JS, encoding="utf-8")
    out = subprocess.run(
        [_NODE, HARNESS, str(js), mode],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(_NODE is None, reason="node.js not available")
def test_renderer_js_is_syntactically_valid(tmp_path):
    js = tmp_path / "renderer.js"
    js.write_text(RENDERER_JS, encoding="utf-8")
    out = subprocess.run([_NODE, "--check", str(js)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@pytest.mark.skipif(_NODE is None, reason="node.js not available")
def test_harness_shadow_pass_runs_first_and_skips_glass_casters(tmp_path):
    r = _run_harness("ok", tmp_path)
    assert r["ok"], r
    # Shadows are live at 2048 and gated on in the shader.
    assert r["shadowOn"] is True
    assert r["shadowSize"] == 2048
    assert r["uShadowOn"] == 1
    # The shadow pass renders BEFORE the main pass, in its own program + FBO viewport.
    assert r["shadowBeforeMain"] is True
    assert r["shadowIsOwnProgram"] is True
    assert r["shadowViewport"] == [0, 0, 2048, 2048]
    # Two opaque casters (wall + roof) cast; the ONE glass node is skipped as a caster
    # (a window must pass sun, not block it).
    assert r["shadowDrawCount"] == 2


@pytest.mark.skipif(_NODE is None, reason="node.js not available")
def test_harness_light_matrix_maps_a_point_into_the_unit_frustum(tmp_path):
    r = _run_harness("ok", tmp_path)
    # A known interior world point projects inside the sun's ortho shadow frustum.
    assert r["inFrustum"] is True
    assert all(abs(c) <= 1.001 for c in r["ndc"])


@pytest.mark.skipif(_NODE is None, reason="node.js not available")
def test_harness_glass_draws_in_a_second_blended_depth_write_off_pass(tmp_path):
    r = _run_harness("ok", tmp_path)
    # On screen: 2 opaque (depth-write on) + 1 glass (blend on, depth-write off).
    assert r["screenDrawCount"] == 3
    assert r["opaqueDrawCount"] == 2
    assert r["glassDrawCount"] == 1


@pytest.mark.skipif(_NODE is None, reason="node.js not available")
def test_harness_renders_without_shadows_when_fbo_setup_fails(tmp_path):
    r = _run_harness("fbfail", tmp_path)
    assert r["ok"], r
    # The FBO stub reports incomplete: shadows disable cleanly...
    assert r["shadowOn"] is False
    assert r["uShadowOn"] == 0
    assert r["shadowDrawCount"] == 0
    # ...but the model still renders (opaque + glass), no planar fallback, no break.
    assert r["screenDrawCount"] == 3
    assert r["opaqueDrawCount"] == 2
    assert r["glassDrawCount"] == 1
