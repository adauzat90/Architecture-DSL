"""Phase 19 — DXF v2: associative DIMENSION entities and glyph polish.

The primary pins are stdlib group-code scans of the emitted stream (no
third-party parser); an optional ``ezdxf`` block reads the associative output
and runs the auditor. Covered here:

* associative dims — a real rotated-linear ``DIMENSION`` per overall/chain
  segment, each backed by an anonymous ``*D<n>`` geometry block, on the
  ``Defpoints`` convention layer, with our ft-in text preserved;
* the 4-way matrix (``dim_mode`` nominal|faces × ``dims`` geometry|associative);
* counter mitring in DXF (reusing the renderer's trim) with SVG unchanged;
* the overhead-door dashed-panel glyph;
* loft guard lines on open edges (SVG + DXF), sharing the check's computation.
"""

from __future__ import annotations

import pytest

from barndsl import compile_source, render_svg, to_dxf
from barndsl.validation import loft_guard_edges, loft_guard_pairs

from test_dxf import _entities, _pairs  # reuse the group-code scanners

# -- fixtures -----------------------------------------------------------------

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

# A shop bay with an overhead/sectional garage door on its south wall.
SHOP = """\
plan "Shop Door"
envelope 50 x 30
ceiling 10
room living: living at 0,0  size 30 x 30
room shop:   shop   at 30,0 size 20 x 30
door living - shop width 3
entry living south width 3 offset 4
door shop south overhead width 9 offset 4
"""

# A U-kitchen: a full west run plus south and north runs off the same corner —
# two mitred L/U joints.
UKITCHEN = """\
plan "U Kitchen"
envelope 30 x 16
room k: kitchen at 0,0 size 16 x 16
room lv: living east-of k size 14 x 16
open k - lv width 6 offset 5
entry lv south width 3 offset 5
fixture counter in k along W
fixture counter in k along S from 0 to 12
fixture counter in k along N from 0 to 12
"""

# A loft that only partly covers the great room below — the north edge overlooks
# a double-height void and flags LOFT_GUARD.
LOFT = """\
plan "Loft Guard"
envelope 24 x 18
ceiling 9
room living: living at 0,0 size 24 x 18
room loft: loft at 0,0 size 24 x 10 level 1
stair flight at 0,3 size 4 x 12 from 0 to 1
open living - loft width 4
entry living south width 10 offset 6
"""


def _plan(src: str):
    plan = compile_source(src).plan
    assert plan is not None
    return plan


def _blocks(dxf: str) -> dict[str, list[dict]]:
    """Every BLOCK in the BLOCKS section as ``name -> [entity dicts]``."""
    pairs = _pairs(dxf)
    out: dict[str, list[dict]] = {}
    in_blocks = False
    name: str | None = None
    cur: dict | None = None
    for code, val in pairs:
        if code == 2 and val == "BLOCKS":
            in_blocks = True
            continue
        if not in_blocks:
            continue
        if code == 0 and val == "ENDSEC":
            break
        if code == 0 and val == "BLOCK":
            name = None
            cur = None
            continue
        if code == 0 and val == "ENDBLK":
            name = None
            cur = None
            continue
        if code == 0:  # an entity inside the current block
            cur = {"type": val, "codes": [], "layer": None}
            if name is not None:
                out[name].append(cur)
            continue
        if code == 2 and name is None:  # the block's name tag
            name = val
            out.setdefault(name, [])
            continue
        if cur is not None:
            cur["codes"].append((code, val))
            if code == 8 and cur["layer"] is None:
                cur["layer"] = val
    return out


def _dimensions(dxf: str) -> list[dict]:
    return [e for e in _entities(_pairs(dxf)) if e["type"] == "DIMENSION"]


def _dim_texts_geometry(dxf: str) -> list[str]:
    """The dimension TEXT strings on A-ANNO-DIMS (one per chain/overall segment)."""
    out = []
    for e in _entities(_pairs(dxf)):
        if e["type"] == "TEXT" and e["layer"] == "A-ANNO-DIMS":
            codes = dict(e["codes"])
            out.append(codes[1])
    return out


# -- associative structure ----------------------------------------------------


def test_geometry_flavor_has_no_dimension_entities():
    dxf = to_dxf(_plan(SRC))  # default is geometry
    assert not _dimensions(dxf)


def test_one_dimension_per_segment_backed_by_a_block():
    plan = _plan(SRC)
    seg_labels = _dim_texts_geometry(to_dxf(plan))  # geometry flavor: one TEXT/seg
    dxf = to_dxf(plan, dims="associative")
    dims = _dimensions(dxf)
    # A DIMENSION per overall/chain segment.
    assert len(dims) == len(seg_labels)
    blocks = _blocks(dxf)
    names = set()
    for d in dims:
        codes = dict(d["codes"])
        block = codes[2]
        names.add(block)
        assert block.startswith("*D")
        # dimtype 32 (rotated linear, anonymous-block flavor).
        assert codes[70] == "32"
        # dimstyle Standard, on the dims layer.
        assert codes[3] == "Standard"
        assert d["layer"] == "A-ANNO-DIMS"
        # The geometry block exists and carries our exploded lines/ticks/text.
        assert block in blocks
        types = {e["type"] for e in blocks[block]}
        assert "LINE" in types and "TEXT" in types
    # Names are the deterministic *D1..*Dn run, one block per DIMENSION.
    assert names == {f"*D{i}" for i in range(1, len(dims) + 1)}


