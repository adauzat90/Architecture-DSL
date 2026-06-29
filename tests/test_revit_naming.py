"""Tests for the Revit→DSL name heuristics (`barndsl_revit.naming`).

Revit-free, so it runs in the normal suite. These guard the room-type guesser
(the heuristic most likely to need tuning) and the id slugger used when reading a
Revit model back into the DSL.
"""

from __future__ import annotations

import os
import sys

import pytest

from barndsl import RoomType  # to assert the guesses are real DSL types

_LIB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "revit", "barndsl.extension", "lib"
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import naming  # noqa: E402


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Master Bath", "bathroom"),  # bath wins over master
        ("Master Bedroom", "bedroom"),
        ("Primary Suite", "bedroom"),
        ("Powder Room", "half_bath"),
        ("Kitchen", "kitchen"),
        ("Great Room", "living"),
        ("Living Room", "living"),
        ("Dining", "dining"),
        ("Hallway", "hallway"),
        ("Foyer", "hallway"),
        ("Walk-in Closet", "closet"),
        ("Laundry", "laundry"),
        ("Home Office", "office"),
        ("2-Car Garage", "garage"),
        ("Workshop", "shop"),
        ("Front Porch", "porch"),
        ("Server Closet 3", "closet"),
        ("", "other"),
        ("Vestibule", "other"),
    ],
)
def test_guess_room_type(name, expected):
    assert naming.guess_room_type(name) == expected


def test_guessed_types_are_valid_dsl_types():
    for _kw, rtype in naming.TYPE_KEYWORDS:
        RoomType(rtype)  # raises if a keyword maps to a non-type


def test_slug_id_is_valid_identifier():
    used = set()
    assert naming.slug_id("Great Room", used) == "great_room"
    assert naming.slug_id("2-Car Garage", used) == "r_2_car_garage"  # can't start with a digit
    assert naming.slug_id("Bath #1!", used) == "bath_1"


def test_slug_id_disambiguates_collisions():
    used = set()
    a = naming.slug_id("Bedroom", used)
    b = naming.slug_id("Bedroom", used)
    c = naming.slug_id("Bedroom", used)
    assert (a, b, c) == ("bedroom", "bedroom_2", "bedroom_3")


def test_slug_id_handles_empty():
    used = set()
    assert naming.slug_id("", used) == "room"
    assert naming.slug_id("", used) == "room_2"
