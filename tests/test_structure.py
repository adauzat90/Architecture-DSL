"""Tests for automatic post-and-beam frame placement (`frame` directive)."""

from __future__ import annotations



from barndsl import (
    Beam,
    Post,
    barndominium,
    compile_source,
    emit_dsl,
    inches,
    place_frame,
)
from barndsl import RoomType as T
from barndsl.diagnostics import REGISTRY


def _plan(frame_line: str = "frame bay 12 span 40 post 6", env: str = "60 x 40"):
    src = f"""\
plan "Frame"
envelope {env}
ceiling 12
{frame_line}
room living: living at 0,0 size {env.split(' x ')[0]} x {env.split(' x ')[1]}
entry living south width 3 offset 20
entry living north width 3 offset 24
window living east width 6 offset 10
window living west width 6 offset 10
"""
    return compile_source(src)


# --- placement geometry ------------------------------------------------------


def test_frame_places_bents_and_a_ridge():
    p = _plan().plan
    frames = [b for b in p.beams if b.role == "frame"]
    ridges = [b for b in p.beams if b.role == "ridge"]
    # 60 ft long / 12 ft bay = 5 bays => 6 frame lines, each spanning the 40 ft width.
    assert len(frames) == 6
    assert all(abs(b.length - 40) < 1e-6 for b in frames)
    # One ridge running the full 60 ft length.
    assert len(ridges) == 1 and abs(ridges[0].length - 60) < 1e-6
    assert ridges[0].orientation == "h"
    assert frames[0].orientation == "v"


def test_ridge_runs_along_the_long_axis_when_tall():
    # A north-south-long building: ridge should run in y, frames span x.
    p = _plan(env="30 x 72").plan
    ridge = next(b for b in p.beams if b.role == "ridge")
    assert ridge.orientation == "v" and abs(ridge.length - 72) < 1e-6
    frame = next(b for b in p.beams if b.role == "frame")
    assert frame.orientation == "h" and abs(frame.length - 30) < 1e-6


def test_no_ridge_option_omits_the_ridge():
    p = _plan(frame_line="frame bay 12 span 40 post 6 no-ridge").plan
    assert not [b for b in p.beams if b.role == "ridge"]
    assert p.frame_spec is not None and p.frame_spec.ridge is False


def test_beam_linear_feet_and_counts_in_metrics():
    p = _plan().plan
    m = p.metrics()
    assert m["frame_count"] == 6
    assert m["beam_count"] == 7  # 6 frames + 1 ridge
    assert abs(m["beam_linear_ft"] - (6 * 40 + 60)) < 1e-6
    assert m["post_count"] == len(p.posts) > 0


def test_corner_posts_present_and_unique():
    p = _plan().plan
    corners = {(0.0, 0.0), (60.0, 0.0), (0.0, 40.0), (60.0, 40.0)}
    have = {(round(pt.x, 3), round(pt.y, 3)) for pt in p.posts}
    assert corners <= have
    # No coincident posts after dedupe.
    assert len(have) == len(p.posts)


def test_interior_posts_appear_only_when_span_exceeded():
    narrow = _plan(env="60 x 40").plan  # 40 <= span 40 → no interior support
    assert not [pt for pt in narrow.posts if pt.role == "interior"]
    wide = _plan(frame_line="frame bay 12 span 24 post 6", env="60 x 40").plan
    interior = [pt for pt in wide.posts if pt.role == "interior"]
    assert interior  # 40 ft width / 24 ft max span → a support line is added


# --- determinism / round-trip ------------------------------------------------


def test_placement_is_deterministic_and_idempotent():
    p = _plan().plan
    before = [(b.x1, b.y1, b.x2, b.y2, b.role) for b in p.beams]
    place_frame(p)  # re-run
    after = [(b.x1, b.y1, b.x2, b.y2, b.role) for b in p.beams]
    assert before == after


def test_frame_round_trips_through_emit():
    src = emit_dsl(_plan().plan)
    assert any(line.startswith("frame ") for line in src.splitlines())
    again = compile_source(src)
    assert again.plan is not None and not again.errors
    # Fixed point: emitting the recompiled plan reproduces the source.
    assert emit_dsl(again.plan) == src


def test_post_section_round_trips_in_inches():
    p = _plan(frame_line="frame bay 12 span 40 post 8").plan
    assert abs(p.frame_spec.post - inches(8)) < 1e-9
    src = emit_dsl(p)
    assert "post 8" in next(l for l in src.splitlines() if l.startswith("frame"))


def test_builder_frame_matches_the_compiler():
    built = (
        barndominium("Frame")
        .envelope(60, 40)
        .ceiling(12)
        .add_room("living", T.LIVING, x=0, y=0, width=60, length=40)
        .frame(bay=12, span=40, post=inches(6))
    )
    compiled = _plan().plan
    assert len(built.beams) == len(compiled.beams)
    assert len(built.posts) == len(compiled.posts)


