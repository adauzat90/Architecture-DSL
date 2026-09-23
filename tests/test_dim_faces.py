"""Phase 18 — face-of-stud dimension convention (``dim_mode = "faces"``).

The default ``"nominal"`` mode dimensions to the model's room lines (partition
centrelines / nominal envelope face) and is byte-for-byte the historical drawing.
``"faces"`` mode is the professional convention: overall dims run outside-face to
outside-face, and each interior room break becomes the two faces of the wall
crossing there — a clear-width segment flanked by thin wall-thickness segments,
sourced from the shared ``wallbodies`` band geometry. These pin the hand math,
the pixel-exact face sourcing, both-mode SVG/DXF parity, and default byte-identity.
"""

from __future__ import annotations

from barndsl import compile_source, render_svg, to_dxf
from barndsl.constants import (
    EXTERIOR_WALL_THICKNESS,
    INTERIOR_WALL_THICKNESS,
    PLUMBING_WALL_THICKNESS,
)
from barndsl.drawing import (
    chain_breaks,
    chain_ticks,
    exterior_runs,
    opening_jambs,
    overall_span,
    run_breaks,
    wall_faces,
)
from barndsl.render import RenderConfig, _Renderer, fmt_ft_in
from barndsl.wallbodies import wall_bands

# A 12′ room (kitchen) between two 4.5 in interior partitions on the south wall:
# rooms a(10) | kitchen(12) | c(10) partition the 32 ft south wall. The middle
# room's clear width is the acceptance-bar hand-math fixture.
_HANDMATH = (
    'plan "Handmath"\n'
    "envelope 32 x 12\n"
    "ceiling 9\n"
    "room a: living at 0,0 size 10 x 12\n"
    "room kitchen: kitchen at 10,0 size 12 x 12\n"
    "room c: bedroom at 22,0 size 10 x 12\n"
)


def _faces_ticks(plan, side="S"):
    fx0, fy0, fx1, fy1 = plan.bounds()
    room_pts, lo, hi = chain_breaks(side, plan.rooms, fx0, fy0, fx1, fy1)
    wall = {"S": fy0, "N": fy1, "W": fx0, "E": fx1}[side]
    jambs = opening_jambs(plan, side, plan.rooms, wall, lo, hi)
    return chain_ticks(side, room_pts, jambs, lo, hi, "faces", wall_bands(plan, 0)), (lo, hi)


# -- hand math ---------------------------------------------------------------


def test_faces_chain_hand_math_12ft_room_reads_clear_between_two_partitions():
    plan = compile_source(_HANDMATH).plan
    ticks, (lo, hi) = _faces_ticks(plan, "S")
    segs = [round(b - a, 6) for a, b in zip(ticks, ticks[1:])]
    half = INTERIOR_WALL_THICKNESS / 2.0
    # ticks: outside face | a's part face pair | kitchen's part face pair | out
    # -0.27 | 9.8125 10.1875 | 21.8125 22.1875 | 32.27
    clear_kitchen = 12.0 - INTERIOR_WALL_THICKNESS
    assert clear_kitchen == round(11 + 7.5 / 12, 6)  # 11′-7½″
    assert fmt_ft_in(clear_kitchen) == "11′-7½″"
    # The middle (clear) segment and the two 4½″ thickness segments are present.
    assert round(clear_kitchen, 6) in segs
    thickness = round(INTERIOR_WALL_THICKNESS, 6)
    assert segs.count(thickness) == 2
    assert fmt_ft_in(INTERIOR_WALL_THICKNESS) == "4½″"
    # Faces are exactly ± half a partition off the nominal centrelines (10, 22).
    assert 10.0 - half in ticks and 10.0 + half in ticks
    assert 22.0 - half in ticks and 22.0 + half in ticks


