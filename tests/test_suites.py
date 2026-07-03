"""Tests for the ``suite`` / ``zone`` statements (review §2.3).

Grammar (good/bad parses + recovery), the builder methods, every new
diagnostic (fired and not-fired), the sharpened design checks using declared
membership vs the unchanged inference fallback, emit round-trip, and the
introspect section.
"""

from __future__ import annotations

import pytest

from barndsl import barndominium, compile_source, emit_dsl
from barndsl.diagnostics import REGISTRY, explain
from barndsl.introspect import plan_summary, summary_text


def _codes(result, severity: str) -> set[str]:
    bucket = {
        "error": result.errors, "warning": result.warnings, "info": result.infos
    }[severity]
    return {d.code for d in bucket}


def _all_codes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


# A valid three-bedroom plan with a declared primary suite + private zone. The
# geometry is clean (compiles OK) so tests can toggle the declarations alone.
_BASE = """\
plan "Suites"
envelope 52 x 32
ceiling 10
room living:      living   at 0,0   size 52 x 15
room hall_beds:   hallway  at 0,15  size 52 x 3
room master_bed:  bedroom  at 0,18  size 16 x 14
room master_bath: bathroom at 16,18 size 8 x 14
room master_wic:  closet   at 24,18 size 4 x 14
room bed_2:       bedroom  at 28,18 size 12 x 14
room bed_3:       bedroom  at 40,18 size 12 x 14
door living - hall_beds width 4
door hall_beds - master_bed width 3 offset 1
door master_bed - master_bath width 2.67
door master_bed - master_wic width 2.5
door hall_beds - bed_2 width 3 offset 1
door hall_beds - bed_3 width 3 offset 1
entry living south width 3 offset 4
window living south width 12 offset 4
window master_bed north width 5 offset 4
window master_bath north width 3 offset 1
window bed_2 north width 4 offset 4
window bed_3 north width 4 offset 4
{decl}
"""


def _compile(decl: str = ""):
    return compile_source(_BASE.format(decl=decl))


# --- grammar: good parses ----------------------------------------------------


def test_suite_and_zone_parse_onto_the_plan():
    r = _compile(
        "suite primary: master_bed master_bath master_wic\n"
        "zone private: primary bed_2 bed_3 hall_beds"
    )
    assert r.plan is not None
    assert [(s.id, s.members) for s in r.plan.suites] == [
        ("primary", ("master_bed", "master_bath", "master_wic"))
    ]
    assert [(z.id, z.members) for z in r.plan.zones] == [
        ("private", ("primary", "bed_2", "bed_3", "hall_beds"))
    ]


def test_declaration_order_is_preserved():
    r = _compile(
        "suite a: master_bed\n"
        "suite b: bed_2\n"
        "zone z1: a\n"
        "zone z2: b"
    )
    assert [s.id for s in r.plan.suites] == ["a", "b"]
    assert [z.id for z in r.plan.zones] == ["z1", "z2"]


def test_colon_is_optional_like_the_room_statement():
    # The ':' tokenizes away (a separator), so it's optional sugar.
    r = _compile("suite primary master_bed master_bath")
    assert r.plan.suites[0].members == ("master_bed", "master_bath")


def test_duplicate_members_are_dropped_keeping_order():
    r = _compile("suite primary: master_bed master_bath master_bed")
    assert r.plan.suites[0].members == ("master_bed", "master_bath")


def test_source_locations_are_recorded():
    r = _compile("suite primary: master_bed master_bath")
    s = r.plan.suites[0]
    assert s.line is not None and s.col is not None and s.end_col is not None


# --- grammar: bad parses + recovery ------------------------------------------


def test_empty_suite_is_a_syntax_error():
    r = _compile("suite primary:")
    assert "SYNTAX" in _codes(r, "error")


def test_empty_zone_is_a_syntax_error():
    r = _compile("zone private:")
    assert "SYNTAX" in _codes(r, "error")


def test_suite_missing_id_is_an_error():
    r = _compile('suite "": master_bed')
    assert "EMPTY_ID" in _codes(r, "error")


