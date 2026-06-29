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