def test_faces_chain_segments_sum_to_the_overall_invariant():
    plan = compile_source(_HANDMATH).plan
    ticks, _ = _faces_ticks(plan, "S")
    segs_sum = ticks[-1] - ticks[0]
    fx0, _, fx1, _ = plan.bounds()
    lo2, hi2 = overall_span(fx0, fx1, "faces")
    overall = hi2 - lo2
    # The chain partitions [lo-ext, hi+ext], so segments always sum to the
    # faces-mode overall — nominal span plus one full exterior thickness.
    assert abs(segs_sum - overall) < 1e-9
    assert abs(overall - ((fx1 - fx0) + EXTERIOR_WALL_THICKNESS)) < 1e-9


def test_faces_overall_is_outside_face_to_outside_face():
    plan = compile_source(_HANDMATH).plan
    nominal = render_svg(plan, RenderConfig(dim_mode="nominal", show_room_dims=False))
    faces = render_svg(plan, RenderConfig(dim_mode="faces", show_room_dims=False))
    assert "32′" in nominal              # nominal overall width = 32 ft
    assert "32′-6½″" in faces            # + one 6½″ exterior wall, face to face
    assert "12′-6½″" in faces            # length likewise (12 + 6½″)


# -- face coordinates come from the shared wallbodies band -------------------


def test_face_tick_lands_pixel_exact_on_the_drawn_band_edge():
    # A face tick's screen x must equal the drawn poché band's face x exactly —
    # both come from the same wallbodies geometry, so the number is byte-shared.
    plan = compile_source(_HANDMATH).plan
    r = _Renderer(plan, RenderConfig(dim_mode="faces", show_room_dims=False))
    svg = r.render()
    bands = wall_bands(plan, 0)
    # The vertical partition centred on x=10 (between a and kitchen).
    part = next(
        b for b in bands
        if b.orientation == "v" and abs((b.x0 + b.x1) / 2 - 10.0) < 1e-6
        and b.kind == "interior"
    )
    face_x = r.sx(min(b for b in (part.x0, part.x1)))
    # The band rect draws that face at this screen x; a chain tick sits on it too.
    assert f'x="{face_x:.1f}"' in svg  # the poché rect's left face
    # And the chain's face computation returns exactly the band's faces.
    near, far = wall_faces("S", 10.0, bands)
    assert (near, far) == (min(part.x0, part.x1), max(part.x0, part.x1))


def test_plumbing_wall_gives_thicker_face_spacing_than_a_2x4_partition():
    # A declared plumbing (wet) wall is the thicker 2x6 — its two faces spread by
    # the real 6½″, not the ordinary 4½″, straight from the band geometry.
    src = (
        'plan "Wet"\n'
        "envelope 30 x 12\n"
        "ceiling 9\n"
        "room bath: bathroom at 0,0 size 10 x 12\n"
        "room hall: hallway at 10,0 size 10 x 12\n"
        "room bed: bedroom at 20,0 size 10 x 12\n"
        "wall bath - hall plumbing\n"
    )
    plan = compile_source(src).plan
    bands = wall_bands(plan, 0)
    wet_near, wet_far = wall_faces("S", 10.0, bands)   # bath|hall = plumbing
    dry_near, dry_far = wall_faces("S", 20.0, bands)   # hall|bed = ordinary
    assert abs((wet_far - wet_near) - PLUMBING_WALL_THICKNESS) < 1e-9
    assert abs((dry_far - dry_near) - INTERIOR_WALL_THICKNESS) < 1e-9
    assert (wet_far - wet_near) > (dry_far - dry_near)


# -- opening jambs stay put --------------------------------------------------


