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
    if kw.get("foundation", True):
        doc.add_family(BIC.OST_StructuralFoundation, "Footing-Rectangular")
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


def _instances_of(doc, structural_type):
    """Created family instances whose 4th NewFamilyInstance arg is this type."""
    out = []
    for e in doc.created:
        if e[0] == "instance" and len(e) >= 3 and len(e[2]) >= 4 and e[2][3] == structural_type:
            out.append((e[1], e[2]))
    return out


def test_bents_are_placed_at_plate_height_not_the_floor():
    # Regression: framing was drawn at the floor (z=0); a bent belongs at the
    # plate (top of the posts = ceiling height above the floor).
    data = _framed_exchange()
    doc = _ready_doc(columns=True, framing=True)
    builder.build(doc, data)
    from revit_fakes import Structure

    beams = _instances_of(doc, Structure.StructuralType.Beam)
    assert beams, "no beams created"
    # cedar_ridge ceiling is 12; every bent/ridge sits at or above the plate.
    for _inst, args in beams:
        curve = args[0]
        assert curve.p1.Z >= 12.0 - 1e-6
        assert abs(curve.p1.Z - curve.p2.Z) < 1e-6  # level member


def test_posts_get_a_top_at_the_plate():
    data = _framed_exchange()
    doc = _ready_doc(columns=True, framing=True)
    builder.build(doc, data)
    from revit_fakes import BuiltInParameter as BIP
    from revit_fakes import Structure

    cols = _instances_of(doc, Structure.StructuralType.Column)
    assert cols, "no columns created"
    inst, _args = cols[0]
    # A single-storey post keeps its base level as the top level and offsets the
    # top up to the plate (ceiling height = 12).
    top_off = inst.get_Parameter(BIP.FAMILY_TOP_LEVEL_OFFSET_PARAM).AsDouble()
    assert abs(top_off - 12.0) < 1e-6
    assert inst.get_Parameter(BIP.FAMILY_TOP_LEVEL_PARAM).AsElementId() is not None


def test_wall_top_constrains_to_the_level_above_on_a_two_storey_plan():
    doc = _ready_doc(two_levels=True, level_gap=9.0)  # Level 2 at the plate
    data = _exchange(
        'plan "T"\nenvelope 30 x 24\nceiling 9\n'
        "room living: living at 0,0 size 30 x 24\n"
        "room loft: loft at 0,0 size 30 x 24 level 1\n"
        "entry living south width 3 offset 10\n"
        "window living west width 8 offset 8\n"
        "window loft west width 8 offset 8\n"
        "stair s at 0,0 size 4 x 12 from 0 to 1\n"
    )
    rep = builder.build(doc, data)
    from revit_fakes import BuiltInParameter as BIP

    ground = [w for k, w in ((e[0], e[1]) for e in doc.created)
              if k == "wall" and getattr(w, "level_id", None) is not None
              and w.get_Parameter(BIP.WALL_HEIGHT_TYPE).AsElementId() is not None]
    assert ground, "no ground-storey wall was constrained to the level above"
    assert any("constrained" in n for n in rep.notes)


def test_wall_location_line_defaults_to_centreline_untouched():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    from revit_fakes import BuiltInParameter as BIP

    walls = [w for k, w in ((e[0], e[1]) for e in doc.created) if k == "wall"]
    # Default: location line left at centreline (0), no geometry shift.
    assert all(w.get_Parameter(BIP.WALL_KEY_REF_PARAM).AsDouble() == 0 for w in walls)


def test_wall_location_line_finish_face_exterior_is_applied_when_configured():
    doc = _ready_doc()
    opts = report.BuildOptions(location_line="finish_face_exterior")
    builder.build(doc, _exchange(CEDAR), opts)
    from revit_fakes import BuiltInParameter as BIP

    walls = [w for k, w in ((e[0], e[1]) for e in doc.created) if k == "wall"]
    # Exterior walls get FinishFaceExterior (2); interior walls stay centreline.
    assert any(w.get_Parameter(BIP.WALL_KEY_REF_PARAM).AsDouble() == 2 for w in walls)


def test_rooms_get_numbers_and_finishes():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    from revit_fakes import BuiltInParameter as BIP

    rooms = list(doc.placed_rooms)
    assert rooms
    numbers = [r.get_Parameter(BIP.ROOM_NUMBER).AsString() for r in rooms]
    assert "101" in numbers  # ground-floor rooms number 101, 102, …
    assert all(n for n in numbers)  # every room is numbered
    # A bathroom gets a tile floor finish from the default schedule.
    finishes = [r.get_Parameter(BIP.ROOM_FINISH_FLOOR).AsString() for r in rooms]
    assert any(f == "Tile" for f in finishes)


def test_ceilings_build_per_room():
    doc = _ready_doc()
    doc.add_ceiling_type("2x2 ACT")
    rep = builder.build(doc, _exchange(CEDAR))
    # cedar has 3 rooms → 3 ceilings, each at the ceiling height above the level.
    assert rep.count(status="created", kind="ceiling") == 3
    from revit_fakes import BuiltInParameter as BIP

    ceils = [c for k, c in ((e[0], e[1]) for e in doc.created) if k == "ceiling"]
    assert all(
        abs(c.get_Parameter(BIP.CEILING_HEIGHTABOVELEVEL_PARAM).AsDouble() - 10.0) < 1e-6
        for c in ceils
    )


