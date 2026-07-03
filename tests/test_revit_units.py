"""Tests for the exchange's ``units`` field and metric normalisation.

The ``barndsl.revit/1`` exchange is canonically feet and is ALWAYS *emitted* in
feet (so fingerprints never move), but it *accepts* metric on load: a document
declaring ``meters``/``metres``/``m`` is normalised to feet — on both consumers,
the core's :func:`exchange_to_plan` and the pyRevit extension's ``exchange.load``
— by one schema-driven walker duplicated across the interpreter boundary. These
tests cover the conversion, the field classification (areas by factor², angles/
ratios untouched), the loud failure on an unclassified numeric field, unit
rejection on both sides, and that the two copies of the table stay in sync.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

from barndsl import compile_source
from barndsl.revit import (
    EXCHANGE_SCHEMA,
    FOOT_PER_METER,
    RevitImportError,
    exchange_to_plan,
    normalize_exchange_units,
    to_revit_model,
    _convert_document,
    _UNIT_FIELDS,
)

# The extension's lib/ isn't a package on the default path; add it like pyRevit.
_LIB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "revit", "barndsl.extension", "lib"
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import exchange as ext_exchange  # noqa: E402

F = FOOT_PER_METER
GALLERY = ["cottage.barn", "hall_spine.barn", "lshape.barn", "two_story.barn"]
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples", "gallery")


def _gallery_doc(rel):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    return to_revit_model(plan).to_dict()


def _metric_doc():
    """A small hand-built metric exchange (meters), geometrically loadable."""
    return {
        "schema": EXCHANGE_SCHEMA,
        "units": "meters",
        "plan": {
            "name": "Metric House",
            "ceiling_height": 3.0,
            "floor_depth": 0.3,
            "floor_to_floor": 3.3,
            "envelope_width": 10.0,
            "envelope_length": 8.0,
            "wings": [],
            "orientation": 45.0,
            "siding": None,
            "roofing": None,
            "roof_pitch": 0.5,
        },
        "levels": [{"index": 0, "name": "Level 1", "elevation": 0.0, "height": 3.0}],
        "walls": [],
        "openings": [
            {"id": "o0", "category": "window", "kind": "casement", "level": 0,
             "location": [0.0, 2.5], "width": 1.2, "height": 1.5, "sill": 1.0,
             "exterior": True, "egress": False, "rooms": ["living"], "host_wall": None,
             "swing_into": None, "hinge": None},
            {"id": "o1", "category": "door", "kind": "swing", "level": 0,
             "location": [6.0, 2.5], "width": 1.0, "height": 2.0, "sill": 0.0,
             "exterior": False, "egress": False, "rooms": ["living", "kitchen"],
             "host_wall": None, "swing_into": None, "hinge": None},
        ],
        "rooms": [
            {"id": "living", "name": "Living", "type": "living", "level": 0,
             "point": [3.0, 2.5], "area": 30.0, "x": 0.0, "y": 0.0, "width": 6.0,
             "length": 5.0, "clear_width": 6.0, "clear_length": 5.0,
             "clear_area": 30.0, "ceiling_height": 3.0, "vaulted": False},
            {"id": "kitchen", "name": "Kitchen", "type": "kitchen", "level": 0,
             "point": [8.0, 2.5], "area": 20.0, "x": 6.0, "y": 0.0, "width": 4.0,
             "length": 5.0, "clear_width": 4.0, "clear_length": 5.0,
             "clear_area": 20.0, "ceiling_height": 3.0, "vaulted": False},
        ],
        "site": {"width": 20.0, "length": 15.0,
                 "setbacks": {"front": 5.0, "side": 2.0, "rear": 3.0}},
    }


# --- emission is always feet -------------------------------------------------


@pytest.mark.parametrize("rel", GALLERY)
def test_emission_is_always_feet(rel):
    assert _gallery_doc(rel)["units"] == "feet"


def test_metric_roundtrip_still_emits_feet():
    # A plan reconstructed from a metric document re-emits feet, byte-identically
    # to the same plan emitted directly (nothing metric survives into emission).
    plan = compile_source(
        "plan \"X\"\nenvelope 30 x 20\nceiling 9\nroom a: living at 0,0 size 30 x 20\n"
    ).plan
    feet_doc = to_revit_model(plan).to_dict()
    metric = copy.deepcopy(feet_doc)
    _convert_document(metric, 1.0 / F)
    metric["units"] = "meters"
    refeet = to_revit_model(exchange_to_plan(metric)).to_dict()
    assert refeet["units"] == "feet"
    assert refeet == to_revit_model(exchange_to_plan(feet_doc)).to_dict()


# --- feet passes through untouched (byte-identical path) ---------------------


def test_feet_document_passes_through_untouched():
    plan = compile_source(
        "plan \"X\"\nenvelope 30 x 20\nceiling 9\nroom a: living at 0,0 size 30 x 20\n"
    ).plan
    feet_doc = to_revit_model(plan).to_dict()
    # Same object back, no copy, no mutation — nothing may perturb a fingerprint.
    assert normalize_exchange_units(feet_doc) is feet_doc
    assert ext_exchange.normalize_units(feet_doc) is feet_doc


def test_absent_units_defaults_to_feet():
    doc = _gallery_doc("cottage.barn")
    del doc["units"]
    assert normalize_exchange_units(doc) is doc  # missing units == feet (legacy)


# --- hand-built metric conversion --------------------------------------------


def test_metric_normalises_lengths_and_areas():
    norm = normalize_exchange_units(_metric_doc())
    assert norm["units"] == "feet"
    plan = norm["plan"]
    assert plan["envelope_width"] == pytest.approx(10.0 * F)
    assert plan["envelope_length"] == pytest.approx(8.0 * F)
    assert plan["ceiling_height"] == pytest.approx(3.0 * F)
    living = norm["rooms"][0]
    assert living["width"] == pytest.approx(6.0 * F)
    assert living["length"] == pytest.approx(5.0 * F)
    # AREA converts by the SQUARE of the factor.
    assert living["area"] == pytest.approx(30.0 * F * F)
    assert living["clear_area"] == pytest.approx(30.0 * F * F)
    # width * length stays consistent with the converted area.
    assert living["area"] == pytest.approx(living["width"] * living["length"])
    # setbacks (nested) convert as lengths.
    assert norm["site"]["setbacks"]["front"] == pytest.approx(5.0 * F)
    assert norm["site"]["width"] == pytest.approx(20.0 * F)


def test_metric_leaves_angles_and_ratios_untouched():
    norm = normalize_exchange_units(_metric_doc())
    assert norm["plan"]["orientation"] == 45.0  # a bearing in degrees, not a length
    assert norm["plan"]["roof_pitch"] == 0.5     # a rise:run ratio, not a length


def test_normalize_does_not_mutate_input():
    doc = _metric_doc()
    before = copy.deepcopy(doc)
    normalize_exchange_units(doc)
    assert doc == before  # metric normalisation works on a deep copy


def test_metric_reconstructs_correct_plan():
    plan = exchange_to_plan(_metric_doc())
    assert plan.envelope_width == pytest.approx(10.0 * F)
    assert plan.envelope_length == pytest.approx(8.0 * F)
    assert plan.ceiling_height == pytest.approx(3.0 * F)
    assert plan.orientation == 45.0
    assert plan.roof_pitch == 0.5
    living = plan.room("living")
    assert living.width == pytest.approx(6.0 * F)
    assert living.length == pytest.approx(5.0 * F)
    win = plan.windows[0]
    assert win.width == pytest.approx(1.2 * F)
    assert win.sill_height == pytest.approx(1.0 * F)
    # Site + setbacks survive the trip in feet.
    assert plan.site_spec.width == pytest.approx(20.0 * F)
    assert plan.site_spec.front == pytest.approx(5.0 * F)


# --- completeness: a real plan scaled to metric round-trips exactly ----------
#
# Building the metric document with the SAME walker (reciprocal factor) makes the
# feet→metric→feet trip an exact fixed point, and — critically — running the
# walker over a full producer document proves the table classifies EVERY field
# the producer emits (an unclassified numeric field would raise here).


def _rooms_key(plan):
    return sorted(
        (r.id, round(r.x, 6), round(r.y, 6), round(r.width, 6), round(r.length, 6), r.level)
        for r in plan.rooms
    )


def _int_doors_key(plan):
    return sorted(
        (frozenset((d.room_a, d.room_b)), d.kind, round(d.width, 6)) for d in plan.interior_doors
    )


def _windows_key(plan):
    return sorted(
        (w.room, w.wall.value, round(w.width, 6), round(w.sill_height, 6)) for w in plan.windows
    )


def _ext_doors_key(plan):
    return sorted((d.room, d.wall.value, round(d.width, 6)) for d in plan.exterior_doors)


@pytest.mark.parametrize("rel", GALLERY)
def test_full_plan_metric_roundtrip_matches_feet(rel):
    feet_doc = _gallery_doc(rel)
    metric = copy.deepcopy(feet_doc)
    _convert_document(metric, 1.0 / F)  # feet -> meters (raises if a field is unclassified)
    metric["units"] = "meters"

    from_feet = exchange_to_plan(feet_doc)
    from_metric = exchange_to_plan(metric)

    assert _rooms_key(from_metric) == _rooms_key(from_feet)
    assert _int_doors_key(from_metric) == _int_doors_key(from_feet)
    assert _windows_key(from_metric) == _windows_key(from_feet)
    assert _ext_doors_key(from_metric) == _ext_doors_key(from_feet)
    assert from_metric.envelope_width == pytest.approx(from_feet.envelope_width, abs=1e-6)
    assert from_metric.envelope_length == pytest.approx(from_feet.envelope_length, abs=1e-6)
    assert from_metric.ceiling_height == pytest.approx(from_feet.ceiling_height, abs=1e-6)


# --- unit rejection on both sides --------------------------------------------


def test_src_rejects_unknown_units():
    doc = _metric_doc()
    doc["units"] = "cubits"
    with pytest.raises(RevitImportError) as ei:
        exchange_to_plan(doc)
    assert "cubits" in str(ei.value)


def test_extension_rejects_unknown_units():
    doc = _gallery_doc("cottage.barn")
    doc["units"] = "cubits"
    with pytest.raises(ext_exchange.ExchangeError) as ei:
        ext_exchange.load(doc)
    assert "cubits" in str(ei.value)


@pytest.mark.parametrize("spelling", ["meters", "metres", "m"])
def test_accepted_metric_spellings(spelling):
    doc = _metric_doc()
    doc["units"] = spelling
    plan = exchange_to_plan(doc)
    assert plan.envelope_width == pytest.approx(10.0 * F)


# --- loud failure on an unclassified numeric field ---------------------------


def test_src_unknown_numeric_field_raises():
    doc = _metric_doc()
    doc["walls"] = [{"id": "w0", "level": 0, "start": [0, 0], "end": [0, 5],
                     "height": 3.0, "exterior": True, "thickness": 0.15,
                     "profile": "flat", "apex": None, "apex_height": 0.0,
                     "bogus_len": 4.2}]
    with pytest.raises(RevitImportError) as ei:
        exchange_to_plan(doc)
    assert "bogus_len" in str(ei.value)


def test_extension_unknown_numeric_field_raises():
    doc = _gallery_doc("cottage.barn")
    doc["units"] = "meters"
    doc["rooms"][0]["surprise"] = 9.9
    with pytest.raises(ext_exchange.ExchangeError) as ei:
        ext_exchange.load(doc)
    assert "surprise" in str(ei.value)


def test_feet_path_ignores_unknown_numeric_field():
    # The feet path does no conversion, so a stray numeric field is harmless
    # there (the loud check only guards the metric conversion walker).
    doc = _metric_doc()
    doc["units"] = "feet"
    doc["rooms"][0]["bogus"] = 3.0
    plan = exchange_to_plan(doc)  # no raise
    assert plan.room("living") is not None


# --- extension load + validate accept metric ---------------------------------


def test_extension_load_normalises_metric_to_feet():
    doc = _gallery_doc("cottage.barn")
    metric = copy.deepcopy(doc)
    _convert_document(metric, 1.0 / F)
    metric["units"] = "meters"
    loaded = ext_exchange.load(metric)
    assert loaded["units"] == "feet"
    # A representative length matches the feet original.
    assert loaded["rooms"][0]["width"] == pytest.approx(doc["rooms"][0]["width"], abs=1e-6)


def test_extension_validate_accepts_metric_pre_normalisation():
    # validate() reports dangling references and is unit-agnostic, so it must run
    # clean on a metric document that hasn't been normalised yet.
    doc = _gallery_doc("cottage.barn")
    doc["units"] = "meters"
    assert ext_exchange.validate(doc) == []


# --- the two conversion tables stay in sync ----------------------------------


def test_unit_tables_stay_in_sync():
    # The extension can't import the core, so the table is duplicated; assert the
    # two copies (and the factor / accepted spellings) are identical.
    assert ext_exchange._UNIT_FIELDS == _UNIT_FIELDS
    assert ext_exchange.FOOT_PER_METER == FOOT_PER_METER
    from barndsl.revit import _FEET_UNITS, _METER_UNITS

    assert ext_exchange._FEET_UNITS == _FEET_UNITS
    assert ext_exchange._METER_UNITS == _METER_UNITS
