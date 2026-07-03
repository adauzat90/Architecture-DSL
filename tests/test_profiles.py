"""Tests for jurisdiction profiles (src/barndsl/profiles.py) and their threading
through validate / compile_source / the CLI.

Covers: DEFAULT byte-identity (no profile == explicit default), each profiled
threshold actually flipping its check's outcome, JSON overrides + unknown-key
rejection, load_profile resolution, and the CLI --profile flag / profiles list.
"""

from __future__ import annotations

import json
import os

import pytest

from barndsl import (
    DEFAULT_PROFILE,
    Profile,
    compile_source,
    get_profile,
    load_profile,
    profiles_text,
)
from barndsl.compiler import compile_file
from barndsl.profiles import (
    BUILTIN_PROFILES,
    DEFAULT,
    profile_from_dict,
)
from barndsl import constants


def _codes(src, profile=None):
    return {i.code for i in compile_source(src, profile=profile).diagnostics}


def _msgs(src, profile=None):
    return [str(i) for i in compile_source(src, profile=profile).diagnostics]


# A handful of representative plans for the byte-identity checks.
_PLAN_OK = """\
plan "OK"
envelope 40 x 30
ceiling 9
room lr: living at 0,0 size 20 x 20
room bed: bedroom at 20,0 size 12 x 12
room bath: bathroom at 20,12 size 8 x 8
door lr - bed width 2.67
door lr - bath width 2.67
entry lr south width 3 offset 2
window bed east width 4 offset 2
window lr west width 8 offset 2
"""

_PLAN_TIGHT = """\
plan "Tight"
envelope 40 x 30
ceiling 7
room bed: bedroom at 0,0 size 10 x 7
room hall: hallway at 10,0 size 12 x 3
room bath: bathroom at 22,0 size 8 x 8
entry bed south width 3 offset 2
window bed east width 4 offset 2
"""

_PLAN_STAIR = """\
plan "Stair"
envelope 40 x 30
ceiling 10
room a: living at 0,0 size 20 x 20
room loft: living at 0,0 size 20 x 14 level 1
stair st at 0,0 size 3.5 x 13.5 from 0 to 1
entry a south width 3 offset 2
"""


# --------------------------------------------------------------------------
# 1. DEFAULT is byte-identical to no profile (existing behaviour unchanged).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("src", [_PLAN_OK, _PLAN_TIGHT, _PLAN_STAIR])
def test_default_profile_is_byte_identical(src):
    none = _msgs(src, None)
    explicit = _msgs(src, DEFAULT_PROFILE)
    assert none == explicit


def test_bundled_example_default_identical():
    example = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "examples", "cedar_ridge.barn"
    )
    if not os.path.exists(example):
        pytest.skip("bundled example missing")
    a = compile_file(example)
    b = compile_file(example, profile=DEFAULT_PROFILE)
    assert [str(i) for i in a.diagnostics] == [str(i) for i in b.diagnostics]


def test_default_matches_constants():
    """DEFAULT must mirror the constants so the byte-identity guarantee holds."""
    assert DEFAULT.min_ceiling_height == constants.MIN_CEILING
    assert DEFAULT.min_bedroom_area == constants.MIN_BEDROOM_AREA
    assert DEFAULT.min_bedroom_dimension == constants.MIN_BEDROOM_DIMENSION
    assert DEFAULT.min_hallway_width == constants.MIN_HALLWAY_WIDTH
    assert DEFAULT.comfort_hallway_width == constants.COMFORT_HALLWAY_WIDTH
    assert DEFAULT.min_egress_area == constants.MIN_EGRESS_AREA
    assert DEFAULT.min_egress_area_grade == constants.MIN_EGRESS_AREA_GRADE
    assert DEFAULT.max_egress_sill == constants.MAX_EGRESS_SILL
    assert DEFAULT.max_riser_height == constants.MAX_RISER_HEIGHT
    assert DEFAULT.min_tread_depth == constants.MIN_TREAD_DEPTH
    assert DEFAULT.min_stair_width == constants.MIN_STAIR_WIDTH
    assert DEFAULT.natural_light_ratio == constants.NATURAL_LIGHT_RATIO
    assert DEFAULT.is_default


# --------------------------------------------------------------------------
# 2. Each profiled parameter actually changes its check's outcome.
# --------------------------------------------------------------------------


def test_strict_flips_habitability_codes():
    strict = get_profile("strict")
    # ceiling 7 (ok default), below strict 7.5; bedroom 70 sq ft / 7 ft ok default,
    # below strict 80 / 8; hall 3 ft ok default, below strict 3.5.
    d = _codes(_PLAN_TIGHT)
    s = _codes(_PLAN_TIGHT, strict)
    for code in ("CEILING", "BEDROOM_AREA", "BEDROOM_DIM", "HALL_WIDTH"):
        assert code not in d, code
        assert code in s, code