# --- diagnostics -------------------------------------------------------------


def test_bay_wide_info_for_a_heavy_spacing():
    r = _plan(frame_line="frame bay 18 span 40 post 6")
    assert "BAY_WIDE" in {d.code for d in r.infos}


def test_bay_wide_silent_at_twelve_feet():
    r = _plan(frame_line="frame bay 12 span 40 post 6")
    assert "BAY_WIDE" not in {d.code for d in r.infos}


def test_post_obstruct_info_for_a_stranded_interior_post():
    r = _plan(frame_line="frame bay 12 span 24 post 6", env="60 x 40")
    obstruct = [d for d in r.infos if d.code == "POST_OBSTRUCT"]
    assert obstruct and obstruct[0].room == "living"


def test_structure_codes_are_registered():
    for code in ("POST_OBSTRUCT", "BAY_WIDE", "POST_IN_OPENING"):
        assert code in REGISTRY


def test_post_in_opening_flags_a_window_over_a_post():
    # 60 ft / 12 ft bay → a post at x=12 on the south wall; a window spanning
    # x=8..16 there stands on it.
    src = """\
plan "Clash"
envelope 60 x 40
ceiling 12
frame bay 12 span 40 post 6
room a: living at 0,0 size 60 x 40
entry a south width 3 offset 30
window a south width 8 offset 8
"""
    r = compile_source(src)
    clash = [d for d in r.warnings if d.code == "POST_IN_OPENING"]
    assert clash and clash[0].room == "a"


def test_post_at_a_jamb_does_not_flag():
    # A window whose edge lands on a post (x=12) is fine — that's how an opening
    # is framed. Window x=12..20 has its jamb on the post, nothing inside it.
    src = """\
plan "Jamb"
envelope 60 x 40
ceiling 12
frame bay 12 span 40 post 6
room a: living at 0,0 size 60 x 40
entry a south width 3 offset 30
window a south width 8 offset 12
"""
    r = compile_source(src)
    assert "POST_IN_OPENING" not in {d.code for d in r.warnings}


def test_post_in_opening_flags_an_exterior_door():
    src = """\
plan "DoorClash"
envelope 60 x 40
ceiling 12
frame bay 12 span 40 post 6
room a: living at 0,0 size 60 x 40
entry a south width 6 offset 9
window a north width 4 offset 4
"""
    r = compile_source(src)
    clash = [d for d in r.warnings if d.code == "POST_IN_OPENING"]
    assert clash and "exterior door" in clash[0].message


def test_frame_demo_has_no_post_window_conflicts():
    from pathlib import Path

    from barndsl import compile_file

    demo = Path(__file__).resolve().parent.parent / "examples" / "frame_demo.barn"
    r = compile_file(str(demo))
    assert "POST_IN_OPENING" not in {d.code for d in r.warnings}


def test_no_frame_means_no_structure_and_no_structural_infos():
    src = """\
plan "Plain"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 40 x 30
entry living south width 3 offset 10
window living north width 8 offset 10
"""
    r = compile_source(src)
    assert r.plan.frame_spec is None
    assert not r.plan.beams and not r.plan.posts
    assert "BAY_WIDE" not in {d.code for d in r.infos}


# --- parser errors -----------------------------------------------------------


def test_unknown_frame_option_is_a_parse_error():
    r = compile_source(
        'plan "x"\nenvelope 40 x 30\nceiling 10\nframe bay 12 wobble 3\n'
        "room a: living at 0,0 size 40 x 30\n"
    )
    assert any(d.code == "BAD_OPTION" for d in r.errors)


def test_nonpositive_frame_value_is_a_parse_error():
    r = compile_source(
        'plan "x"\nenvelope 40 x 30\nceiling 10\nframe bay 0\n'
        "room a: living at 0,0 size 40 x 30\n"
    )
    assert any(d.code == "BAD_NUMBER" for d in r.errors)


# --- wings -------------------------------------------------------------------


def test_frame_covers_each_footprint_section():
    src = """\
plan "L"
envelope 40 x 30
wing 20 x 20 at 40,0
ceiling 12
frame bay 12 span 40 post 6
room a: living at 0,0 size 40 x 30
room b: shop at 40,0 size 20 x 20
door a - b width 6
entry a south width 3 offset 10
window a north width 8 offset 10
window b east width 4 offset 8
"""
    p = compile_source(src).plan
    # Both blocks framed: ridge per section.
    assert len([b for b in p.beams if b.role == "ridge"]) == 2
    assert p.metrics()["beam_linear_ft"] > 0


def test_beam_and_post_orientation_helpers():
    assert Beam(0, 0, 10, 0).orientation == "h"
    assert Beam(0, 0, 0, 10).orientation == "v"
    assert abs(Beam(0, 0, 3, 4).length - 5) < 1e-9
    assert Post(1, 2).role == "post"
