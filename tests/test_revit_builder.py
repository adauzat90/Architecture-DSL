"""Tests for the Revit builder, run against a fake Revit API (`revit_fakes`).

The builder can't talk to a real Revit here, but it's ordinary code driving an
API of a known shape. These tests install that fake shape and assert the builder
*drives it correctly*: picks the right wall types, hosts openings, sizes door/
window families, rolls back a dry run, honours named overrides, builds structure/
porches/stairs, and reads a model back. They verify the builder's logic — not
that Revit does the right thing with the calls (only a real Revit can).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))  # for revit_fakes
import revit_fakes  # noqa: E402

revit_fakes.install()  # MUST run before importing the builder

_LIB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "revit", "barndsl.extension", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from barndsl_revit import builder, report  # noqa: E402
from revit_fakes import BuiltInCategory as BIC  # noqa: E402
from revit_fakes import FakeDocument, WallFunction  # noqa: E402

from barndsl import compile_source, exchange_to_plan, to_revit_model  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _exchange(dsl: str) -> dict:
    plan = compile_source(dsl).plan
    assert plan is not None
    return to_revit_model(plan).to_dict()


def _example_exchange(rel: str) -> dict:
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        return _exchange(fh.read())


def _ready_doc(**kw):
    """A project stocked with everything a full build needs."""
    doc = FakeDocument()
    doc.add_level(0.0, "Level 1")
    if kw.get("two_levels"):
        doc.add_level(kw.get("level_gap", 9.0), "Level 2")
    if kw.get("walls", True):
        doc.add_wall_type("Ext", function=WallFunction.Exterior)
        doc.add_wall_type("Int", function=WallFunction.Interior)
    if kw.get("floor", True):
        doc.add_floor_type("Generic 12")
    if kw.get("roof", True):
        doc.add_roof_type("Gable Metal")
    if kw.get("doors", True):
        doc.add_family(BIC.OST_Doors, "Single-Flush")
    if kw.get("windows", True):
        doc.add_family(BIC.OST_Windows, "Fixed")
    if kw.get("columns"):
        doc.add_family(BIC.OST_StructuralColumns, "HSS-Column")
    if kw.get("framing"):
        doc.add_family(BIC.OST_StructuralFraming, "W-Wide Flange")
    if kw.get("plumbing", True):
        doc.add_family(BIC.OST_PlumbingFixtures, "Toilet-Domestic")
    if kw.get("appliances", True):
        doc.add_family(BIC.OST_SpecialityEquipment, "Refrigerator")
    return doc


CEDAR = """\
plan "Cedar"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bed: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bed width 2.67
door living - bath width 2.67
entry living south width 3 offset 10
window bed east width 4 offset 4
window living west width 6 offset 10
"""


# --- forward build -----------------------------------------------------------


def test_basic_build_counts_and_commits():
    doc = _ready_doc()
    rep = builder.build(doc, _exchange(CEDAR))
    assert rep.count(status="created", kind="wall") > 0
    assert rep.count(status="created", kind="door") == 3  # 2 interior + 1 entry
    assert rep.count(status="created", kind="window") == 2
    assert rep.count(status="created", kind="room") == 3
    assert doc.commits == ["Build barndsl plan"]
    assert doc.rollbacks == []


def test_exterior_openings_host_on_exterior_walls():
    doc = _ready_doc()
    ext_id = next(w.Id.Value for w in doc.wall_types if w.Name == "Ext")
    int_id = next(w.Id.Value for w in doc.wall_types if w.Name == "Int")
    data = _exchange(CEDAR)
    builder.build(doc, data)
    # Correlate each created family instance with its host wall's type.
    host_types = []
    for entry in doc.created:
        if entry[0] == "instance":
            args = entry[2]
            host = args[2]  # openings: NewFamilyInstance(point, sym, host, level, st)
            # Fixtures place a family the same way but hosted on a level, not a
            # wall — skip those (they carry no wtype_id).
            if hasattr(host, "wtype_id"):
                host_types.append(host.wtype_id.Value)
    # Every window/entry in CEDAR is exterior, so at least one exterior host used.
    assert ext_id in host_types
    # And the interior doors used the interior type.
    assert int_id in host_types


def test_no_wall_type_builds_nothing():
    doc = _ready_doc(walls=False)
    rep = builder.build(doc, _exchange(CEDAR))
    assert rep.count(status="created") == 0
    assert any("no basic wall type" in n for n in rep.notes)
    assert doc.commits == []


def test_missing_door_family_skips_doors():
    doc = _ready_doc(doors=False)
    rep = builder.build(doc, _exchange(CEDAR))
    assert rep.count(status="created", kind="door") == 0
    skipped = [r for r in rep.records if r.kind == "door" and r.status == "skipped"]
    assert skipped and all("no door family" in r.message for r in skipped)


def test_room_unplaced_is_reported_not_fatal():
    doc = _ready_doc()
    doc.room_unplaced = True  # NewRoom returns None (point not enclosed)
    rep = builder.build(doc, _exchange(CEDAR))
    assert rep.count(status="created", kind="room") == 0
    assert all("enclosed" in r.message for r in rep.records if r.kind == "room")


def test_family_instance_failure_is_caught():
    doc = _ready_doc()
    doc.fail_family_instance = True
    rep = builder.build(doc, _exchange(CEDAR))
    assert rep.count(status="failed", kind="door") > 0
    # The build still commits (defensive per-element handling).
    assert doc.commits == ["Build barndsl plan"]


# --- dry run -----------------------------------------------------------------


def test_dry_run_rolls_back_and_reports():
    doc = _ready_doc()
    opts = report.BuildOptions(dry_run=True)
    rep = builder.build(doc, _exchange(CEDAR), opts)
    assert rep.dry_run is True
    assert rep.summary_line().startswith("[dry run] would create")
    # A real build happened then rolled back — nothing committed.
    assert doc.rollbacks and doc.commits == []
    # Counts are still populated (the preview is accurate).
    assert rep.count(status="created", kind="wall") > 0


# --- family sizing -----------------------------------------------------------


def _door_symbol_names(doc):
    return [s.Name for s in doc.symbols.get(BIC.OST_Doors, [])]


def test_sizing_duplicates_one_type_per_distinct_size():
    # Two doors at 3.0 and one at 2.5 → two distinct sized door types.
    dsl = """\
