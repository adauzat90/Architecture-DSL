"""Tests for the pyRevit extension's pure exchange parser (`barndsl_revit.exchange`).

The parser is the consumer-side seam: it validates a `barndsl.revit/1` document
before the Revit builder touches it. These tests run it against documents the
barndsl core actually produces, so the producer and consumer can't drift apart,
plus the error paths for malformed input.
"""

from __future__ import annotations

import os
import sys

import pytest

from barndsl import compile_source, to_revit_model

# The extension's lib/ isn't a package on the default path; add it like pyRevit does.
_LIB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "revit",
    "barndsl.extension",
    "lib",
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import exchange  # noqa: E402


_CEDAR = """\
plan "Cedar Ridge"
envelope 60 x 40
ceiling 12
room living: living at 0,0 size 28 x 26
room kitchen: kitchen at 28,0 size 18 x 14
room bed: bedroom at 28,14 size 18 x 12
room shop: shop at 46,0 size 14 x 40
door living - bed width 2.67
entry living south width 3 offset 10
window living west width 6 offset 4
"""


def _model_dict():
    plan = compile_source(_CEDAR).plan
    return to_revit_model(plan).to_dict()


def test_schema_constants_have_not_drifted():
    # The schema string is declared once in the core (revit.EXCHANGE_SCHEMA) and
    # again in the extension (exchange.SCHEMA), across an interpreter boundary the
    # extension can't bridge by importing the core. This guards them explicitly so
    # a bump in one place fails loudly here, not as a mystery "unsupported schema".
    from barndsl.revit import EXCHANGE_SCHEMA

    assert EXCHANGE_SCHEMA == exchange.SCHEMA


def test_core_output_carries_every_required_section():
    # The extension hard-requires these top-level sections (exchange._REQUIRED_KEYS).
    # If the producer ever stops emitting one, building would fail in Revit; assert
    # the core's document supplies them all so the contract is checked in CI.
    doc = _model_dict()
    for key in exchange._REQUIRED_KEYS:
        assert key in doc, "core exchange is missing required section %r" % key


def test_load_accepts_core_output():
    data = exchange.load(_model_dict())
    assert data["schema"] == exchange.SCHEMA
    # load() normalises the optional sections so the builder can index freely.
    assert "columns" in data["structure"] and "framing" in data["structure"]
    assert isinstance(data["areas"], list)


def test_loads_from_json_string():
    plan = compile_source(_CEDAR).plan
    text = to_revit_model(plan).to_json()
    data = exchange.loads(text)
    assert data["plan"]["name"] == "Cedar Ridge"


def test_validate_clean_on_core_output():
    # Everything the core emits hosts onto a real wall on a real level.
    assert exchange.validate(_model_dict()) == []


def test_indexes():
    data = exchange.load(_model_dict())
    levels = exchange.levels_by_index(data)
    walls = exchange.walls_by_id(data)
    assert 0 in levels
    for o in data["openings"]:
        assert o["host_wall"] in walls


def test_rejects_wrong_schema():
    with pytest.raises(exchange.ExchangeError):
        exchange.load({"schema": "something/else", "units": "feet"})


def test_rejects_wrong_units():
    data = _model_dict()
    data["units"] = "millimeters"
    with pytest.raises(exchange.ExchangeError):
        exchange.load(data)


def test_rejects_missing_section():
    data = _model_dict()
    del data["walls"]
    with pytest.raises(exchange.ExchangeError):
        exchange.load(data)


def test_rejects_duplicate_wall_ids():
    data = _model_dict()
    data["walls"].append(dict(data["walls"][0]))  # same id twice
    with pytest.raises(exchange.ExchangeError):
        exchange.load(data)


def test_validate_flags_dangling_host():
    data = exchange.load(_model_dict())
    data["openings"][0]["host_wall"] = "w_does_not_exist"
    problems = exchange.validate(data)
    assert any("missing wall" in p for p in problems)


def test_validate_flags_unhosted_opening():
    data = exchange.load(_model_dict())
    data["openings"][0]["host_wall"] = None
    problems = exchange.validate(data)
    assert any("no host wall" in p for p in problems)


def test_optional_swing_fields_are_accepted():
    # swing_into/hinge are additive barndsl.revit/1 fields: present on the core's
    # output, harmless when absent, and clean when they name a served room.
    plan = compile_source(
        _CEDAR + "door living - kitchen width 2.67 into kitchen hinge far\n"
    ).plan
    data = exchange.load(to_revit_model(plan).to_dict())
    swung = [o for o in data["openings"] if o.get("swing_into")]
    assert swung and swung[0]["swing_into"] == "kitchen"
    assert exchange.validate(data) == []


def test_validate_flags_swing_into_an_unserved_room():
    data = exchange.load(_model_dict())
    door = next(o for o in data["openings"] if o["category"] == "door")
    door["swing_into"] = "not_a_room_it_serves"
    problems = exchange.validate(data)
    assert any("swings into" in p for p in problems)


# --- sectioned (L/T/U + monitor) roofs ---------------------------------------