def test_stair_tread_riser_parameterised():
    # A 11.5 ft run climbs 10 ft: fits the default 7.75 in / 10 in stair, but not
    # the strict 7 in / 11 in one (STAIR_RUN); the rural 8.25 in / 9 in is looser.
    assert "STAIR_RUN" not in _codes(_PLAN_STAIR)
    assert "STAIR_RUN" in _codes(_PLAN_STAIR, get_profile("strict"))


_EGRESS_AREA = """\
plan "Eg"
envelope 40 x 30
ceiling 9
room bed: bedroom at 0,0 size 12 x 12
room hall: hallway at 12,0 size 6 x 4
room bath: bathroom at 18,0 size 8 x 8
door bed - hall width 2.67
entry bath south width 3 offset 2
window bed west casement width 2 offset 2 sill 2 head 4.7
"""


def test_egress_area_grade_parameterised():
    # Clears ~5.4 sq ft: >= the default 5.0 grade minimum, below strict's 5.7.
    assert "EGRESS_SIZE" not in _codes(_EGRESS_AREA)
    assert "EGRESS_SIZE" in _codes(_EGRESS_AREA, get_profile("strict"))


_EGRESS_SILL = """\
plan "Sill"
envelope 40 x 30
ceiling 9
room bed: bedroom at 0,0 size 12 x 12
room hall: hallway at 12,0 size 6 x 4
room bath: bathroom at 18,0 size 8 x 8
door bed - hall width 2.67
entry bath south width 3 offset 2
window bed west casement width 4 offset 2 sill 3.58 head 7.5
"""


def test_egress_sill_parameterised():
    # Sill 43 in: under the default 44 in cap, over strict's 42 in, under rural's 48 in.
    assert "EGRESS_SIZE" not in _codes(_EGRESS_SILL)
    assert "EGRESS_SIZE" in _codes(_EGRESS_SILL, get_profile("strict"))
    assert "EGRESS_SIZE" not in _codes(_EGRESS_SILL, get_profile("rural"))


_DAYLIGHT = """\
plan "DL"
envelope 40 x 30
ceiling 9
room lr: living at 0,0 size 20 x 20
room bath: bathroom at 20,0 size 8 x 8
entry lr south width 3 offset 2
window lr west casement width 9 offset 2 sill 3 head 6.67
"""


def test_natural_light_ratio_parameterised():
    # 400 sq ft room, ~33 sq ft glazing: >= 8% (32) default, below strict's 10% (40).
    assert "NAT_LIGHT" not in _codes(_DAYLIGHT)
    assert "NAT_LIGHT" in _codes(_DAYLIGHT, get_profile("strict"))


_HALL_TIGHT = """\
plan "HT"
envelope 40 x 30
ceiling 9
room lr: living at 0,0 size 20 x 20
room hall: hallway at 20,0 size 3.5 x 10
room bath: bathroom at 23.5,0 size 8 x 8
door lr - hall width 2.67
door hall - bath width 2.67
entry lr south width 3 offset 2
"""


def test_comfort_hallway_width_parameterised():
    # A 3.5 ft hall triggers the comfort nudge by default (target 4 ft); the rural
    # profile sets comfort == the 3 ft minimum, disabling the nudge.
    assert "HALL_TIGHT" in _codes(_HALL_TIGHT)
    assert "HALL_TIGHT" not in _codes(_HALL_TIGHT, get_profile("rural"))


# --------------------------------------------------------------------------
# 3. Message honesty: enforced number shown, profile named, IRC base cited.
# --------------------------------------------------------------------------


def test_message_names_profile_and_base():
    strict = get_profile("strict")
    (ceiling,) = [
        i
        for i in compile_source(_PLAN_TIGHT, profile=strict).diagnostics
        if i.code == "CEILING"
    ]
    assert "7.5 ft minimum" in ceiling.message
    assert "strict" in ceiling.message
    assert "IRC base of 7 ft" in ceiling.message


# --------------------------------------------------------------------------
# 4. JSON overrides / load_profile / errors.
# --------------------------------------------------------------------------


def test_profile_from_dict_subset_override():
    p = profile_from_dict({"name": "co", "min_ceiling_height": 8.0})
    assert p.min_ceiling_height == 8.0
    assert p.min_bedroom_area == DEFAULT.min_bedroom_area  # untouched
    assert p.name == "co"
    assert not p.is_default


def test_profile_from_dict_extends():
    p = profile_from_dict({"extends": "strict", "min_ceiling_height": 8.0})
    assert p.min_ceiling_height == 8.0
    assert p.min_tread_depth == get_profile("strict").min_tread_depth


def test_profile_from_dict_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown profile key"):
        profile_from_dict({"min_ceiling": 8.0})


def test_profile_from_dict_bad_value_rejected():
    with pytest.raises(ValueError):
        profile_from_dict({"min_ceiling_height": -1})
    with pytest.raises(ValueError):
        profile_from_dict({"min_ceiling_height": "tall"})


def test_load_profile_builtin_and_alias():
    assert load_profile("strict") is get_profile("strict")
    assert load_profile("IRC-2021") is DEFAULT
    assert load_profile("baseline") is DEFAULT


