"""DXF export — a minimal, valid DXF R12 document from a plan."""

from __future__ import annotations

from barndsl import compile_source, save_dxf, to_dxf
from barndsl.dxf import LAYERS

SRC = """\
plan "DXF Test"
envelope 33 x 24
ceiling 9
room living:  living   at 0,0    size 18 x 14
room kitchen: kitchen  north-of living size 18 x 10
room bed:     bedroom  at 18,0   size 15 x 12
door living - kitchen width 6
door living - bed width 3
entry living south width 3 offset 4
window bed south width 4 offset 3
"""


def _plan():
    return compile_source(SRC).plan


def test_dxf_has_the_required_sections_and_eof():
    dxf = to_dxf(_plan())
    assert dxf.startswith("0\nSECTION")
    for section in ("HEADER", "TABLES", "ENTITIES"):
        assert section in dxf
    assert dxf.rstrip().endswith("EOF")


def test_layers_are_declared():
    dxf = to_dxf(_plan())
    assert "LAYER" in dxf
    for name in LAYERS:
        assert name in dxf


def test_entity_counts_match_the_plan():
    plan = _plan()
    dxf = to_dxf(plan)
    lines = dxf.count("\nLINE\n")
    texts = dxf.count("\nTEXT\n")
    # 4 lines for the envelope rectangle + 4 per room + 1 per opening (windows
    # and exterior doors). Interior doors aren't drawn in v1.
    n_open = len(plan.windows) + len(plan.exterior_doors)
    assert lines == 4 + 4 * len(plan.rooms) + n_open
    # Two TEXT labels (name + area) per room.
    assert texts == 2 * len(plan.rooms)


def test_non_ascii_names_are_sanitised():
    src = SRC.replace('"DXF Test"', '"Café Plan"')
    plan = compile_source(src).plan
    # A room label with a non-ascii char shouldn't break the ascii DXF write.
    plan.rooms[0].label = "Séjour"
    dxf = to_dxf(plan)
    assert dxf.isascii()


def test_save_dxf_writes_file(tmp_path):
    out = tmp_path / "plan.dxf"
    save_dxf(_plan(), str(out))
    assert out.exists()
    assert out.read_text().rstrip().endswith("EOF")