_LSHAPE = """\
plan "L Barn"
envelope 40 x 24
ceiling 9
wing 16 x 20 at 40,0
room living: living at 0,0 size 40 x 24
room shop: shop at 40,0 size 16 x 20
"""

_MONITOR = """\
plan "Monitor Barn"
envelope 40 x 60
ceiling 9
roof monitor pitch 0.5
room living: living at 0,0 size 40 x 60
"""


def _exchange_of(src):
    return to_revit_model(compile_source(src).plan).to_dict()


def test_single_rectangle_roof_carries_no_sections():
    # A plain rectangular plan keeps the pre-sections roof shape byte-for-byte,
    # so its roof identity/fingerprint is unchanged (never a needless recreate).
    roof = _model_dict()["roof"]
    assert "sections" not in roof
    idents = [i for i in exchange.identities(_model_dict()) if i[0] == "roof"]
    assert idents == [("roof", "roof", exchange.ROOF_IDENTITY, idents[0][3])]


def test_lshape_roof_has_one_section_per_footprint_and_unique_identities():
    data = exchange.load(_exchange_of(_LSHAPE))
    assert len(data["roof"]["sections"]) == 2
    assert exchange.validate(data) == []
    roof_idents = [i for i in exchange.identities(data) if i[0] == "roof"]
    assert len(roof_idents) == 2
    keys = [k for _kind, _src, k, _fp in roof_idents]
    assert keys == ["%s|0" % exchange.ROOF_IDENTITY, "%s|1" % exchange.ROOF_IDENTITY]
    assert len(set(keys)) == 2  # distinct so the diff tracks each plane


def test_monitor_roof_emits_three_plane_identities():
    data = exchange.load(_exchange_of(_MONITOR))
    sections = data["roof"]["sections"]
    assert [s["role"] for s in sections] == [
        "monitor_side", "monitor_center", "monitor_side"
    ]
    roof_idents = [i for i in exchange.identities(data) if i[0] == "roof"]
    assert len(roof_idents) == 3
    # Deterministic across re-exports (or an unchanged monitor would recreate).
    assert exchange.identities(data) == exchange.identities(_exchange_of(_MONITOR))


def test_monitor_side_sheds_both_rise_toward_the_centre():
    """Review fix: each side shed's LOW (slope-defining) eave is its OUTER
    edge, so both planes rise to the raised clerestory — previously the
    second shed inverted (a base_height discontinuity at the seam)."""
    data = exchange.load(_exchange_of(_MONITOR))
    lo_side, centre, hi_side = data["roof"]["sections"]
    # 40 x 60 envelope: long axis y, strips split along x; outline order is
    # [south, east, north, west] — the strip eaves are west(3)/east(1).
    assert lo_side["outline_slopes"] == [False, False, False, True]   # west = outer
    assert hi_side["outline_slopes"] == [False, True, False, False]   # east = outer
    # The centre gable keeps both eaves slope-defining and sits on the sheds.
    assert centre["outline_slopes"].count(True) == 2
    assert lo_side["base_height"] == 0.0 and hi_side["base_height"] == 0.0
    assert centre["base_height"] > 0.0


def test_monitor_on_an_lshape_roofs_the_envelope_not_the_bounds():
    """Review fix: the monitor form belongs to the primary envelope block;
    the wing gets its own gable field and the concave notch stays uncovered."""
    src = _LSHAPE.replace("ceiling 9", "ceiling 9\nroof monitor pitch 0.5")
    data = exchange.load(_exchange_of(src))
    sections = data["roof"]["sections"]
    roles = [s["role"] for s in sections]
    assert roles == ["monitor_side", "monitor_center", "monitor_side", "field"]
    # Monitor strips confined to the 40 x 24 envelope...
    for s in sections[:3]:
        xs = [p for seg in s["outline"] for p in (seg[0][0], seg[1][0])]
        ys = [p for seg in s["outline"] for p in (seg[0][1], seg[1][1])]
        assert max(xs) <= 40 + 1e-9 and max(ys) <= 24 + 1e-9
    # ...and the wing field covers exactly the wing (16 x 20 at 40,0).
    wing = sections[3]
    xs = [p for seg in wing["outline"] for p in (seg[0][0], seg[1][0])]
    ys = [p for seg in wing["outline"] for p in (seg[0][1], seg[1][1])]
    assert min(xs) == 40 and max(xs) == 56 and min(ys) == 0 and max(ys) == 20
    # The notch (x < 40, y > 24 is outside; but e.g. x=45, y=22 IS wing) — the
    # point above the envelope's north edge on the wing side of nothing:
    # (10, 26) lies outside every section.
    def covered(px, py):
        for s in sections:
            xs = [p for seg in s["outline"] for p in (seg[0][0], seg[1][0])]
            ys = [p for seg in s["outline"] for p in (seg[0][1], seg[1][1])]
            if min(xs) <= px <= max(xs) and min(ys) <= py <= max(ys):
                return True
        return False
    assert not covered(10, 26)
    assert covered(45, 10)
