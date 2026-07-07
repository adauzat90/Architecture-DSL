"""Tests for the 3D opening geometry — door leaves, panels, and window glazing.

Phase 1 of the 3D-viewer roadmap fills each hosted opening's void with the part
that makes it read as a house: a swing door's leaf (one node per leaf so the
viewer can animate them), a sliding/overhead panel, or a glazed window pane with
mullions. A cased opening stays a leafless passage. These pin the node shapes and
the materials the renderer switches on, plus the determinism the exporters need.
"""

from __future__ import annotations

import json

from barndsl import compile_source
from barndsl.gltf import build_scene
from barndsl.materials import GLASS_MATERIAL, PALETTE, TRIM_MATERIAL


def _plan(src):
    plan = compile_source(src).plan
    assert plan is not None
    return plan


def _open_nodes(src):
    """Non-empty nodes on the ``openings`` layer, keyed by name for easy lookup."""
    scene = build_scene(_plan(src))
    return {n.name: n for n in scene.nodes if n.layer == "openings" and not n.empty}


# --- swing doors: one leaf node each, double/french two ----------------------

_SWING = """\
plan "Swing"
envelope 30 x 20
ceiling 9
room a: living at 0,0 size 15 x 20
room b: bedroom at 15,0 size 15 x 20
door a - b width 3 offset 8
entry a south width 3 offset 4
"""


def test_swing_door_produces_a_single_leaf_node_with_a_door_record():
    nodes = _open_nodes(_SWING)
    leaves = [n for k, n in nodes.items() if k.startswith("door:") and k.endswith(":leaf")]
    assert len(leaves) == 2  # the interior door + the exterior entry, one leaf each
    for leaf in leaves:
        assert leaf.door is not None
        assert leaf.door["mode"] == "swing"


_DOUBLE = """\
plan "Double"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 40 x 30
entry living south french width 6 offset 18
"""


def test_double_or_french_door_produces_two_leaf_nodes():
    nodes = _open_nodes(_DOUBLE)
    leaves = sorted(k for k in nodes if k.startswith("door:") and ":leaf" in k)
    assert len(leaves) == 2, leaves
    assert leaves[0].endswith(":leaf0") and leaves[1].endswith(":leaf1")
    # a french pair reads as glazed leaves
    for name in leaves:
        assert nodes[name].material is GLASS_MATERIAL


# --- overhead (garage) door: a ribbed metal panel ----------------------------

_OVERHEAD = """\
plan "Garage"
envelope 40 x 30
ceiling 12
room shop: shop at 0,0 size 40 x 30
door shop south overhead width 10 offset 15
"""


def test_overhead_door_panel_uses_the_metal_rib_pattern():
    nodes = _open_nodes(_OVERHEAD)
    panels = [n for k, n in nodes.items() if k.startswith("door:") and k.endswith(":panel")]
    assert len(panels) == 1
    panel = panels[0]
    assert panel.material.pattern == "rib"       # reads as a ribbed metal door
    assert panel.door is not None and panel.door["mode"] == "overhead"


# --- window glazing ----------------------------------------------------------

_WINDOW = """\
plan "Glazed"
envelope 30 x 20
ceiling 9
room a: living at 0,0 size 30 x 20
entry a south width 3 offset 4
window a north width 4 offset 12
"""


def test_window_produces_a_glass_material_node():
    nodes = _open_nodes(_WINDOW)
    glass = [n for k, n in nodes.items() if k.startswith("window:") and k.endswith(":glass")]
    assert len(glass) == 1
    assert glass[0].material is GLASS_MATERIAL
    assert glass[0].door is None                 # glazing is static, no animation
    # the mullion cross also rides the openings layer so the window reads as one
    assert any(k.startswith("window:") and k.endswith(":mullions") for k in nodes)


# --- cased opening: no leaf --------------------------------------------------

_CASED = """\
plan "Cased"
envelope 30 x 20
ceiling 9
room a: living at 0,0 size 15 x 20
room b: kitchen at 15,0 size 15 x 20
open a - b width 4
entry a south width 3 offset 4
"""