plan "S"
envelope 40 x 20
ceiling 9
room a: living at 0,0 size 20 x 20
room b: bedroom at 20,0 size 20 x 20
room c: office at 0,0 size 0 x 0
door a - b width 3
entry a south width 3 offset 5
entry b south width 2.5 offset 5
"""
    # (room c is a no-op guard; remove if it trips validation)
    dsl = dsl.replace("room c: office at 0,0 size 0 x 0\n", "")
    doc = _ready_doc()
    builder.build(doc, _exchange(dsl))
    sized = [n for n in _door_symbol_names(doc) if n.startswith("barndsl")]
    assert len(sized) == 2  # 3.00x6.67 and 2.50x6.67


def test_sizing_sets_width_param():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    sized = [s for s in doc.symbols.get(BIC.OST_Doors, []) if s.Name.startswith("barndsl")]
    assert sized
    for s in sized:
        w = s.get_Parameter(revit_fakes.BuiltInParameter.FAMILY_WIDTH_PARAM).AsDouble()
        assert w > 0


def test_size_families_disabled_creates_no_duplicates():
    doc = _ready_doc()
    opts = report.BuildOptions(size_families=False)
    builder.build(doc, _exchange(CEDAR), opts)
    assert not any(n.startswith("barndsl") for n in _door_symbol_names(doc))


# --- named overrides ---------------------------------------------------------


def test_named_override_selects_that_type():
    doc = _ready_doc()
    doc.add_wall_type("Special Exterior", function=WallFunction.Exterior)
    opts = report.BuildOptions(exterior_wall_type="Special Exterior")
    rep = builder.build(doc, _exchange(CEDAR), opts)
    assert rep.resources["exterior_wall"] == "Special Exterior"


def test_missing_named_override_falls_back_with_note():
    doc = _ready_doc()
    opts = report.BuildOptions(door_family="Nonexistent Family")
    rep = builder.build(doc, _exchange(CEDAR), opts)
    assert any("door family 'Nonexistent Family' not found" in n for n in rep.notes)
    # Still built using the auto-picked family.
    assert rep.count(status="created", kind="door") == 3


# --- structure & porches -----------------------------------------------------


def test_structure_builds_when_families_present():
    doc = _ready_doc(columns=True, framing=True)
    with open(os.path.join(EXAMPLES, "cedar_ridge.barn"), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    plan.frame()
    from barndsl import emit_dsl

    plan = compile_source(emit_dsl(plan), name=plan.name).plan
    rep = builder.build(doc, to_revit_model(plan).to_dict())
    assert rep.count(status="created", kind="column") > 0
    assert rep.count(status="created", kind="framing") > 0


def test_structure_skipped_without_families():
    doc = _ready_doc()  # no structural families
    with open(os.path.join(EXAMPLES, "cedar_ridge.barn"), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    plan.frame()
    from barndsl import emit_dsl

    plan = compile_source(emit_dsl(plan), name=plan.name).plan
    rep = builder.build(doc, to_revit_model(plan).to_dict())
    assert rep.count(status="created", kind="column") == 0
    assert any("structural-column family" in n for n in rep.notes)


def test_porch_builds_as_floor():
    doc = _ready_doc()
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    assert rep.count(status="created", kind="porch") == 1


def test_porch_skipped_without_floor_type():
    doc = _ready_doc(floor=False)
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    assert rep.count(status="created", kind="porch") == 0


# --- slabs, grids, roof ------------------------------------------------------


def test_floor_slabs_build():
    doc = _ready_doc()
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    # cedar_ridge is a single rectangle → one ground slab.
    assert rep.count(status="created", kind="slab") == 1


def test_slabs_skipped_without_floor_type():
    doc = _ready_doc(floor=False)
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    assert rep.count(status="created", kind="slab") == 0


def _framed_exchange():
    with open(os.path.join(EXAMPLES, "cedar_ridge.barn"), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    plan.frame()
    from barndsl import emit_dsl

    plan = compile_source(emit_dsl(plan), name=plan.name).plan
    return to_revit_model(plan).to_dict()


def test_grids_build_from_a_frame():
    data = _framed_exchange()
    doc = _ready_doc()
    rep = builder.build(doc, data)
    assert rep.count(status="created", kind="grid") == len(data["grids"]) > 0
    # Grid names are set from the labels.
    grid_names = [g.Name for k, g in ((e[0], e[1]) for e in doc.created) if k == "grid"]
    assert "1" in grid_names and "A" in grid_names


def test_no_grids_without_a_frame():
    doc = _ready_doc()
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    assert rep.count(kind="grid") == 0


def test_roof_builds_as_footprint_roof():
    doc = _ready_doc()
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    assert rep.count(status="created", kind="roof") == 1
    assert any(k == "roof" for k, *_ in doc.created)


def test_roof_skipped_without_roof_type():
    doc = _ready_doc(roof=False)
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"))
    assert rep.count(status="created", kind="roof") == 0
    assert any(r.kind == "roof" and r.status == "skipped" for r in rep.records)


def test_fixtures_placed_when_families_loaded():
    doc = _ready_doc()
    data = _example_exchange("cedar_ridge.barn")
    rep = builder.build(doc, data)
    assert data["fixtures"], "example should carry fixture seeds"
    # Every seed becomes a placed family instance.
    assert rep.count(status="created", kind="fixture") == len(data["fixtures"])


def test_fixtures_skipped_without_families():
    doc = _ready_doc(plumbing=False, appliances=False)
    data = _example_exchange("cedar_ridge.barn")
    rep = builder.build(doc, data)
    assert rep.count(status="created", kind="fixture") == 0
    assert rep.count(status="skipped", kind="fixture") == len(data["fixtures"])


def test_fixtures_pass_can_be_disabled():
    doc = _ready_doc()
    rep = builder.build(doc, _example_exchange("cedar_ridge.barn"), report.BuildOptions(fixtures=False))
    assert rep.count(kind="fixture") == 0


def test_roof_slopes_its_eave_edges():
    doc = _ready_doc()
    data = _example_exchange("cedar_ridge.barn")
    builder.build(doc, data)
    roofs = [el for k, el in ((e[0], e[1]) for e in doc.created) if k == "roof"]
    assert len(roofs) == 1
    # Two eaves were made slope-defining, at the plan's pitch; gable ends aren't.
    expected = sum(1 for s in data["roof"]["outline_slopes"] if s)
    assert len(roofs[0].slopes) == expected == 2
    angle = data["roof"]["slope_angle"]
    assert all(a == pytest.approx(angle) for a in roofs[0].slopes.values())


def test_gable_walls_build_from_a_profile():
    doc = _ready_doc()
    data = _example_exchange("cedar_ridge.barn")
    rep = builder.build(doc, data)
    walls = [el for k, el in ((e[0], e[1]) for e in doc.created) if k == "wall"]
    gables = [w for w in walls if getattr(w, "profile", None)]
    n_gable = sum(1 for w in data["walls"] if w.get("profile") == "gable")
    assert n_gable > 0
    # Every gable-end wall used the vertical-profile overload (a 5-edge pentagon);
    # eave/interior walls used the flat line overload.
    assert len(gables) == n_gable
    assert all(len(w.profile) == 5 for w in gables)
    assert rep.count(status="created", kind="wall") == len(data["walls"])


def test_slabs_grids_roof_can_be_disabled():
    data = _framed_exchange()
    doc = _ready_doc()
    opts = report.BuildOptions(slabs=False, grids=False, roof=False)
    rep = builder.build(doc, data, opts)
    assert rep.count(kind="slab") == 0
    assert rep.count(kind="grid") == 0
    assert rep.count(kind="roof") == 0
    # Walls etc. still built.
    assert rep.count(status="created", kind="wall") > 0


# --- stairs ------------------------------------------------------------------


def test_stairs_build_via_edit_scope():
    doc = _ready_doc(two_levels=True)
    rep = builder.build(doc, _example_exchange("gallery/two_story.barn"))
    assert rep.count(status="created", kind="stair") == 1
    assert doc.stair_commits == 1 and doc.stair_cancels == 0
    # At least one straight run was created.
    assert any(k == "stair_run" for k, *_ in doc.created)


def test_stairs_dry_run_cancels_scope():
    doc = _ready_doc(two_levels=True)
    opts = report.BuildOptions(dry_run=True)
    builder.build(doc, _example_exchange("gallery/two_story.barn"), opts)
    assert doc.stair_cancels == 1 and doc.stair_commits == 0


def test_switchback_stair_builds_two_runs_and_a_landing():
    # A 7x7 stair footprint can't fit a straight 9 ft climb, so the planner folds
    # it into a switchback — and the builder should instantiate two runs + a landing.
    dsl = """\
