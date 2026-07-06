"""DXF export — a valid, deterministic DXF R2000 (AC1015) drawing from a plan.

The primary pins are stdlib structural checks that scan the emitted group-code
stream directly (no third-party parser). An optional belt-and-braces block loads
the file with ``ezdxf`` (skipped when it isn't installed) and runs its auditor.
"""

from __future__ import annotations

import math

import pytest

from barndsl import compile_source, render_svg, save_dxf, to_dxf
from barndsl.dxf import LAYERS
from barndsl.render import RenderConfig

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

# The Phase-10 chain-dimension fixture: two rooms partition the south (and north)
# wall into a 12′ + 8′ chain — the DXF must draw the same segment labels.
CHAIN_SRC = (
    'plan "Chain Demo"\n'
    "envelope 20 x 9\n"
    "ceiling 9\n"
    "room living: living at 0,0 size 12 x 9\n"
    "room kitchen: kitchen at 12,0 size 8 x 9\n"
    "entry living south width 3 offset 4\n"
)


def _plan(src: str = SRC):
    return compile_source(src).plan


# -- a tiny group-code scanner (DXF is (code, value) line pairs) --------------


def _pairs(dxf: str) -> list[tuple[int, str]]:
    lines = dxf.split("\n")
    out: list[tuple[int, str]] = []
    for i in range(0, len(lines) - 1, 2):
        code = lines[i].strip()
        if code == "":
            continue
        out.append((int(code), lines[i + 1]))
    return out


def _entities(pairs: list[tuple[int, str]]) -> list[dict]:
    """Every entity in the ENTITIES section as ``{type, layer, closed, codes}``."""
    in_ents = False
    ents: list[dict] = []
    cur: dict | None = None
    for code, val in pairs:
        if code == 2 and val == "ENTITIES":
            in_ents = True
            continue
        if code == 0 and val == "ENDSEC":
            if in_ents and cur is not None:
                ents.append(cur)
                cur = None
            in_ents = False
            continue
        if not in_ents:
            continue
        if code == 0:
            if cur is not None:
                ents.append(cur)
            cur = {"type": val, "layer": None, "codes": []}
        elif cur is not None:
            cur["codes"].append((code, val))
            if code == 8 and cur["layer"] is None:
                cur["layer"] = val
    return ents


def _header_vars(pairs: list[tuple[int, str]]) -> dict[str, list[tuple[int, str]]]:
    """Header variables: ``$NAME`` -> the following (code, value) pairs."""
    out: dict[str, list[tuple[int, str]]] = {}
    name: str | None = None
    in_header = False
    for code, val in pairs:
        if code == 2 and val == "HEADER":
            in_header = True
            continue
        if code == 0 and val == "ENDSEC":
            if in_header:
                break
            continue
        if not in_header:
            continue
        if code == 9:
            name = val
            out[name] = []
        elif name is not None:
            out[name].append((code, val))
    return out


# -- structure ----------------------------------------------------------------


def test_is_ac1015_with_required_sections_and_eof():
    dxf = to_dxf(_plan())
    pairs = _pairs(dxf)
    hv = _header_vars(pairs)
    assert hv["$ACADVER"][0] == (1, "AC1015")
    names = {v for c, v in pairs if c == 2}
    for section in ("HEADER", "TABLES", "BLOCKS", "ENTITIES", "OBJECTS"):
        assert section in names
    for record in ("*Model_Space", "*Paper_Space"):
        assert record in names
    assert dxf.rstrip().endswith("EOF")


def test_declares_imperial_units():
    hv = _header_vars(_pairs(to_dxf(_plan())))
    assert hv["$INSUNITS"][0] == (70, "2")       # feet
    assert hv["$MEASUREMENT"][0] == (70, "0")    # English
    assert hv["$LUNITS"][0] == (70, "4")         # architectural


def test_every_entity_layer_is_declared():
    dxf = to_dxf(_plan())
    pairs = _pairs(dxf)
    declared = {v for c, v in pairs if c == 2}  # includes table LAYER names
    for name in LAYERS:
        assert name in declared
    for ent in _entities(pairs):
        assert ent["layer"] in declared, ent


def test_walls_are_closed_lwpolylines():
    ents = _entities(_pairs(to_dxf(_plan())))
    walls = [e for e in ents if e["type"] == "LWPOLYLINE" and e["layer"] == "A-WALL"]
    assert walls, "expected wall polylines"
    for w in walls:
        codes = dict(w["codes"])
        assert codes[70] == "1"  # closed flag


