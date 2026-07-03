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