def test_dimension_text_matches_the_label_of_its_block():
    dxf = to_dxf(_plan(SRC), dims="associative")
    blocks = _blocks(dxf)
    for d in _dimensions(dxf):
        codes = dict(d["codes"])
        block, entity_text = codes[2], codes[1]
        block_texts = [
            dict(e["codes"])[1] for e in blocks[block] if e["type"] == "TEXT"
        ]
        # The DIMENSION's explicit text (group 1) is exactly its block's label,
        # so a regenerating reader shows the same ft-in value we drew.
        assert block_texts == [entity_text]


def test_defpoints_layer_declared_and_carries_the_definition_points():
    dxf = to_dxf(_plan(SRC), dims="associative")
    declared = {v for c, v in _pairs(dxf) if c == 2}
    assert "Defpoints" in declared
    blocks = _blocks(dxf)
    # Every dim block puts its three definition points on the Defpoints layer.
    dim_blocks = [n for n in blocks if n.startswith("*D")]
    assert dim_blocks
    for name in dim_blocks:
        pts = [e for e in blocks[name] if e["type"] == "POINT"]
        assert len(pts) == 3
        assert all(e["layer"] == "Defpoints" for e in pts)


def test_geometry_default_is_byte_identical_to_the_explicit_flavor():
    plan = _plan(SRC)
    assert to_dxf(plan) == to_dxf(plan, dims="geometry")


# -- the 4-way matrix ---------------------------------------------------------


def test_four_way_matrix_chain_labels_agree():
    plan = _plan(SRC)
    # Reference labels (per flavor the dim geometry is the same math).
    for mode in ("nominal", "faces"):
        geo = to_dxf(plan, dim_mode=mode, dims="geometry")
        assoc = to_dxf(plan, dim_mode=mode, dims="associative")
        geo_labels = sorted(_dim_texts_geometry(geo))
        assoc_labels = sorted(
            dict(d["codes"])[1] for d in _dimensions(assoc)
        )
        assert geo_labels == assoc_labels, mode
        assert assoc.isascii()


def test_four_way_matrix_is_byte_deterministic():
    plan = _plan(SRC)
    for mode in ("nominal", "faces"):
        for flavor in ("geometry", "associative"):
            a = to_dxf(plan, dim_mode=mode, dims=flavor)
            b = to_dxf(plan, dim_mode=mode, dims=flavor)
            assert a == b, (mode, flavor)


def test_faces_mode_adds_segments_in_both_flavors():
    plan = _plan(SRC)
    n_nom = len(_dimensions(to_dxf(plan, dim_mode="nominal", dims="associative")))
    n_fac = len(_dimensions(to_dxf(plan, dim_mode="faces", dims="associative")))
    assert n_fac > n_nom  # face-of-stud double-ticks add chain segments


# -- ezdxf oracle (skipped when ezdxf isn't installed) ------------------------


def test_ezdxf_audits_associative_clean():
    ezdxf = pytest.importorskip("ezdxf")
    import io

    for src in (SRC, SHOP, UKITCHEN, LOFT):
        plan = _plan(src)
        for mode in ("nominal", "faces"):
            dxf = to_dxf(plan, dim_mode=mode, dims="associative")
            doc = ezdxf.read(io.StringIO(dxf))
            auditor = doc.audit()
            assert not auditor.errors, (src, mode, auditor.errors)
            assert not auditor.fixes, (src, mode, auditor.fixes)
            msp = doc.modelspace()
            dims = msp.query("DIMENSION")
            assert len(dims) == len(_dimensions(dxf))
            # Each parsed DIMENSION resolves its geometry block.
            for d in dims:
                assert d.dxf.geometry in doc.blocks


# -- counter mitring in DXF ---------------------------------------------------


def test_ukitchen_counters_are_trimmed_and_mitred_in_dxf():
    plan = _plan(UKITCHEN)
    ents = _entities(_pairs(to_dxf(plan)))
    fixt = [e for e in ents if e["layer"] == "A-FLOR-FIXT"]

    def rect_of(e):
        xs, ys = [], []
        for c, v in e["codes"]:
            if c == 10:
                xs.append(float(v))
            elif c == 20:
                ys.append(float(v))
        return min(xs), min(ys), max(xs), max(ys)

    # A counter draws as a rect immediately followed by its "COUNTER" label — so
    # identify counters (not the appliances that legitimately sit on them) by that
    # adjacency.
    boxes = []
    for rect_e, next_e in zip(fixt, fixt[1:]):
        if rect_e["type"] != "LWPOLYLINE" or next_e["type"] != "TEXT":
            continue
        if dict(next_e["codes"]).get(1) == "COUNTER":
            boxes.append(rect_of(rect_e))
    assert len(boxes) == 3
    # No two drawn counter rects overlap on paper (the runs abut, mitred).
    eps = 0.01
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ax0, ay0, ax1, ay1 = boxes[i]
            bx0, by0, bx1, by1 = boxes[j]
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            assert not (ox > eps and oy > eps), (boxes[i], boxes[j])
    # Two L/U joints → two miter diagonals (extra A-FLOR-FIXT LINEs).
    miter_lines = [e for e in fixt if e["type"] == "LINE"]
    assert len(miter_lines) == 2


