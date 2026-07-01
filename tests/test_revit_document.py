"""Tests for the documentation pass (`builder.document`) via the fake Revit API.

Covers the Tier-2 deliverables — floor-plan views, room/door/window tags, native
schedules, and a sheet per level — plus their idempotent re-document. Like the
other builder tests, these verify the builder *drives the view/annotation API
correctly*; only a live Revit confirms placement/appearance.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import revit_fakes  # noqa: E402

revit_fakes.install()

_LIB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "revit", "barndsl.extension", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import builder, report  # noqa: E402
from revit_fakes import BuiltInCategory as BIC  # noqa: E402
from revit_fakes import FakeDocument, WallFunction  # noqa: E402

from barndsl import compile_source, to_revit_model  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

CEDAR = """\
plan "Cedar"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bed: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bed width 2.67
entry living south width 3 offset 10
window bed east width 4 offset 4
window living west width 6 offset 10
"""


def _doc(**kw):
    doc = FakeDocument()
    doc.add_level(0.0, "Level 1")
    if kw.get("two_levels"):
        # Floor-to-floor for the two_story example (ceiling 9 + 1 ft assembly).
        doc.add_level(10.0, "Level 2")
    doc.add_wall_type("Ext", function=WallFunction.Exterior)
    doc.add_wall_type("Int", function=WallFunction.Interior)
    doc.add_floor_type("Generic 12")
    doc.add_roof_type("Gable")
    doc.add_family(BIC.OST_Doors, "Single-Flush")
    doc.add_family(BIC.OST_Windows, "Fixed")
    if kw.get("view_type", True):
        doc.add_view_family_type("Floor Plan")
    if kw.get("title_block", True):
        doc.add_title_block("A1 Title Block")
    return doc


def _exchange(dsl):
    return to_revit_model(compile_source(dsl).plan).to_dict()


def _example_exchange(rel):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        return _exchange(fh.read())


def _built(**kw):
    doc = _doc(**kw)
    builder.build(doc, kw.get("exchange", _exchange(CEDAR)))
    return doc


def test_document_creates_view_schedules_sheet():
    doc = _built()
    rep = builder.document(doc)
    assert rep.count(status="created", kind="view") == 1  # one level
    assert rep.count(status="created", kind="schedule") == 3  # doors, windows, rooms
    assert rep.count(status="created", kind="sheet") == 1


def test_document_tags_managed_rooms_and_openings():
    doc = _built()
    rep = builder.document(doc)
    # 3 rooms + 2 doors (interior + entry) + 2 windows = 7 tags.
    assert rep.count(status="created", kind="tag") == 7


def test_two_levels_get_a_view_and_sheet_each():
    doc = _built(two_levels=True, exchange=_example_exchange("gallery/two_story.barn"))
    rep = builder.document(doc)
    assert rep.count(status="created", kind="view") == 2
    assert rep.count(status="created", kind="sheet") == 2


def test_redocument_is_idempotent():
    doc = _built()
    builder.document(doc)
    v1, s1, sc1 = len(doc.views), len(doc.sheets), len(doc.schedules)
    rep = builder.document(doc)
    assert (len(doc.views), len(doc.sheets), len(doc.schedules)) == (v1, s1, sc1)
    assert any("replaced" in n for n in rep.notes)


def test_views_skipped_without_a_view_type():
    doc = _built(view_type=False)
    rep = builder.document(doc)
    assert rep.count(status="created", kind="view") == 0
    assert any("no floor-plan view type" in n for n in rep.notes)
    # Schedules don't need a view type, so they still build.
    assert rep.count(status="created", kind="schedule") == 3


def test_sheets_skipped_without_a_title_block():
    doc = _built(title_block=False)
    rep = builder.document(doc)
    assert rep.count(status="created", kind="sheet") == 0
    assert any("no title block" in n for n in rep.notes)


def test_each_doc_pass_can_be_disabled():
    doc = _built()
    opts = report.BuildOptions(views=False, tags=False, schedules=False, sheets=False)
    rep = builder.document(doc, opts)
    for kind in ("view", "tag", "schedule", "sheet"):
        assert rep.count(kind=kind) == 0


def test_document_only_tags_managed_elements():
    # A hand-drawn (unmarked) room shouldn't be tagged.
    doc = _built()
    level = doc.levels[0]
    doc.add_placed_room("User Room", 100, 100, 10, 10, level)  # unmarked
    rep = builder.document(doc)
    # Still 7 (the 3 built rooms + 4 openings), not 8.
    assert rep.count(status="created", kind="tag") == 7