plan "SB"
envelope 24 x 20
ceiling 9
room living: living at 0,0 size 17 x 20
room well: hallway at 17,0 size 7 x 20
room loft: loft at 17,0 size 7 x 20 level 1
stair flight at 17,0 size 7 x 7 from 0 to 1
open living - well width 4
entry living south width 3 offset 8
window living south width 6 offset 2
window loft south width 4 offset 1
"""
    data = _exchange(dsl)
    stair_area = [a for a in data["areas"] if a["kind"] == "stair"][0]
    assert stair_area["meta"]["plan"]["layout"] == "switchback"

    doc = _ready_doc(two_levels=True)
    rep = builder.build(doc, data)
    assert rep.count(status="created", kind="stair") == 1
    assert sum(1 for k, *_ in doc.created if k == "stair_run") == 2
    assert sum(1 for k, *_ in doc.created if k == "stair_landing") == 1


def test_stairs_disabled():
    doc = _ready_doc(two_levels=True)
    opts = report.BuildOptions(stairs=False)
    rep = builder.build(doc, _example_exchange("gallery/two_story.barn"), opts)
    assert rep.count(kind="stair") == 0


# --- idempotent re-build -----------------------------------------------------


def _managed_count(doc):
    n = 0
    for e in doc._by_id.values():
        p = e.get_Parameter(revit_fakes.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p is not None and p.AsString() == builder.MANAGED_MARK:
            n += 1
    return n


def test_created_elements_are_marked_managed():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    assert _managed_count(doc) > 0
    # Levels are reused, not marked (so a re-build won't delete them).
    for lv in doc.levels:
        p = lv.get_Parameter(revit_fakes.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        assert p.AsString() != builder.MANAGED_MARK


def test_replace_rebuild_is_idempotent():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    after_one = _managed_count(doc)
    rep = builder.build(doc, data, report.BuildOptions(replace=True))
    after_two = _managed_count(doc)
    assert after_two == after_one  # replaced, not doubled
    assert any("replaced" in n for n in rep.notes)


def test_without_replace_rebuild_duplicates():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data, report.BuildOptions(replace=False))
    one = _managed_count(doc)
    builder.build(doc, data, report.BuildOptions(replace=False))
    assert _managed_count(doc) == 2 * one


def test_replace_leaves_unmanaged_elements_alone():
    doc = _ready_doc()
    # A user-drawn wall (no managed mark).
    user_wall = revit_fakes.Wall("user", doc)
    builder.build(doc, _exchange(CEDAR), report.BuildOptions(replace=True))
    assert user_wall.Id.Value in doc._by_id  # survived the purge


# --- diagnostics -------------------------------------------------------------


def test_diagnose_reports_readiness():
    doc = _ready_doc(columns=True)
    info = builder.diagnose(doc)
    assert info["revit"].startswith("2025")
    assert info["ready"]["walls"] is True
    assert info["ready"]["doors"] is True
    assert info["ready"]["structural_columns"] is True
    assert info["ready"]["structural_framing"] is False  # not added
    assert "Single-Flush" in info["door_families"]


# --- reading a model back ----------------------------------------------------


def test_read_model_round_trips_to_a_plan():
    doc = _ready_doc()
    level = doc.levels[0]
    # Two abutting rooms, placed away from the origin to exercise normalisation.
    living = doc.add_placed_room("Living Room", 10, 5, 18, 24, level)
    bed = doc.add_placed_room("Master Bedroom", 28, 5, 12, 24, level)
    # An interior door on the shared wall, and an exterior window on Living's south.
    doc.add_instance(BIC.OST_Doors, 28, 17, from_room=living, to_room=bed, width=3.0)
    doc.add_instance(BIC.OST_Windows, 19, 5, from_room=living, to_room=None, width=4.0, sill=3.0)

    exchange, rep = builder.read_model(doc)
    assert len(exchange["rooms"]) == 2
    # Normalised so the south-west corner is at the origin.
    assert min(r["x"] for r in exchange["rooms"]) == pytest.approx(0.0)
    assert min(r["y"] for r in exchange["rooms"]) == pytest.approx(0.0)
    # Names mapped to types.
    types = {r["id"]: r["type"] for r in exchange["rooms"]}
    assert "bedroom" in types.values() and "living" in types.values()
    # One interior door (two rooms) and one window (one room).
    doors = [o for o in exchange["openings"] if o["category"] == "door"]
    windows = [o for o in exchange["openings"] if o["category"] == "window"]
    assert len(doors) == 1 and len(doors[0]["rooms"]) == 2
    assert len(windows) == 1

    # And the exchange feeds the (tested) core reconstruction cleanly.
    plan = exchange_to_plan(exchange)
    assert len(plan.rooms) == 2
    assert len(plan.interior_doors) == 1
    assert len(plan.windows) == 1


def test_read_model_skips_unplaced_rooms():
    doc = _ready_doc()
    level = doc.levels[0]
    rm = doc.add_placed_room("Living", 0, 0, 10, 10, level)
    rm.Area = 0  # mark unplaced
    rm.Location = None
    exchange, rep = builder.read_model(doc)
    assert exchange["rooms"] == []
    assert any(r.kind == "room" and r.status == "skipped" for r in rep.records)