def test_a_bad_suite_line_is_skipped_but_the_plan_survives():
    # Statement-level recovery: the malformed suite is dropped (recovered=True),
    # the rest of the plan still validates.
    r = _compile("suite primary:")
    assert r.recovered is True
    assert r.plan is not None and r.plan.rooms  # survivors kept
    # A valid line after the bad one still parses.
    r2 = _compile("suite primary:\nsuite two: bed_2")
    assert r2.recovered is True
    assert [s.id for s in r2.plan.suites] == ["two"]


# --- builder methods ---------------------------------------------------------


def test_builder_suite_and_zone():
    plan = (
        barndominium("B")
        .envelope(width=30, length=24)
        .ceiling(9)
        .add_room("bed", "bedroom", x=0, y=0, width=16, length=24)
        .add_room("bath", "bathroom", x=16, y=0, width=14, length=24)
        .suite("primary", "bed", "bath")
        .zone("private", "primary")
    )
    assert plan.suites[0].id == "primary"
    assert plan.suites[0].members == ("bed", "bath")
    assert plan.zones[0].members == ("primary",)


def test_builder_rejects_an_empty_suite_or_zone():
    b = barndominium("B").envelope(width=20, length=20).ceiling(9)
    with pytest.raises(ValueError):
        b.suite("primary")
    with pytest.raises(ValueError):
        b.zone("private")


def test_builder_dedupes_members():
    plan = (
        barndominium("B").envelope(width=20, length=20).ceiling(9)
        .add_room("a", "bedroom", x=0, y=0, width=10, length=20)
        .suite("s", "a", "a")
    )
    assert plan.suites[0].members == ("a",)


# --- SUITE_REF / ZONE_REF ----------------------------------------------------


def test_suite_ref_flags_an_unknown_member():
    r = _compile("suite primary: master_bed nonexistent")
    refs = [d for d in r.errors if d.code == "SUITE_REF"]
    assert refs and refs[0].room == "nonexistent"


def test_zone_ref_flags_an_unknown_member():
    r = _compile("zone private: master_bed ghost")
    refs = [d for d in r.errors if d.code == "ZONE_REF"]
    assert refs and refs[0].room == "ghost"


def test_zone_member_may_be_a_suite_id():
    # A suite id is a legal zone member — no ZONE_REF for it.
    r = _compile("suite primary: master_bed\nzone private: primary bed_2")
    assert "ZONE_REF" not in _codes(r, "error")


def test_ref_free_when_all_members_exist():
    r = _compile(
        "suite primary: master_bed master_bath\nzone private: primary bed_2 bed_3"
    )
    assert "SUITE_REF" not in _codes(r, "error")
    assert "ZONE_REF" not in _codes(r, "error")


# --- SUITE_OVERLAP / ZONE_OVERLAP --------------------------------------------


def test_suite_overlap_flags_a_room_in_two_suites():
    r = _compile("suite a: master_bed master_bath\nsuite b: master_bath")
    over = [d for d in r.warnings if d.code == "SUITE_OVERLAP"]
    assert over and over[0].room == "master_bath"


def test_suite_overlap_silent_for_disjoint_suites():
    r = _compile("suite a: master_bed master_bath\nsuite b: bed_2 bed_3")
    assert "SUITE_OVERLAP" not in _codes(r, "warning")


def test_zone_overlap_flags_a_room_in_two_zones_directly():
    r = _compile("zone a: master_bed bed_2\nzone b: bed_2 bed_3")
    over = [d for d in r.warnings if d.code == "ZONE_OVERLAP"]
    assert over and over[0].room == "bed_2"


def test_zone_overlap_detects_double_membership_via_a_suite():
    # master_bed is in zone `a` directly and in zone `b` through `primary`.
    r = _compile(
        "suite primary: master_bed master_bath\n"
        "zone a: master_bed\n"
        "zone b: primary"
    )
    over = {d.room for d in r.warnings if d.code == "ZONE_OVERLAP"}
    assert "master_bed" in over


def test_zone_overlap_silent_for_disjoint_zones():
    r = _compile("zone a: master_bed master_bath\nzone b: bed_2 bed_3")
    assert "ZONE_OVERLAP" not in _codes(r, "warning")