def test_cased_opening_produces_no_leaf():
    # Find the cased opening's id off the model, then assert no leaf carries it.
    from barndsl.revit import to_revit_model

    plan = _plan(_CASED)
    cased_ids = {o.id for o in to_revit_model(plan).openings
                 if o.category == "cased_opening"}
    assert cased_ids, "the plan has a cased opening"
    nodes = _open_nodes(_CASED)
    # no swinging/sliding leaf shares a cased opening's id (the entry door may
    # still have one — that is a real door, not the cased passage)
    for cid in cased_ids:
        assert f"door:{cid}:leaf" not in nodes
        assert f"door:{cid}:panel" not in nodes
    # the cased casing node carries no door animation record
    for k, n in nodes.items():
        if k.startswith("cased:"):
            assert n.door is None


# --- determinism -------------------------------------------------------------


def test_opening_geometry_is_deterministic_across_builds():
    plan = _plan(_SWING)

    def snapshot():
        scene = build_scene(plan)
        return [
            (n.name, n.material.name, len(n.indices),
             json.dumps(n.door, sort_keys=True) if n.door else None)
            for n in scene.nodes if n.layer == "openings"
        ]

    assert snapshot() == snapshot()


# --- Phase 6: window frames + exterior door trim + interior drywall walls -----


def test_window_gets_a_perimeter_frame_in_trim_material():
    # A window carries a `<name>:frame` casing node on the openings layer, in the
    # painted-trim material — four slim boxes (jambs + head + sill) proud of the wall.
    nodes = _open_nodes(_WINDOW)
    frames = [n for k, n in nodes.items()
              if k.startswith("window:") and k.endswith(":frame")]
    assert len(frames) == 1
    frame = frames[0]
    assert frame.material is TRIM_MATERIAL
    # Four boxes: 4 * 36 = 144 vertex indices (each add_box is 12 triangles).
    assert len(frame.indices) == 4 * 36


def test_exterior_door_gets_head_and_jamb_trim_no_sill():
    # An exterior door gets a `door:<id>:frame` casing (head + two jambs, no
    # threshold band) in the trim material; an interior door does not.
    nodes = _open_nodes(_SWING)
    from barndsl.revit import to_revit_model

    model = to_revit_model(_plan(_SWING))
    ext_ids = {o.id for o in model.openings if o.category == "door" and o.exterior}
    int_ids = {o.id for o in model.openings if o.category == "door" and not o.exterior}
    assert ext_ids and int_ids, "SWING has both an exterior entry and an interior door"
    for oid in ext_ids:
        f = nodes.get(f"door:{oid}:frame")
        assert f is not None and f.material is TRIM_MATERIAL
        assert len(f.indices) == 3 * 36  # head + two jambs, no sill band
    for oid in int_ids:
        assert f"door:{oid}:frame" not in nodes  # interior doors carry no exterior trim


def test_interior_partition_is_drywall_exterior_wall_keeps_siding():
    # Requirement A: an interior partition run wears drywall; an exterior run keeps
    # the plan's siding hint (ribbed metal by default).
    from barndsl.revit import to_revit_model

    src = """\
plan "Shell"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 30
door living - bedroom width 3 offset 15
entry living south width 3 offset 10
"""
    plan = _plan(src)
    model = to_revit_model(plan)
    ext = {w.id: w.exterior for w in model.walls}
    scene = build_scene(plan)
    saw_interior = saw_exterior = False
    for n in scene.nodes:
        if not n.name.startswith("wall:"):
            continue
        wid = n.name.split(":")[1]
        if ext[wid]:
            assert n.material is PALETTE["metal_siding"], n.name
            saw_exterior = True
        else:
            assert n.material is PALETTE["drywall"], n.name
            saw_interior = True
    assert saw_interior and saw_exterior


def test_trim_and_drywall_walls_stay_deterministic_across_builds():
    # The new trim nodes + per-wall material choice must serialise identically twice.
    plan = _plan(_SWING)

    def snap():
        return [(n.name, n.material.name, len(n.indices))
                for n in build_scene(plan).nodes]

    assert snap() == snap()