def test_ceilings_skipped_without_a_ceiling_type():
    doc = _ready_doc()  # no ceiling type added
    rep = builder.build(doc, _exchange(CEDAR))
    assert rep.count(kind="ceiling") == 0
    assert any("ceiling" in n for n in rep.notes)


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


def test_foundation_footings_land_under_posts():
    data = _framed_exchange()
    doc = _ready_doc()
    rep = builder.build(doc, data)
    n_posts = len(data["foundation"]["footings"])
    assert n_posts > 0
    assert rep.count(status="created", kind="footing") == n_posts
    # The thickened-edge turndown is reported for detailing.
    assert any("turndown" in n for n in rep.notes)


def test_foundation_footings_skipped_without_family():
    data = _framed_exchange()
    doc = _ready_doc(foundation=False)
    rep = builder.build(doc, data)
    assert rep.count(status="created", kind="footing") == 0
    assert rep.count(status="skipped", kind="footing") == len(data["foundation"]["footings"])


def test_foundation_pass_can_be_disabled():
    data = _framed_exchange()
    doc = _ready_doc()
    rep = builder.build(doc, data, report.BuildOptions(foundation=False))
    assert rep.count(kind="footing") == 0


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
    # The marker now lives in Extensible Storage, not Comments — count via the
    # builder's own predicate.
    return sum(1 for e in doc._by_id.values() if builder._is_managed(e))


def test_created_elements_are_marked_managed():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    assert _managed_count(doc) > 0
    # Levels are reused, not marked (so a re-build won't delete them).
    for lv in doc.levels:
        assert not builder._is_managed(lv)