# --- ZONE_CROSS --------------------------------------------------------------

# A kitchen dropped into a zone that otherwise holds bedrooms/baths.
_CROSS = """\
plan "Cross"
envelope 40 x 30
ceiling 9
room bed1:   bedroom  at 0,0   size 14 x 14
room bath:   bathroom at 14,0  size 8 x 14
room kit:    kitchen  at 22,0  size 18 x 14
room living: living   at 0,14  size 40 x 16
door bed1 - living width 3
door bath - living width 2.67
door kit - living width 4
entry living south width 3 offset 4
window bed1 west width 4 offset 4
window living south width 12 offset 4
window kit east width 4 offset 4
window bath north width 3 offset 1
{decl}
"""


def test_zone_cross_flags_a_public_room_in_the_private_band():
    r = compile_source(_CROSS.format(decl="zone private: bed1 bath kit"))
    cross = [d for d in r.infos if d.code == "ZONE_CROSS"]
    assert cross and cross[0].room == "kit"
    assert "private band" in cross[0].message


def test_zone_cross_flags_a_private_room_in_the_public_band():
    r = compile_source(_CROSS.format(decl="zone public: living kit bed1"))
    cross = {d.room for d in r.infos if d.code == "ZONE_CROSS"}
    assert "bed1" in cross


def test_zone_cross_silent_for_a_mixed_open_concept_zone():
    # A zone with BOTH public and private rooms is ambiguous — never flagged.
    r = compile_source(_CROSS.format(decl="zone core: living kit bed1 bath"))
    assert "ZONE_CROSS" not in _codes(r, "info")


def test_zone_cross_silent_without_any_zone():
    # No zones declared → the check can't fire (no false positives).
    r = compile_source(_CROSS.format(decl=""))
    assert "ZONE_CROSS" not in _codes(r, "info")


def test_zone_cross_silent_when_room_is_in_two_zones():
    # Ambiguous membership (also a ZONE_OVERLAP) is skipped by ZONE_CROSS.
    r = compile_source(
        _CROSS.format(decl="zone private: bed1 bath kit\nzone other: kit living")
    )
    assert "ZONE_CROSS" not in {d.code for d in r.infos if d.room == "kit"}


def test_zone_cross_ignores_neutral_rooms():
    # A hallway (neutral) in a private zone doesn't count as the opposite band,
    # so it neither fires nor blocks — only bed/bath/public rooms matter.
    r = compile_source(
        _CROSS.replace(
            "room living: living   at 0,14  size 40 x 16",
            "room living: living   at 0,14  size 32 x 16\n"
            "room hall:   hallway  at 32,14 size 8 x 16",
        ).format(decl="zone private: bed1 bath hall")
    )
    # hall is neutral; no public room in the zone → no ZONE_CROSS at all.
    assert "ZONE_CROSS" not in _codes(r, "info")


# --- sharpened checks: declared membership vs inference ----------------------


def test_master_ensuite_satisfied_by_a_declared_suite():
    # master_bath opens to BOTH master_bed and the hall, so strict inference
    # calls it shared and flags MASTER_ENSUITE — but the declared suite relaxes
    # that to "reachable through the suite", and the direct door satisfies it.
    two_baths = _BASE.replace(
        "room bed_3:       bedroom  at 40,18 size 12 x 14",
        "room bed_3:       bedroom  at 40,18 size 6 x 14\n"
        "room bath_2:      bathroom at 46,18 size 6 x 14",
    ).replace(
        "door hall_beds - bed_3 width 3 offset 1",
        "door hall_beds - bed_3 width 3 offset 1\n"
        "door hall_beds - master_bath width 2.67\n"
        "door hall_beds - bath_2 width 2.67",
    ).replace(
        "window bed_3 north width 4 offset 4",
        "window bed_3 north width 4 offset 4\nwindow bath_2 north width 3 offset 1",
    )
    # Without a suite, inference fires (two shared baths, no private ensuite).
    assert "MASTER_ENSUITE" in _codes(
        compile_source(two_baths.format(decl="")), "info"
    )
    # With the suite declared, the in-suite door path makes it exact and silent.
    assert "MASTER_ENSUITE" not in _codes(
        compile_source(two_baths.format(decl="suite primary: master_bed master_bath")),
        "info",
    )
    # But declaration alone is not geometry: a suite naming a bath the bedroom
    # can't reach through the suite (hall-only access) does NOT suppress it.
    no_direct = two_baths.replace("door master_bed - master_bath width 2.67\n", "")
    assert "MASTER_ENSUITE" in _codes(
        compile_source(no_direct.format(decl="suite primary: master_bed master_bath")),
        "info",
    )