def test_load_profile_json_file(tmp_path):
    f = tmp_path / "county.json"
    f.write_text(json.dumps({"name": "county", "min_hallway_width": 3.5}))
    p = load_profile(str(f))
    assert p.name == "county"
    assert p.min_hallway_width == 3.5


def test_load_profile_missing_name_is_error():
    with pytest.raises(ValueError, match="unknown profile"):
        load_profile("does-not-exist")


def test_load_profile_bad_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    with pytest.raises(ValueError, match="could not read profile"):
        load_profile(str(f))


def test_with_overrides_unknown_field():
    with pytest.raises(ValueError, match="unknown profile threshold"):
        DEFAULT.with_overrides(nope=1.0)


def test_to_dict_roundtrips_fields():
    d = get_profile("strict").to_dict()
    assert d["name"] == "strict"
    assert d["min_ceiling_height"] == 7.5


def test_builtin_registry_names():
    assert set(BUILTIN_PROFILES) == {"default", "strict", "rural"}
    assert all(isinstance(p, Profile) for p in BUILTIN_PROFILES.values())


# --------------------------------------------------------------------------
# 5. CLI: --profile end-to-end, bad name exit 2, profiles listing.
# --------------------------------------------------------------------------


def _write(tmp_path, src="ceiling 7"):
    f = tmp_path / "p.barn"
    f.write_text(_PLAN_TIGHT)
    return str(f)


def test_cli_compile_profile_flag(tmp_path, capsys):
    from barndsl.cli import main

    path = _write(tmp_path)
    rc = main(["compile", path, "--profile", "strict"])
    out = capsys.readouterr().out
    assert rc == 1  # strict introduces CEILING/BEDROOM errors
    assert "CEILING" in out


def test_cli_compile_default_unchanged(tmp_path, capsys):
    from barndsl.cli import main

    path = _write(tmp_path)
    main(["compile", path])
    plain = capsys.readouterr().out
    main(["compile", path, "--profile", "default"])
    explicit = capsys.readouterr().out
    assert plain == explicit


def test_cli_bad_profile_name_exit_2(tmp_path, capsys):
    from barndsl.cli import main

    path = _write(tmp_path)
    rc = main(["compile", path, "--profile", "nonesuch"])
    assert rc == 2
    assert "unknown profile" in capsys.readouterr().err


def test_cli_score_profile_flag(tmp_path):
    from barndsl.cli import main

    path = _write(tmp_path)
    assert main(["score", path, "--profile", "rural"]) == 0


def test_cli_profiles_listing(capsys):
    from barndsl.cli import main

    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "strict" in out and "rural" in out
    assert "ILLUSTRATIVE" in out  # the disclaimer


def test_cli_profiles_dump_json(capsys):
    from barndsl.cli import main

    assert main(["profiles", "--profile", "strict"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "strict" and data["min_tread_depth"] == 11 / 12


def test_profiles_text_has_disclaimer():
    assert "ILLUSTRATIVE" in profiles_text()
    assert "not legal advice" in profiles_text().lower()


# --- review hardening: golden default-path messages + tag consistency ---------


def test_default_path_messages_are_pinned_verbatim():
    """The None-vs-DEFAULT comparison above cannot catch a rewording (both
    sides run the same code) — pin the exact default text of messages the
    profiling touched, so any drift from the pre-profile wording fails here."""
    tight = compile_source(_PLAN_TIGHT)
    msgs = {}
    for d in tight.diagnostics:
        msgs.setdefault(d.code, d.message)  # first per code (bed before hall)
    assert msgs["ROOM_CLEAR"] == (
        "Bedroom 'bed' measures 70 sq ft nominal but only ~62.4184 sq ft "
        "clear (finish-face); the 70 sq ft minimum is measured between "
        "finished surfaces, so the built room falls short."
    )
    assert msgs["HALL_TIGHT"] == (
        "Hallway 'hall' is 3 ft wide \u2014 legal (>= 3 ft) but tight; 4 ft is "
        "comfortable for two people and moving furniture."
    )
    assert msgs["NAT_LIGHT"] == (
        "Glazing 0 sq ft is below the natural-light minimum of 5.6 sq ft "
        "(8% of floor area)."
    )
    assert not any("(the '" in m for m in msgs.values())  # no profile tags


def test_amended_stair_and_daylight_messages_name_the_profile():
    """Review finding: STAIR_RUN / NAT_LIGHT / HALL_TIGHT showed amended
    numbers without attribution — under strict they must name the profile."""
    strict = get_profile("strict")

    stair = compile_source(_PLAN_STAIR, profile=strict)
    sr = next(d for d in stair.diagnostics if d.code == "STAIR_RUN")
    assert "'strict' profile amends" in sr.message

    day = compile_source(_DAYLIGHT, profile=strict)
    nl = next(d for d in day.diagnostics if d.code == "NAT_LIGHT")
    assert "'strict' profile amends" in nl.message

    hall = compile_source(_HALL_TIGHT, profile=strict)
    ht = next(d for d in hall.diagnostics if d.code == "HALL_TIGHT")
    assert "'strict' profile amends" in ht.message