def test_faces_mode_leaves_opening_jambs_untouched():
    src = (
        'plan "Jamb"\n'
        "envelope 40 x 24\n"
        "ceiling 9\n"
        "room living: living at 0,0 size 40 x 24\n"
        "entry living south width 3 offset 6\n"
        "window living south width 4 offset 14\n"
    )
    plan = compile_source(src).plan
    fx0, fy0, fx1, fy1 = plan.bounds()
    room_pts, lo, hi = chain_breaks("S", plan.rooms, fx0, fy0, fx1, fy1)
    jambs = opening_jambs(plan, "S", plan.rooms, fy0, fx0, fx1)
    ticks = chain_ticks("S", room_pts, jambs, lo, hi, "faces", wall_bands(plan, 0))
    # No interior room break — only jambs. Each jamb tick stays at its exact
    # face-of-opening coordinate; only the two span ends move out by half a wall.
    ext = EXTERIOR_WALL_THICKNESS / 2.0
    assert ticks[0] == lo - ext and ticks[-1] == hi + ext
    for j in (6.0, 9.0, 14.0, 18.0):
        assert j in ticks


# -- both-mode SVG <-> DXF parity --------------------------------------------


def _dxf_texts(dxf: str) -> list[str]:
    lines = dxf.split("\n")
    out = []
    for i in range(0, len(lines) - 1, 2):
        if lines[i].strip() == "1":
            out.append(lines[i + 1])
    return out


def test_svg_dxf_chain_labels_agree_in_both_modes():
    plan = compile_source(_HANDMATH).plan
    for mode in ("nominal", "faces"):
        svg = render_svg(plan, RenderConfig(dim_mode=mode, show_room_dims=False))
        dxf = to_dxf(plan, dim_mode=mode)
        texts = _dxf_texts(dxf)
        if mode == "nominal":
            # 12 ft clear kitchen segment in both, ASCII feet mark in the DXF.
            assert "12′" in svg
            assert "12'" in texts
        else:
            # 11′-7½″ clear in the SVG; the DXF spells the fraction in ASCII.
            assert "11′-7½″" in svg
            assert "11'-7 1/2\"" in texts


def test_dxf_stays_ascii_in_faces_mode():
    plan = compile_source(_HANDMATH).plan
    dxf = to_dxf(plan, dim_mode="faces")
    assert dxf.isascii()


# -- default byte-identity ---------------------------------------------------


def test_default_mode_is_byte_identical_svg_and_dxf():
    plan = compile_source(_HANDMATH).plan
    # An explicit "nominal" and the default RenderConfig must be identical, and
    # both must match the no-config call — the default output is unchanged.
    assert render_svg(plan) == render_svg(plan, RenderConfig(dim_mode="nominal"))
    assert render_svg(plan) == render_svg(plan, RenderConfig())
    assert to_dxf(plan) == to_dxf(plan, dim_mode="nominal")
    # Faces genuinely differs (guards against a no-op wiring bug).
    assert render_svg(plan, RenderConfig(dim_mode="faces")) != render_svg(plan)
    assert to_dxf(plan, dim_mode="faces") != to_dxf(plan)


# -- wing plan in faces mode -------------------------------------------------


def _lshape_plan():
    with open("examples/gallery/lshape.barn", encoding="utf-8") as fh:
        return compile_source(fh.read()).plan


def test_wing_plan_renders_faces_mode_with_exterior_runs_per_wing():
    plan = _lshape_plan()
    faces = render_svg(plan, RenderConfig(dim_mode="faces", show_room_dims=False))
    nominal = render_svg(plan, RenderConfig(dim_mode="nominal", show_room_dims=False))
    # Both render; faces differs from nominal (double ticks + moved ends).
    assert faces and faces != nominal
    # Each notched exterior run still produces its own chain — the faces
    # transform is applied per run, so the drawing has the wing suite's chain.
    rooms = plan.rooms
    bands = wall_bands(plan, 0)
    for offset, lo, hi in exterior_runs(plan, "E"):
        room_pts = run_breaks("E", rooms, offset, lo, hi)
        jambs = opening_jambs(plan, "E", rooms, offset, lo, hi)
        ticks = chain_ticks("E", room_pts, jambs, lo, hi, "faces", bands)
        # Ends moved out by half an exterior wall on every run.
        ext = EXTERIOR_WALL_THICKNESS / 2.0
        assert abs(ticks[0] - (lo - ext)) < 1e-9
        assert abs(ticks[-1] - (hi + ext)) < 1e-9