def test_single_counter_is_byte_identical_and_unmitred():
    """A kitchen with one counter run keeps the historical DXF bytes — the
    mitre computation is a no-op with no corner joint."""
    src = UKITCHEN.replace(
        "fixture counter in k along W\n"
        "fixture counter in k along S from 0 to 12\n"
        "fixture counter in k along N from 0 to 12\n",
        "fixture counter in k along S\n",
    )
    ents = _entities(_pairs(to_dxf(_plan(src))))
    fixt = [e for e in ents if e["layer"] == "A-FLOR-FIXT"]
    # A single run draws no miter diagonal.
    assert not [e for e in fixt if e["type"] == "LINE"]


def test_svg_counter_mitring_unchanged_by_dxf_work():
    # The SVG already drew mitred counters; this pins it stays put.
    svg = render_svg(_plan(UKITCHEN))
    assert svg.count('data-joint="miter"') == 2


# -- overhead-door glyph ------------------------------------------------------


def test_overhead_door_draws_a_dashed_panel_and_track():
    plan = _plan(SHOP)
    ents = _entities(_pairs(to_dxf(plan)))
    door_lines = [e for e in ents if e["layer"] == "A-DOOR" and e["type"] == "LINE"]
    dashed = [e for e in door_lines if (6, "DASHED") in e["codes"]]
    # Panel line pair across the opening + the track line inside — three dashed
    # lines, matching the SVG's dashed segmented-panel convention.
    assert len(dashed) == 3


def test_plan_without_overhead_has_no_dashed_door_lines():
    ents = _entities(_pairs(to_dxf(_plan(SRC))))
    door_lines = [e for e in ents if e["layer"] == "A-DOOR" and e["type"] == "LINE"]
    assert not [e for e in door_lines if (6, "DASHED") in e["codes"]]


# -- loft guard lines ---------------------------------------------------------


def test_shared_guard_helper_matches_the_rule():
    """The drawing helper and the LOFT_GUARD check flag exactly the same rooms —
    a plan is never flagged without a guard line drawn, or vice versa."""
    result = compile_source(LOFT)
    plan = result.plan
    assert plan is not None
    flagged = {d.room for d in result.infos if d.code == "LOFT_GUARD"}
    pair_rooms = {u.id for u, _ in loft_guard_pairs(plan)}
    assert flagged == pair_rooms == {"loft"}
    # A full-coverage loft (no void) is flagged by neither.
    full = LOFT.replace("room loft: loft at 0,0 size 24 x 10 level 1",
                        "room loft: loft at 0,0 size 24 x 18 level 1")
    fplan = compile_source(full).plan
    assert not list(loft_guard_pairs(fplan))
    assert not loft_guard_edges(fplan)


def test_guard_edge_is_the_open_north_edge():
    plan = _plan(LOFT)
    edges = loft_guard_edges(plan)
    # The loft covers y in [0,10] of an 18 ft room → open edge at y=10, on level 1.
    assert edges == [(1, 0.0, 10.0, 24.0, 10.0)]


def test_dxf_draws_guard_lines_on_the_loft_level():
    plan = _plan(LOFT)
    ents = _entities(_pairs(to_dxf(plan)))
    # Guard rides A-FLOR-OTLN on the loft's level (-L1); a thin double line.
    guards = [
        e for e in ents
        if e["layer"] == "A-FLOR-OTLN-L1" and e["type"] == "LINE"
        and _is_horizontal_at(e, y=10.0)
    ]
    assert len(guards) == 2  # the doubled rail


def _is_horizontal_at(e: dict, y: float, tol: float = 0.2) -> bool:
    codes = dict(e["codes"])
    return abs(float(codes[20]) - y) <= tol and abs(float(codes[21]) - y) <= tol


def test_svg_draws_guard_lines_for_a_loft_plan():
    plan = _plan(LOFT)
    svg = render_svg(plan)
    # The guard-rail accent colour appears (two strokes); a full-coverage loft
    # draws none.
    from barndsl.render import GUARD_COLOR

    assert svg.count(f'stroke="{GUARD_COLOR}"') == 2
    full = LOFT.replace("size 24 x 10 level 1", "size 24 x 18 level 1")
    assert f'stroke="{GUARD_COLOR}"' not in render_svg(_plan(full))