def test_managed_mark_does_not_touch_the_comments_field():
    # Regression: the marker used to squat on Comments, clobbering user
    # annotations. It now lives in Extensible Storage; the only Comments value a
    # build writes is the egress stamp on egress doors ("barndsl egress"), which
    # is a schedule filter, not the managed marker.
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    for e in doc._by_id.values():
        p = e.get_Parameter(revit_fakes.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        assert p is None or p.AsString() in ("", None, "barndsl egress")
        assert p is None or p.AsString() != builder.MANAGED_MARK


def test_legacy_comments_marker_is_still_recognised():
    # An element marked the old way (Comments) is still treated as managed, so an
    # old build is cleaned by a new one.
    doc = _ready_doc()
    legacy = revit_fakes.Wall("legacy", doc)
    legacy.get_Parameter(
        revit_fakes.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
    ).Set(builder.MANAGED_MARK)
    assert builder._is_managed(legacy)


def test_replace_rebuild_is_idempotent():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    after_one = _managed_count(doc)
    rep = builder.build(doc, data, report.BuildOptions(replace=True))
    after_two = _managed_count(doc)
    assert after_two == after_one  # replaced, not doubled
    # The default replace re-build is a diff: nothing changed, so everything is
    # kept in place rather than purged and recreated.
    assert rep.count(status="kept") == after_one
    assert rep.count(status="created") == 0


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


# --- diff rebuild (update-in-place) --------------------------------------------

from barndsl_revit import exchange as bx  # noqa: E402


def _managed_ids(doc):
    """{identity_key: revit element id} for every identity-stamped element."""
    out = {}
    for e in doc._by_id.values():
        ident = builder._es_identity(e)
        if ident is not None:
            out[ident[0]] = e.Id.Value
    return out


def test_identities_are_deterministic_across_reexports():
    # Re-exporting the same plan must yield byte-identical identity keys and
    # fingerprints, or unchanged elements would be needlessly recreated.
    a = bx.identities(_example_exchange("cedar_ridge.barn"))
    b = bx.identities(_example_exchange("cedar_ridge.barn"))
    assert a == b
    keys = [k for _kind, _src, k, _fp in a]
    assert len(keys) == len(set(keys))  # identity keys are unique


def test_unchanged_rebuild_keeps_every_element_id():
    doc = _ready_doc(columns=True, framing=True)
    doc.add_ceiling_type("2x2 ACT")
    rep1 = builder.build(doc, _framed_exchange())
    ids1 = _managed_ids(doc)
    assert ids1
    rep2 = builder.build(doc, _framed_exchange())
    ids2 = _managed_ids(doc)
    # Every element survived with its Revit id intact — user dimensions/tags
    # attached to them stay live across the iteration.
    assert ids2 == ids1
    assert rep2.count(status="kept") == len(ids1)
    # Nothing managed was recreated (only non-element passes may re-run).
    created = [r for r in rep2.records if r.status == "created"]
    assert all(r.kind == "project" for r in created)


def test_unchanged_dry_run_rebuild_reports_kept_and_rolls_back():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    ids1 = _managed_ids(doc)
    rep = builder.build(doc, data, report.BuildOptions(dry_run=True))
    assert rep.count(status="kept") == len(ids1)
    assert doc.rollbacks  # nothing committed
    assert _managed_ids(doc) == ids1


def test_rebuild_keeps_room_numbers():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    from revit_fakes import BuiltInParameter as BIP

    numbers1 = {r.Id.Value: r.get_Parameter(BIP.ROOM_NUMBER).AsString()
                for r in doc.placed_rooms}
    builder.build(doc, data)
    numbers2 = {r.Id.Value: r.get_Parameter(BIP.ROOM_NUMBER).AsString()
                for r in doc.placed_rooms}
    assert numbers2 == numbers1  # same rooms, same numbers


#: CEDAR with the bed/bath boundary moved north one foot — one localised edit.
CEDAR_MOVED = CEDAR.replace(
    "room bed: bedroom at 24,0 size 16 x 15", "room bed: bedroom at 24,0 size 16 x 16"
).replace(
    "room bath: bathroom at 24,15 size 16 x 15", "room bath: bathroom at 24,16 size 16 x 14"
)


def test_moving_a_room_boundary_recreates_only_the_affected_elements():
    doc = _ready_doc()
    data_a = _exchange(CEDAR)
    data_b = _exchange(CEDAR_MOVED)
    builder.build(doc, data_a)
    ids1 = _managed_ids(doc)
    rep2 = builder.build(doc, data_b)
    ids2 = _managed_ids(doc)

    # The expected keeps fall out of the pure exchange-vs-exchange diff.
    a = {k: fp for _kind, _src, k, fp in bx.identities(data_a)}
    b = {k: fp for _kind, _src, k, fp in bx.identities(data_b)}
    expected_kept = {k for k in ids1 if b.get(k) == a.get(k)}
    assert expected_kept, "an unrelated room should survive the edit"

    # Untouched: the living room, its west window, the entry, the walls away
    # from the moved boundary — all keep their Revit ids.
    assert "room|living" in expected_kept
    for k in expected_kept:
        assert ids2[k] == ids1[k]
    assert rep2.count(status="kept") == len(expected_kept)

    # Touched: the resized rooms and the walls/openings along the moved
    # boundary were recreated (new ids), stale ones removed.
    assert ids2["room|bed"] != ids1["room|bed"]
    assert ids2["room|bath"] != ids1["room|bath"]
    changed = set(ids2) - expected_kept
    assert changed
    for k in changed:
        assert ids1.get(k) != ids2[k]

    # The exchange merges the east exterior wall into one unchanged run, so the
    # bed window (whose record is untouched) is kept; the bed/bath divider wall
    # itself moved, so it (and the doors whose locations shifted) recreated.
    bed_win = next(o for o in data_b["openings"] if o["category"] == "window"
                   and "bed" in o["rooms"])
    assert bx.opening_identity(bed_win) in expected_kept
    divider = next(w for w in data_b["walls"] if not w["exterior"]
                   and abs(w["start"][1] - 16.0) < 1e-6 and abs(w["end"][1] - 16.0) < 1e-6)
    assert bx.wall_identity(divider) in changed


def test_changing_a_wall_recreates_its_hosted_openings():
    # An opening whose record is untouched still can't be kept when its host
    # wall changes: Revit deletes hosted instances with their host.
    import copy

    doc = _ready_doc()
    data_a = _exchange(CEDAR)
    builder.build(doc, data_a)
    ids1 = _managed_ids(doc)

    data_b = copy.deepcopy(data_a)
    entry = next(o for o in data_b["openings"]
                 if o["category"] == "door" and o["exterior"])
    host = next(w for w in data_b["walls"] if w["id"] == entry["host_wall"])
    host["height"] = float(host["height"]) + 1.0  # same line, changed record

    rep2 = builder.build(doc, data_b)
    ids2 = _managed_ids(doc)
    wall_key = bx.wall_identity(host)
    entry_key = bx.opening_identity(entry)
    assert ids2[wall_key] != ids1[wall_key]    # wall recreated
    assert ids2[entry_key] != ids1[entry_key]  # hosted door recreated with it
    # Openings on other walls were kept.
    kept_openings = [r for r in rep2.records
                     if r.status == "kept" and r.kind in ("door", "window")]
    assert kept_openings


def test_removed_elements_are_purged_on_rebuild():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    assert len(doc.instances.get(BIC.OST_Windows, [])) == 2
    slim = CEDAR.replace("window bed east width 4 offset 4\n", "")
    rep = builder.build(doc, _exchange(slim))
    # The removed window's element is gone; the other window was kept.
    assert len(doc.instances.get(BIC.OST_Windows, [])) == 1
    assert rep.count(status="kept", kind="window") == 1
    assert any("changed/removed" in n for n in rep.notes)


def test_legacy_marked_elements_are_purged_by_a_diff_rebuild():
    # Elements from pre-identity builds carry only the managed marker — via the
    # old Comments field or the v1 (marker-only) Extensible Storage schema.
    # They can't be matched to a record, so a diff rebuild purges them (the old
    # behaviour) instead of keeping stale geometry around.
    doc = _ready_doc()
    comments_wall = revit_fakes.Wall("legacy-comments", doc)
    comments_wall.get_Parameter(
        revit_fakes.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
    ).Set(builder.MANAGED_MARK)

    ES = revit_fakes.ExtensibleStorage
    sb = ES.SchemaBuilder(builder._LEGACY_SCHEMA_GUID)
    sb.SetSchemaName("BarndslManaged")
    sb.AddSimpleField("marker", str)
    v1_schema = sb.Finish()
    es_wall = revit_fakes.Wall("legacy-es", doc)
    ent = ES.Entity(v1_schema)
    ent.Set("marker", builder.MANAGED_MARK)
    es_wall.SetEntity(ent)
    assert builder._is_managed(es_wall) and builder._es_identity(es_wall) is None

    rep = builder.build(doc, _exchange(CEDAR))
    assert comments_wall.Id.Value not in doc._by_id
    assert es_wall.Id.Value not in doc._by_id
    assert any("older barndsl build" in n for n in rep.notes)


def test_full_rebuild_mode_purges_and_recreates():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    ids1 = _managed_ids(doc)
    rep = builder.build(doc, data, report.BuildOptions(rebuild="full"))
    ids2 = _managed_ids(doc)
    assert rep.count(status="kept") == 0
    assert any("replaced" in n for n in rep.notes)
    # Same plan, so the same identity set — but every element id is new.
    assert set(ids2) == set(ids1)
    assert all(ids2[k] != ids1[k] for k in ids2)


def test_rebuild_option_round_trips_and_validates():
    assert report.BuildOptions().rebuild == "diff"
    assert report.BuildOptions.from_dict({"rebuild": "full"}).rebuild == "full"
    assert report.BuildOptions.from_dict({"rebuild": "bogus"}).rebuild == "diff"
    assert report.BuildOptions(rebuild="Full").to_dict()["rebuild"] == "full"


def test_kept_outcome_shows_in_the_report():
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    rep = builder.build(doc, data)
    assert "kept" in rep.summary_line()
    md = rep.to_markdown()
    assert "| element | created | kept | skipped | failed |" in md
    parsed = rep.to_dict()
    assert parsed["counts"]["wall"]["kept"] > 0
    assert any(r["status"] == "kept" and r["revit_id"] for r in parsed["records"])


def test_disabled_pass_purges_kept_candidates_on_diff_rebuild():
    # Turning a pass off mid-iteration removes its elements, matching the full
    # purge: a diff rebuild must not silently keep what the pass no longer owns.
    doc = _ready_doc()
    data = _example_exchange("cedar_ridge.barn")
    builder.build(doc, data)
    assert bx.ROOF_IDENTITY in _managed_ids(doc)
    rep = builder.build(doc, data, report.BuildOptions(roof=False))
    assert bx.ROOF_IDENTITY not in _managed_ids(doc)
    assert rep.count(kind="roof") == 0


def test_fake_wall_delete_cascades_to_hosted_instances():
    # The fakes mirror Revit's host cascade: deleting a wall removes its hosted
    # door/window instances (the assumption the diff's fingerprint coupling
    # rests on; the real cascade still needs live-Revit validation).
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    hosted = next(e[1] for e in doc.created if e[0] == "instance"
                  and isinstance(e[2][2], revit_fakes.Wall))
    host = hosted.Host
    assert hosted.Id.Value in doc._by_id
    doc.Delete(host.Id)
    assert hosted.Id.Value not in doc._by_id


def test_changed_wall_type_override_recreates_walls():
    # Config changes must not be invisible to the diff: rebuilding with a
    # different (resolved) wall type / location line recreates the walls with
    # the new resources instead of keeping ones built the old way.
    doc = _ready_doc()
    doc.add_wall_type("Ext2", function=WallFunction.Exterior)
    data = _exchange(CEDAR)
    builder.build(doc, data)

    opts = report.BuildOptions(
        exterior_wall_type="Ext2", location_line="finish_face_exterior"
    )
    rep = builder.build(doc, data, opts)
    assert rep.count(status="kept", kind="wall") == 0
    assert rep.count(status="created", kind="wall") > 0
    # The rebuilt exterior walls really use the overridden type + location line.
    ext2_id = next(w.Id.Value for w in doc.wall_types if w.Name == "Ext2")
    from revit_fakes import BuiltInParameter as BIP

    walls = [e for e in doc._by_id.values()
             if isinstance(e, revit_fakes.Wall) and builder._is_managed(e)]
    ext2_walls = [w for w in walls
                  if getattr(w, "wtype_id", None) is not None and w.wtype_id.Value == ext2_id]
    assert ext2_walls
    assert any(w.get_Parameter(BIP.WALL_KEY_REF_PARAM).AsDouble() == 2 for w in ext2_walls)
    # Wall-independent elements (rooms) were kept; hosted openings recreated
    # with their walls.
    assert rep.count(status="kept", kind="room") == 3
    assert rep.count(status="kept", kind="door") == 0

    # Rebuilding again with the *same* options keeps everything.
    rep2 = builder.build(doc, data, opts)
    assert rep2.count(status="created", kind="wall") == 0
    assert rep2.count(status="kept", kind="wall") == rep.count(status="created", kind="wall")


#: CEDAR with the living room split in two — bed/bath are untouched (kept).
CEDAR_SPLIT = CEDAR.replace(
    "room living: living at 0,0 size 24 x 30",
    "room living: living at 0,0 size 24 x 20\nroom office: office at 0,20 size 24 x 10",
)


def test_inserted_room_gets_a_unique_number_and_kept_rooms_keep_theirs():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    from revit_fakes import BuiltInParameter as BIP

    before = {r.Id.Value: r.get_Parameter(BIP.ROOM_NUMBER).AsString()
              for r in doc.placed_rooms}
    kept_ids = {r.Id.Value for r in doc.placed_rooms
                if r.get_Parameter(BIP.ROOM_NAME).AsString().lower() in ("bed", "bath")}
    assert len(kept_ids) == 2

    builder.build(doc, _exchange(CEDAR_SPLIT))
    after = {r.Id.Value: r.get_Parameter(BIP.ROOM_NUMBER).AsString()
             for r in doc.placed_rooms}
    numbers = list(after.values())
    # No duplicate numbers, and the kept rooms still hold their originals.
    assert len(numbers) == len(set(numbers)) == 4
    for rid in kept_ids:
        assert after[rid] == before[rid]


def test_kept_ceilings_are_reported_when_the_ceiling_type_disappears():
    doc = _ready_doc()
    doc.add_ceiling_type("2x2 ACT")
    data = _exchange(CEDAR)
    rep1 = builder.build(doc, data)
    assert rep1.count(status="created", kind="ceiling") == 3
    doc.ceiling_types.clear()  # the type vanished from the project
    rep2 = builder.build(doc, data)
    # The ceilings themselves survive (the diff kept them) and are reported so.
    assert rep2.count(status="kept", kind="ceiling") == 3
    assert any("no ceiling type" in n for n in rep2.notes)
    kept_keys = [k for k in _managed_ids(doc) if k.startswith("ceiling|")]
    assert len(kept_keys) == 3


def test_document_artifacts_are_not_misreported_as_a_legacy_build():
    # document()'s dimensions/elevation markers are managed but never in a
    # build's incoming set — the rebuild purges them as ordinary stale
    # elements, not with the misleading "older barndsl build" note.
    doc = _ready_doc()
    doc.add_view_family_type("Floor Plan", revit_fakes.ViewFamily.FloorPlan)
    doc.add_view_family_type("Elevation", revit_fakes.ViewFamily.Elevation)
    data = _framed_exchange()
    builder.build(doc, data)
    builder.document(doc)
    assert any(isinstance(e, revit_fakes.Dimension) for e in doc._by_id.values())

    rep = builder.build(doc, data)
    assert not any("older barndsl build" in n for n in rep.notes)
    assert any("changed/removed" in n for n in rep.notes)
    # The stale doc artifacts were purged (re-document recreates them)...
    assert not any(isinstance(e, revit_fakes.Dimension) for e in doc._by_id.values())
    assert not any(isinstance(e, revit_fakes.ElevationMarker) for e in doc._by_id.values())
    # ...while the model itself was kept in place.
    assert rep.count(status="kept", kind="wall") > 0
    assert rep.count(status="created", kind="wall") == 0


def test_plan_ceiling_height_change_recreates_walls_and_ceilings_only():
    doc = _ready_doc()
    doc.add_ceiling_type("2x2 ACT")
    data_a = _exchange(CEDAR)  # ceiling 10
    builder.build(doc, data_a)
    ids1 = _managed_ids(doc)

    rep = builder.build(doc, _exchange(CEDAR.replace("ceiling 10", "ceiling 9")))
    ids2 = _managed_ids(doc)
    # Wall heights and ceiling planes moved: all recreated (openings ride along
    # via the host coupling; room records fold the plan ceiling in too).
    assert rep.count(status="kept", kind="wall") == 0
    assert rep.count(status="created", kind="wall") > 0
    assert rep.count(status="kept", kind="ceiling") == 0
    assert rep.count(status="created", kind="ceiling") == 3
    # Ceiling-independent elements keep their Revit ids.
    for prefix in ("slab|", "fixture|"):
        keys = [k for k in ids1 if k.startswith(prefix)]
        assert keys, "expected %s elements in the first build" % prefix
        for k in keys:
            assert ids2[k] == ids1[k]
    assert rep.count(status="kept", kind="fixture") > 0
    assert rep.count(status="kept", kind="slab") == 1


# --- door swing / hinge ------------------------------------------------------


SWING = """\
plan "Swing"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 24 x 30
room bed: bedroom at 24,0 size 16 x 30
door living - bed width 2.67 into %s hinge %s
entry living south width 3 offset 10
window bed east width 4 offset 4
"""


def _door_instance_at_x(doc, x):
    """The wall-hosted door instance placed at plan x == ``x``."""
    for e in doc.created:
        if e[0] != "instance":
            continue
        inst, args = e[1], e[2]
        sym = args[1] if len(args) > 1 else None
        if getattr(sym, "_category", None) != BIC.OST_Doors:
            continue
        if abs(args[0].X - x) < 1e-6:
            return inst
    return None


def test_door_flips_facing_into_the_named_room():
    # The shared wall runs north-south at x=24; the fake's default facing off it
    # is +X (east, toward bed). `into living` needs the leaf on the west side.
    doc = _ready_doc()
    rep = builder.build(doc, _exchange(SWING % ("living", "near")))
    inst = _door_instance_at_x(doc, 24.0)
    assert inst is not None
    assert inst.FacingFlipped is True
    assert inst.FacingOrientation.X == -1.0
    assert inst.HandFlipped is False  # hinge near = the family default
    recs = [r for r in rep.records if r.kind == "door" and "living" in r.message]
    assert recs and "flipped" in recs[0].message


def test_door_keeps_default_facing_when_it_already_swings_right():
    # `into bed` matches the fake's default facing (+X): no flip.
    doc = _ready_doc()
    builder.build(doc, _exchange(SWING % ("bed", "far")))
    inst = _door_instance_at_x(doc, 24.0)
    assert inst.FacingFlipped is False
    assert inst.FacingOrientation.X == 1.0
    # hinge far flips the hand.
    assert inst.HandFlipped is True


def test_door_without_swing_is_left_at_the_family_default():
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    for e in doc.created:
        if e[0] == "instance":
            assert e[1].FacingFlipped is False and e[1].HandFlipped is False


def test_egress_doors_are_stamped_in_comments():
    # Exterior egress doors get "barndsl egress" in Comments so a schedule can
    # filter them; interior doors (no egress concept) stay clean.
    doc = _ready_doc()
    builder.build(doc, _exchange(CEDAR))
    from revit_fakes import BuiltInParameter as BIP

    stamps = []
    for e in doc.created:
        if e[0] == "instance":
            sym = e[2][1] if len(e[2]) > 1 else None
            if getattr(sym, "_category", None) == BIC.OST_Doors:
                stamps.append(e[1].get_Parameter(BIP.ALL_MODEL_INSTANCE_COMMENTS).AsString())
    # CEDAR has one egress entry and two interior doors.
    assert stamps.count("barndsl egress") == 1
    assert stamps.count("") == 2


# --- project north (orientation) ---------------------------------------------


def test_orientation_rotates_true_north():
    import math

    doc = _ready_doc()
    src = CEDAR + "orientation 90\n"
    rep = builder.build(doc, _exchange(src))
    pos = doc.ActiveProjectLocation.GetProjectPosition(revit_fakes.XYZ.Zero)
    assert pos.Angle == pytest.approx(math.radians(90.0))
    assert rep.count(status="created", kind="project") == 1


def test_zero_orientation_leaves_project_north_alone():
    doc = _ready_doc()
    rep = builder.build(doc, _exchange(CEDAR))
    pos = doc.ActiveProjectLocation.GetProjectPosition(revit_fakes.XYZ.Zero)
    assert pos.Angle == 0.0
    assert rep.count(kind="project") == 0


# --- finish hints select types -------------------------------------------------


def test_siding_hint_picks_a_matching_exterior_wall_type():
    doc = _ready_doc()
    doc.add_wall_type("Exterior - Metal Panel", function=WallFunction.Exterior)
    src = CEDAR + 'finish siding "metal"\n'
    rep = builder.build(doc, _exchange(src))
    assert rep.resources["exterior_wall"] == "Exterior - Metal Panel"
    assert any("siding hint" in n for n in rep.notes)


def test_named_override_beats_the_siding_hint():
    doc = _ready_doc()
    doc.add_wall_type("Exterior - Metal Panel", function=WallFunction.Exterior)
    src = CEDAR + 'finish siding "metal"\n'
    opts = report.BuildOptions(exterior_wall_type="Ext")
    rep = builder.build(doc, _exchange(src), opts)
    assert rep.resources["exterior_wall"] == "Ext"


def test_unmatched_siding_hint_falls_back_to_the_auto_pick_with_a_note():
    doc = _ready_doc()  # no metal wall type
    src = CEDAR + 'finish siding "metal"\n'
    rep = builder.build(doc, _exchange(src))
    assert rep.resources["exterior_wall"] == "Ext"
    assert any("siding hint 'metal' matched no wall type" in n for n in rep.notes)


def test_roofing_hint_picks_a_matching_roof_type():
    doc = _ready_doc(roof=False)
    doc.add_roof_type("Asphalt Shingle")
    doc.add_roof_type("Standing Seam Metal")
    src = CEDAR + 'finish roof "standing-seam metal"\n'
    rep = builder.build(doc, _exchange(src))
    assert rep.resources["roof_type"] == "Standing Seam Metal"
    assert any("roofing hint" in n for n in rep.notes)


def test_named_roof_type_override_beats_the_roofing_hint():
    doc = _ready_doc(roof=False)
    doc.add_roof_type("Asphalt Shingle")
    doc.add_roof_type("Standing Seam Metal")
    src = CEDAR + 'finish roof "metal"\n'
    opts = report.BuildOptions(roof_type="Asphalt Shingle")
    rep = builder.build(doc, _exchange(src), opts)
    assert rep.resources["roof_type"] == "Asphalt Shingle"


# --- column / beam sections ----------------------------------------------------


def _section_symbols(doc, category):
    return [s for s in doc.symbols.get(category, []) if s.Name.startswith("barndsl")]


def test_columns_and_beams_are_sized_from_the_exchange():
    doc = _ready_doc(columns=True, framing=True)
    # Give the structural families settable section params (b/h, like timber
    # and steel families carry).
    for cat in (BIC.OST_StructuralColumns, BIC.OST_StructuralFraming):
        doc.symbols[cat][0].set_param("b", 0.0)
        doc.symbols[cat][0].set_param("h", 0.0)
    data = _framed_exchange()
    builder.build(doc, data)
    post_size = data["structure"]["columns"][0]["size"]
    assert post_size > 0
    # One duplicated "barndsl WxD" type per category, its section set.
    for cat in (BIC.OST_StructuralColumns, BIC.OST_StructuralFraming):
        sized = _section_symbols(doc, cat)
        assert len(sized) == 1
        assert sized[0].Name == "barndsl %.2fx%.2f" % (post_size, post_size)
        assert sized[0].LookupParameter("b").AsDouble() == pytest.approx(post_size)
        assert sized[0].LookupParameter("h").AsDouble() == pytest.approx(post_size)
    # Every column/beam instance was placed with the sized type.
    from revit_fakes import Structure

    for st in (Structure.StructuralType.Column, Structure.StructuralType.Beam):
        placed = _instances_of(doc, st)
        assert placed
        assert all(args[1].Name.startswith("barndsl") for _inst, args in placed)


def test_section_sizing_falls_back_to_the_default_type_with_a_note():
    # The stock fake families carry no b/h/Width/Depth params, so sizing can't
    # take: instances keep the default type and the report says why.
    doc = _ready_doc(columns=True, framing=True)
    rep = builder.build(doc, _framed_exchange())
    assert any("no settable section" in n for n in rep.notes)
    from revit_fakes import Structure

    cols = _instances_of(doc, Structure.StructuralType.Column)
    assert cols and all(not args[1].Name.startswith("barndsl") for _inst, args in cols)
    assert rep.count(status="created", kind="column") > 0


def test_section_sizing_disabled_with_size_families_off():
    doc = _ready_doc(columns=True, framing=True)
    for cat in (BIC.OST_StructuralColumns, BIC.OST_StructuralFraming):
        doc.symbols[cat][0].set_param("b", 0.0)
        doc.symbols[cat][0].set_param("h", 0.0)
    builder.build(doc, _framed_exchange(), report.BuildOptions(size_families=False))
    assert not _section_symbols(doc, BIC.OST_StructuralColumns)
    assert not _section_symbols(doc, BIC.OST_StructuralFraming)


# --- cased openings ------------------------------------------------------------


CASED = """\
plan "Cased"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 24 x 30
room dining: dining at 24,0 size 16 x 30
open living - dining width 6
entry living south width 3 offset 10
window dining east width 4 offset 4
"""


def test_cased_opening_builds_as_a_wall_cut_not_a_door():
    doc = _ready_doc()
    data = _exchange(CASED)
    cased = [o for o in data["openings"] if o["category"] == "cased_opening"]
    assert len(cased) == 1
    rep = builder.build(doc, data)
    assert rep.count(status="created", kind="opening") == 1
    # No leaf: the only door instance is the entry (at the exchange width 3).
    doors = [e for e in doc.created if e[0] == "instance"
             and getattr(e[2][1], "_category", None) == BIC.OST_Doors]
    assert len(doors) == 1
    # The cut is a rectangle: floor to the opening height, the opening wide.
    ops = [e for e in doc.created if e[0] == "opening"]
    assert len(ops) == 1
    _kind, op, (host, p1, p2) = ops[0]
    assert hasattr(host, "wtype_id")  # cut into the host wall
    width = abs(p2.X - p1.X) + abs(p2.Y - p1.Y)
    assert width == pytest.approx(cased[0]["width"])
    assert p1.Z == pytest.approx(0.0)
    assert p2.Z == pytest.approx(cased[0]["height"])


def test_cased_opening_falls_back_to_a_door_family_with_a_note():
    doc = _ready_doc()
    doc.fail_new_opening = True
    rep = builder.build(doc, _exchange(CASED))
    assert rep.count(status="created", kind="opening") == 0
    # Fallback: the cased opening lands as a sized door family (old behaviour),
    # plus the entry door → 2 door records, and a note explains the downgrade.
    assert rep.count(status="created", kind="door") == 2
    assert any("door-family fallback" in n for n in rep.notes)


# --- fixture orientation ---------------------------------------------------------


def test_fixtures_rotate_to_back_onto_their_wall():
    doc = _ready_doc()
    data = _example_exchange("cedar_ridge.barn")
    rep = builder.build(doc, data)
    import math

    expected = {"S": 0.0, "N": math.pi, "E": math.pi / 2.0, "W": -math.pi / 2.0}
    want_rotated = [fx for fx in data["fixtures"] if expected[fx["wall"]] != 0.0]
    assert want_rotated, "example should have fixtures on non-south walls"
    # One rotation per non-south fixture, at the wall's angle (S = the default).
    assert len(doc.rotations) == len(want_rotated)
    angles = sorted(a for _eid, a in doc.rotations)
    assert angles == sorted(expected[fx["wall"]] for fx in want_rotated)
    rotated_msgs = [r.message for r in rep.records
                    if r.kind == "fixture" and "rotated" in r.message]
    assert len(rotated_msgs) == len(want_rotated)


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


# --- declared wall kinds + opening kinds (review round 2 §2.1/§2.2 builder pass)


#: CEDAR with a declared plumbing wall and authored opening kinds.
CEDAR_KINDS = CEDAR.replace(
    "door living - bed width 2.67",
    "door living - bed double width 5\nwall living - bath plumbing",
).replace(
    "window bed east width 4 offset 4",
    "window bed east fixed width 4 offset 4",
)


def test_declared_wall_kind_picks_a_matching_wall_type():
    # A `wall a - b plumbing` segment lands on the wall type whose name reads
    # like the kind; undeclared interior walls keep the standard interior type.
    doc = _ready_doc()
    wet = doc.add_wall_type("Interior - Plumbing 2x6", function=WallFunction.Interior)
    rep = builder.build(doc, _exchange(CEDAR_KINDS))
    assert rep.resources["plumbing_wall"] == "Interior - Plumbing 2x6"
    walls = [e for e in doc._by_id.values()
             if isinstance(e, revit_fakes.Wall) and builder._is_managed(e)]
    wet_walls = [w for w in walls if w.wtype_id.Value == wet.Id.Value]
    assert len(wet_walls) == 1  # exactly the declared living|bath segment
    int_id = next(w.Id.Value for w in doc.wall_types if w.Name == "Int")
    assert any(w.wtype_id.Value == int_id for w in walls)  # the rest unchanged


def test_declared_wall_kind_override_wins_and_falls_back():
    # A named config override beats the name match; with neither, the interior
    # wall type stands in (the declaration still builds a wall).
    doc = _ready_doc()
    doc.add_wall_type("Interior - Plumbing 2x6", function=WallFunction.Interior)
    special = doc.add_wall_type("Wet Wall Special", function=WallFunction.Interior)
    opts = report.BuildOptions(plumbing_wall_type="Wet Wall Special")
    rep = builder.build(doc, _exchange(CEDAR_KINDS), opts)
    assert rep.resources["plumbing_wall"] == "Wet Wall Special"
    walls = [e for e in doc._by_id.values()
             if isinstance(e, revit_fakes.Wall) and builder._is_managed(e)]
    assert any(w.wtype_id.Value == special.Id.Value for w in walls)

    doc2 = _ready_doc()  # no plumbing-named type, no override
    rep2 = builder.build(doc2, _exchange(CEDAR_KINDS))
    assert rep2.resources["plumbing_wall"] == "Int"


def test_changed_kind_wall_type_recreates_only_the_declared_wall():
    # Folding the kind resolution into the wall context must be surgical: when
    # the plumbing mapping changes, the declared segment is recreated and the
    # rest of the model is kept.
    doc = _ready_doc()
    data = _exchange(CEDAR_KINDS)
    builder.build(doc, data)  # plumbing falls back to Int (no match)
    doc.add_wall_type("Interior - Plumbing 2x6", function=WallFunction.Interior)
    rep = builder.build(doc, data)
    assert rep.count(status="created", kind="wall") == 1
    assert rep.count(status="kept", kind="wall") > 0
    assert rep.count(status="kept", kind="room") == 3


def test_window_and_door_kinds_pick_matching_families():
    # An authored `fixed` window lands on the Fixed family; a `double` door
    # prefers a double-leaf family over the single-flush standard.
    doc = _ready_doc()  # window family is literally named "Fixed"
    doc.add_family(BIC.OST_Windows, "Casement Std")
    doc.add_family(BIC.OST_Doors, "Double-Glass")
    builder.build(doc, _exchange(CEDAR_KINDS))
    inst_syms = [e[2][1] for e in doc.created if e[0] == "instance"
                 and len(e[2]) >= 3 and isinstance(e[2][2], revit_fakes.Wall)]
    fams = {s.Family.Name for s in inst_syms}
    assert "Double-Glass" in fams  # the double living|bed door
    assert "Fixed" in fams  # the fixed bed window
    # The default-kind (casement) living window found the Casement family.
    assert "Casement Std" in fams


def test_unmatched_authored_kind_stands_in_with_a_note():
    # No double-leaf door family loaded: the standard family stands in, noted.
    doc = _ready_doc()
    rep = builder.build(doc, _exchange(CEDAR_KINDS))
    assert any("no door family reads 'double'" in n for n in rep.notes)
    # Default casement windows fall back *silently* (old plans, old behavior).
    assert not any("casement" in n.lower() for n in rep.notes)


def test_default_kinds_do_not_perturb_fingerprints():
    # A plan with only default-kind windows/doors fingerprints identically
    # whether or not the kind plumbing exists — rebuild keeps everything, and
    # the kind entries are omitted from the context when they resolve to the
    # standard picks.
    doc = _ready_doc()
    data = _exchange(CEDAR)
    builder.build(doc, data)
    rep = builder.build(doc, data)
    assert rep.count(status="created") == 0
    ctx = builder._resource_context(
        builder._resolve_resources(doc, report.BuildOptions(), report.BuildReport(), data),
        report.BuildOptions(),
    )
    # Default-kind resolutions land on the standard picks, so no
    # per-sub-kind context keys appear and every fingerprint matches the
    # pre-kind era.
    assert not any("/" in k for k in ctx)
