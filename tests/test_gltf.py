"""Tests for the glTF 2.0 exporter (`barndsl.gltf`).

Pin the structural invariants of the emitted glTF (accessor/bufferView
bookkeeping, POSITION min/max, in-range indices, valid cross-references), the
.glb container framing (magic/length/chunk padding), the geometry contract (every
wall run → a mesh, opening cuts reduce wall volume, a roof when the plan has one),
the y-up coordinate mapping, and a gallery sweep that exports every bundled plan
in both formats. Pure Python — no Revit, no 3D library.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import struct

import pytest

from barndsl import compile_source, to_glb, to_gltf
from barndsl.gltf import build_scene, wall_solids
from barndsl.revit import to_revit_model

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

SIMPLE = """\
plan "Simple"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bedroom width 2.67
door living - bath width 2.67
entry living south width 3 offset 10
window bedroom east width 4 offset 4
window living west width 8 offset 10
"""


def _plan(src=SIMPLE):
    plan = compile_source(src).plan
    assert plan is not None
    return plan


def _example(rel):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    assert plan is not None
    return plan


def _buffer_bytes(gltf: dict) -> bytes:
    """Decode the single embedded base64 data-URI buffer from a .gltf dict."""
    uri = gltf["buffers"][0]["uri"]
    assert uri.startswith("data:")
    return base64.b64decode(uri.split(",", 1)[1])


def validate_gltf(gltf: dict, buf: bytes) -> None:
    """A minimal glTF 2.0 structural validator (the test's own checker).

    Verifies buffer/bufferView/accessor bookkeeping, POSITION min/max, index
    ranges, and that every mesh/node/material/accessor reference resolves.
    """
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["buffers"][0]["byteLength"] == len(buf)
    n_acc = len(gltf["accessors"])
    n_mat = len(gltf["materials"])
    n_mesh = len(gltf["meshes"])

    for bv in gltf["bufferViews"]:
        assert bv["buffer"] == 0
        assert bv["byteOffset"] % 4 == 0, "bufferView byteOffset must be 4-aligned"
        assert bv["byteOffset"] + bv["byteLength"] <= len(buf), "bufferView overruns buffer"

    comp_size = {5126: 4, 5125: 4, 5123: 2}
    type_count = {"VEC3": 3, "SCALAR": 1}
    for a in gltf["accessors"]:
        bv = gltf["bufferViews"][a["bufferView"]]
        stride = comp_size[a["componentType"]] * type_count[a["type"]]
        assert a["count"] * stride == bv["byteLength"], "accessor count vs bufferView length"
        assert a.get("byteOffset", 0) % 4 == 0

    # Node references (parents → children, children → meshes) all resolve.
    n_nodes = len(gltf["nodes"])
    for node in gltf["nodes"]:
        for c in node.get("children", []):
            assert 0 <= c < n_nodes
        if "mesh" in node:
            assert 0 <= node["mesh"] < n_mesh

    for m in gltf["meshes"]:
        for prim in m["primitives"]:
            assert 0 <= prim["material"] < n_mat
            pos = gltf["accessors"][prim["attributes"]["POSITION"]]
            nrm = gltf["accessors"][prim["attributes"]["NORMAL"]]
            idx = gltf["accessors"][prim["indices"]]
            for ref in (prim["attributes"]["POSITION"], prim["attributes"]["NORMAL"], prim["indices"]):
                assert 0 <= ref < n_acc
            # POSITION accessors must carry min/max (spec-required), correctly.
            assert "min" in pos and "max" in pos
            floats = struct.unpack_from(
                "<%df" % (pos["count"] * 3),
                buf,
                gltf["bufferViews"][pos["bufferView"]]["byteOffset"],
            )
            xs, ys, zs = floats[0::3], floats[1::3], floats[2::3]
            assert pos["min"] == pytest.approx([min(xs), min(ys), min(zs)])
            assert pos["max"] == pytest.approx([max(xs), max(ys), max(zs)])
            # Indices reference only existing vertices.
            bv = gltf["bufferViews"][idx["bufferView"]]
            indices = struct.unpack_from("<%dI" % idx["count"], buf, bv["byteOffset"])
            assert max(indices) < pos["count"]
            assert nrm["count"] == pos["count"]


# --- structural validity -----------------------------------------------------


def test_gltf_is_structurally_valid():
    gltf = to_gltf(_plan())
    validate_gltf(gltf, _buffer_bytes(gltf))


def test_gallery_examples_validate():
    for rel in ("cedar_ridge.barn", "gallery/two_story.barn", "frame_demo.barn"):
        gltf = to_gltf(_example(rel))
        validate_gltf(gltf, _buffer_bytes(gltf))


# --- .glb container ----------------------------------------------------------


def test_glb_header_and_chunks():
    glb = to_glb(_plan())
    magic, version, length = struct.unpack_from("<III", glb, 0)
    assert magic == 0x46546C67  # "glTF"
    assert version == 2
    assert length == len(glb)

    # JSON chunk.
    j_len, j_type = struct.unpack_from("<II", glb, 12)
    assert j_type == 0x4E4F534A  # "JSON"
    assert j_len % 4 == 0, "JSON chunk must be 4-byte aligned"
    j_start = 20
    doc = json.loads(glb[j_start : j_start + j_len].decode("utf-8"))
    assert doc["asset"]["version"] == "2.0"

    # BIN chunk follows, also padded to 4.
    b_off = j_start + j_len
    b_len, b_type = struct.unpack_from("<II", glb, b_off)
    assert b_type == 0x004E4942  # "BIN\0"
    assert b_len % 4 == 0, "BIN chunk must be 4-byte aligned"
    assert b_off + 8 + b_len == len(glb)
    # The glb buffer has no uri (it lives in the BIN chunk).
    assert "uri" not in doc["buffers"][0]
    assert doc["buffers"][0]["byteLength"] == b_len


# --- geometry contract -------------------------------------------------------


def test_every_wall_run_produces_geometry():
    plan = _plan()
    model = to_revit_model(plan)
    scene = build_scene(plan)
    wall_nodes = {n.name for n in scene.nodes if n.name.startswith("wall:") and not n.empty}
    for w in model.walls:
        assert f"wall:{w.id}" in wall_nodes, f"wall {w.id} produced no geometry"


def test_opening_cut_reduces_wall_volume():
    plan = _plan()
    model = to_revit_model(plan)
    hosted: dict = {}
    for o in model.openings:
        if o.host_wall is not None:
            hosted.setdefault(o.host_wall, []).append(o)
    assert hosted, "expected at least one hosted opening"
    for wid, ops in hosted.items():
        w = next(w for w in model.walls if w.id == wid)
        cut = sum(b.volume for b in wall_solids(w, ops, 0.0))
        uncut = sum(b.volume for b in wall_solids(w, [], 0.0))
        assert cut < uncut, f"opening on {wid} did not reduce solid volume"


def test_roof_present_when_plan_has_one():
    plan = _plan()
    assert to_revit_model(plan).roof is not None
    scene = build_scene(plan)
    roof = [n for n in scene.nodes if n.name == "roof"]
    assert roof and not roof[0].empty


def test_room_floors_are_tinted_named_nodes():
    plan = _plan()
    scene = build_scene(plan)
    names = {n.name for n in scene.nodes if not n.empty}
    for r in plan.rooms:
        assert f"room:{r.id}" in names


# --- coordinate mapping ------------------------------------------------------


def test_y_up_mapping_sends_north_to_minus_z():
    from barndsl.gltf import _to_gltf

    # Plan +y is north, +z is up. glTF is y-up: north → -z, up → +y.
    assert _to_gltf((3.0, 5.0, 7.0)) == (3.0, 7.0, -5.0)


def test_units_are_feet_in_asset_extras():
    gltf = to_gltf(_plan())
    assert gltf["asset"]["extras"]["units"] == "feet"
    assert gltf["asset"]["extras"]["unit_scale"] == 1.0


# --- gallery sweep -----------------------------------------------------------


# --- CLI wiring --------------------------------------------------------------


def test_cli_gltf_writes_glb(tmp_path, capsys):
    from barndsl.cli import main

    out = tmp_path / "plan.glb"
    rc = main(["gltf", os.path.join(EXAMPLES, "cedar_ridge.barn"), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    assert struct.unpack_from("<I", out.read_bytes(), 0)[0] == 0x46546C67
    assert "glTF:" in capsys.readouterr().out


def test_cli_gltf_default_out_is_file_stem(tmp_path, capsys, monkeypatch):
    from barndsl.cli import main

    src = tmp_path / "myhouse.barn"
    src.write_text(SIMPLE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = main(["gltf", str(src)])
    assert rc == 0
    assert (tmp_path / "myhouse.glb").exists()


def test_cli_view3d_writes_html(tmp_path):
    from barndsl.cli import main

    out = tmp_path / "plan.html"
    rc = main(["view3d", os.path.join(EXAMPLES, "cedar_ridge.barn"), "--out", str(out)])
    assert rc == 0
    assert out.read_text(encoding="utf-8").lstrip().startswith("<!doctype html>")


def test_gallery_sweep_exports_both_formats():
    files = sorted(
        glob.glob(os.path.join(EXAMPLES, "*.barn"))
        + glob.glob(os.path.join(EXAMPLES, "gallery", "*.barn"))
    )
    assert files
    for path in files:
        with open(path, encoding="utf-8") as fh:
            plan = compile_source(fh.read()).plan
        assert plan is not None, path
        gltf = to_gltf(plan)
        validate_gltf(gltf, _buffer_bytes(gltf))
        glb = to_glb(plan)
        assert struct.unpack_from("<I", glb, 0)[0] == 0x46546C67
        assert struct.unpack_from("<I", glb, 8)[0] == len(glb)