def test_bed_sound_suppressed_for_two_bedrooms_in_one_suite():
    # bed_2 and bed_3 share a 14 ft wall → BED_SOUND by inference.
    assert "BED_SOUND" in _codes(_compile(""), "info")
    # Declaring them one suite (a shared kids' room) silences the buffer nudge.
    r = _compile("suite kids: bed_2 bed_3")
    assert "BED_SOUND" not in {
        d.code for d in r.infos if d.code == "BED_SOUND" and d.room in ("bed_2",)
    }


def test_bed_sound_still_fires_for_bedrooms_in_different_suites():
    r = _compile("suite a: bed_2\nsuite b: bed_3")
    assert "BED_SOUND" in _codes(r, "info")


_PRIVACY = """\
plan "Privacy"
envelope 34 x 28
ceiling 9
room living: living  at 0,0   size 34 x 14
room bed:    bedroom at 0,14  size 20 x 14
room sit:    living  at 20,14 size 14 x 14
door living - bed width 3
door bed - sit width 3
entry living south width 3 offset 4
window living south width 12 offset 4
window bed north width 5 offset 4
window sit north width 4 offset 4
{decl}
"""


def test_bed_privacy_fires_by_inference_without_a_suite():
    # bed opens onto both `living` and the `sit` living-area — a privacy leak.
    assert "BED_PRIVACY" in _codes(compile_source(_PRIVACY.format(decl="")), "info")


def test_bed_privacy_suppressed_for_a_public_room_in_the_bedrooms_suite():
    # `sit` is declared part of the bedroom's suite, so opening onto it is fine;
    # the `living` door (outside the suite) would still fire, so make the suite
    # cover the whole leak to prove the declared path suppresses it.
    r = compile_source(
        _PRIVACY.replace("door living - bed width 3\n", "").format(
            decl="suite master: bed sit"
        )
    )
    assert "BED_PRIVACY" not in _codes(r, "info")


_ENTRY = """\
plan "Entry"
envelope 30 x 28
ceiling 9
room living: living  at 0,0  size 30 x 14
room bed:    bedroom at 0,14 size 30 x 14
door living - bed width 3
entry living south width 3 offset 4
entry bed north width 3 offset 4
window living south width 12 offset 4
window bed west width 5 offset 4
{decl}
"""


def test_entry_private_bedroom_info_fires_without_a_suite():
    assert "ENTRY_PRIVATE" in _codes(compile_source(_ENTRY.format(decl="")), "info")


def test_entry_private_suppressed_for_a_suited_bedroom():
    # A patio door off a declared (primary) suite bedroom is expected.
    r = compile_source(_ENTRY.format(decl="suite primary: bed"))
    assert "ENTRY_PRIVATE" not in _codes(r, "info")


def test_entry_private_into_a_bath_is_always_a_warning():
    # Sharpening only touches the bedroom info; a bath entry stays a warning
    # even inside a suite.
    src = """\
plan "BathEntry"
envelope 30 x 28
ceiling 9
room living: living   at 0,0  size 30 x 14
room bed:    bedroom  at 0,14 size 22 x 14
room bath:   bathroom at 22,14 size 8 x 14
door living - bed width 3
door bed - bath width 2.67
entry living south width 3 offset 4
entry bath north width 3 offset 1
window living south width 12 offset 4
window bed west width 5 offset 4
suite primary: bed bath
"""
    assert "ENTRY_PRIVATE" in _codes(compile_source(src), "warning")


# --- emit round-trip ---------------------------------------------------------