def test_arc_count_equals_swing_door_count():
    # Every door in SRC is a single swing (2 interior + 1 entry), so one 90°
    # swing arc each — a clean ARC-per-door invariant.
    plan = _plan()
    ents = _entities(_pairs(to_dxf(plan)))
    arcs = [e for e in ents if e["type"] == "ARC"]
    assert len(arcs) == len(plan.interior_doors) + len(plan.exterior_doors)
    assert all(e["layer"] == "A-DOOR" for e in arcs)


def test_windows_land_on_the_glazing_layer():
    plan = _plan()
    ents = _entities(_pairs(to_dxf(plan)))
    glaz = [e for e in ents if e["layer"] == "A-GLAZ"]
    # Five lines per window (two faces, glazing centre, two jambs).
    assert len(glaz) == 5 * len(plan.windows)


def test_extents_enclose_the_plan_and_the_dimension_offsets():
    plan = _plan()
    hv = _header_vars(_pairs(to_dxf(plan)))
    emin = {c: v for c, v in hv["$EXTMIN"]}
    emax = {c: v for c, v in hv["$EXTMAX"]}
    minx, miny = float(emin[10]), float(emin[20])
    maxx, maxy = float(emax[10]), float(emax[20])
    fx0, fy0, fx1, fy1 = plan.bounds()
    # Bounds enclosed, and the overall dim string (3 ft outside) is inside extents.
    assert minx <= fx0 - 3.0 + 1e-6
    assert miny <= fy0 - 3.0 + 1e-6
    assert maxx >= fx1 - 1e-6
    assert maxy >= fy1 - 1e-6


def test_dimension_text_matches_the_svg_chain_labels():
    plan = _plan(CHAIN_SRC)
    dxf = to_dxf(plan)
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    # The SVG draws the chain in unicode primes; the DXF folds them to ASCII.
    for seg in ("12", "8"):
        assert f"{seg}′" in svg          # SVG chain segment
        assert f"{seg}'" in dxf          # same measurement, ASCII feet mark
    texts = [v for (c, v) in _pairs(dxf) if c == 1]
    assert "12'" in texts and "8'" in texts


def test_no_nan_or_inf_coordinates():
    dxf = to_dxf(_plan())
    for code, val in _pairs(dxf):
        if code in (10, 11, 20, 21, 30, 31, 40, 50, 51):
            f = float(val)
            assert not math.isnan(f) and not math.isinf(f)


def test_multi_level_uses_suffixed_layers():
    src = (
        'plan "Two"\n'
        "envelope 20 x 20\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 20 x 20\n"
        "room b: loft at 0,0 size 20 x 20 level 1\n"
    )
    ents = _entities(_pairs(to_dxf(compile_source(src).plan)))
    layers = {e["layer"] for e in ents}
    assert "A-WALL" in layers
    assert "A-WALL-L1" in layers


def test_export_is_byte_deterministic():
    a = to_dxf(_plan())
    b = to_dxf(_plan())
    assert a == b


def test_non_ascii_names_are_sanitised():
    src = SRC.replace('"DXF Test"', '"Café Plan"')
    plan = compile_source(src).plan
    plan.rooms[0].label = "Séjour"
    dxf = to_dxf(plan)
    assert dxf.isascii()


def test_save_dxf_writes_file(tmp_path):
    out = tmp_path / "plan.dxf"
    save_dxf(_plan(), str(out))
    assert out.exists()
    assert out.read_text().rstrip().endswith("EOF")


# -- ezdxf oracle (skipped when ezdxf isn't installed) ------------------------


def test_ezdxf_reads_and_audits_clean():
    ezdxf = pytest.importorskip("ezdxf")
    import io

    for src in (SRC, CHAIN_SRC):
        plan = compile_source(src).plan
        doc = ezdxf.read(io.StringIO(to_dxf(plan)))
        assert doc.dxfversion == "AC1015"
        auditor = doc.audit()
        assert not auditor.errors
        assert not auditor.fixes
        # The wall polylines really are closed in the parsed model.
        walls = [e for e in doc.modelspace() if e.dxftype() == "LWPOLYLINE"]
        assert walls and all(e.closed for e in walls)