def test_suite_and_zone_round_trip_through_emit():
    r = _compile(
        "suite primary: master_bed master_bath master_wic\n"
        "zone private: primary bed_2 bed_3 hall_beds"
    )
    text = emit_dsl(r.plan)
    assert "suite primary: master_bed master_bath master_wic" in text
    assert "zone private: primary bed_2 bed_3 hall_beds" in text
    # Recompile-equal: emitting the recompiled plan reproduces the same source.
    assert emit_dsl(compile_source(text).plan) == text


def test_emit_omits_suites_and_zones_when_absent():
    text = emit_dsl(_compile("").plan)
    assert "suite " not in text and "zone " not in text


def test_emit_preserves_declaration_order():
    r = _compile("suite b: bed_2\nsuite a: bed_3\nzone y: b\nzone x: a")
    lines = [l for l in emit_dsl(r.plan).splitlines() if l.startswith(("suite", "zone"))]
    assert lines == [
        "suite b: bed_2", "suite a: bed_3", "zone y: b", "zone x: a"
    ]


# --- introspect --------------------------------------------------------------


def test_plan_summary_includes_suites_and_zones_when_declared():
    r = _compile(
        "suite primary: master_bed master_bath\nzone private: primary bed_2"
    )
    summary = plan_summary(r.plan)
    assert summary["suites"] == [
        {"id": "primary", "members": ["master_bed", "master_bath"]}
    ]
    assert summary["zones"] == [{"id": "private", "members": ["primary", "bed_2"]}]


def test_plan_summary_omits_the_keys_when_none_declared():
    summary = plan_summary(_compile("").plan)
    assert "suites" not in summary and "zones" not in summary


def test_summary_text_renders_a_suites_zones_section():
    r = _compile(
        "suite primary: master_bed master_bath\nzone private: primary bed_2"
    )
    text = summary_text(plan_summary(r.plan))
    assert "Suites (id: members):" in text
    assert "  primary: master_bed master_bath" in text
    assert "Zones (id: members):" in text
    assert "  private: primary bed_2" in text


def test_summary_text_adds_nothing_when_no_groupings():
    text = summary_text(plan_summary(_compile("").plan))
    assert "Suites" not in text and "Zones" not in text


# --- registry ----------------------------------------------------------------


def test_new_codes_are_registered_and_explained():
    for code in (
        "SUITE_REF", "SUITE_OVERLAP", "ZONE_REF", "ZONE_OVERLAP", "ZONE_CROSS"
    ):
        assert code in REGISTRY
        assert "Unknown" not in explain(code)


# --- suppression guardrails (review findings) ---------------------------------


def test_entry_private_not_suppressed_for_a_shared_multi_bed_suite():
    """Only a sole-bedroom (primary) suite reads as 'expected patio door' — an
    exterior entry straight into a kids'-suite bedroom is exactly the concern."""
    shared = _ENTRY.replace(
        "room bed:    bedroom at 0,14 size 30 x 14",
        "room bed:    bedroom at 0,14 size 15 x 14\n"
        "room bed2:   bedroom at 15,14 size 15 x 14",
    ).replace(
        "door living - bed width 3",
        "door living - bed width 3\ndoor living - bed2 width 3",
    ).replace(
        "window bed west width 5 offset 4",
        "window bed west width 5 offset 4\nwindow bed2 east width 5 offset 4",
    )
    r = compile_source(shared.format(decl="suite kids: bed bed2"))
    assert "ENTRY_PRIVATE" in _codes(r, "info")


def test_bed_sound_not_silenced_by_one_giant_suite():
    """A suite whose bedrooms are exactly the adjoining pair is intentional; a
    plan-wide all-bedroom 'suite' must not mute the check."""
    r = _compile("suite everything: master_bed bed_2 bed_3")
    assert "BED_SOUND" in _codes(r, "info")


def test_suite_shadowing_a_room_id_warns():
    r = _compile("suite bed_2: master_bed")
    assert "SUITE_SHADOW" in _codes(r, "warning")
    # Distinct ids: no shadow warning.
    r2 = _compile("suite primary: master_bed")
    assert "SUITE_SHADOW" not in _codes(r2, "warning")
