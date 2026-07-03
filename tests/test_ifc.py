"""Tests for the IFC4 exporter (`barndsl.ifc`).

Two layers:

* **Structural tests (no dependencies)** parse the hand-written STEP/SPF output
  textually: the ISO-10303-21 header is well-formed; every ``#id`` referenced in
  the DATA section is defined; entity counts track the Revit-shaped exchange (one
  IfcWall per wall run, one IfcOpeningElement + IfcDoor/IfcWindow per hosted
  opening, one IfcSpace per room, one storey per level); GlobalIds are 22-char,
  unique, drawn from the IFC alphabet and deterministic across two exports; the
  imperial unit block is present; a plan title with quotes/apostrophes/unicode
  survives STEP escaping; and every bundled plan exports without error.
* **Oracle tests (skipif)** open the file with IfcOpenShell when it is installed,
  assert it parses as IFC4, walk the spatial tree and count products. IfcOpenShell
  is *not* an install dependency — only a validation oracle.
"""

from __future__ import annotations

import glob
import os
import re
import uuid

import pytest

from barndsl import compile_source
from barndsl.ifc import _GUID_ALPHABET, compress_guid, to_ifc
from barndsl.revit import to_revit_model

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

SIMPLE = """\
plan "Simple"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bedroom width 2.67
door living - bath width 2.67
entry living south width 3 offset 10
window bedroom east width 4 offset 4
window living west width 8 offset 10
"""


def _plan(src=SIMPLE):
    plan = compile_source(src).plan
    assert plan is not None
    return plan


def _example(rel):
    with open(os.path.join(EXAMPLES, rel), encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    assert plan is not None
    return plan


# --- a tiny SPF reader for the structural checks -----------------------------

_ENTITY = re.compile(r"^#(\d+)=([A-Z0-9_]+)\((.*)\);$")
_REF = re.compile(r"#(\d+)")


def _split_data(ifc: str) -> tuple[list[str], dict[int, str], str]:
    """Return (header_lines, {id: type}, data_body) for a serialised IFC file."""
    lines = ifc.splitlines()
    assert lines[0] == "ISO-10303-21;"
    assert lines[-1] == "END-ISO-10303-21;"
    data_start = lines.index("DATA;")
    data_end = lines.index("ENDSEC;", data_start)
    header = lines[: lines.index("ENDSEC;")]
    types: dict[int, str] = {}
    body_lines = lines[data_start + 1 : data_end]
    for ln in body_lines:
        m = _ENTITY.match(ln)
        assert m, f"malformed entity line: {ln!r}"
        types[int(m.group(1))] = m.group(2)
    return header, types, "\n".join(body_lines)


def _count(types: dict[int, str], name: str) -> int:
    return sum(1 for t in types.values() if t == name)


# --- header ------------------------------------------------------------------


def test_header_is_well_formed():
    ifc = to_ifc(_plan())
    header, _, _ = _split_data(ifc)
    text = "\n".join(header)
    assert "HEADER;" in text
    assert "FILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');" in text
    assert "FILE_SCHEMA(('IFC4'));" in text
    assert "FILE_NAME('Simple.ifc'," in text
    # No wall-clock timestamp leaks in (determinism): the fixed epoch is used.
    assert "1970-01-01T00:00:00" in text


def test_every_reference_resolves():
    ifc = to_ifc(_plan())
    _, types, body = _split_data(ifc)
    defined = set(types)
    for ln in body.splitlines():
        m = _ENTITY.match(ln)
        assert m
        for ref in _REF.findall(m.group(3)):
            assert int(ref) in defined, f"dangling reference #{ref} in {ln!r}"


# --- entity counts track the exchange ----------------------------------------


def test_entity_counts_match_exchange():
    plan = _plan()
    model = to_revit_model(plan)
    _, types, _ = _split_data(to_ifc(plan))

    assert _count(types, "IFCPROJECT") == 1
    assert _count(types, "IFCSITE") == 1
    assert _count(types, "IFCBUILDING") == 1
    assert _count(types, "IFCBUILDINGSTOREY") == len(model.levels)
    assert _count(types, "IFCWALL") == len(model.walls)
    assert _count(types, "IFCSPACE") == len(model.rooms)

    n_doors = sum(1 for o in model.openings if o.category != "window")
    n_windows = sum(1 for o in model.openings if o.category == "window")
    assert _count(types, "IFCDOOR") == n_doors
    assert _count(types, "IFCWINDOW") == n_windows
    # One opening element + one voids relation per hosted opening.
    hosted = [o for o in model.openings if o.host_wall is not None]
    assert _count(types, "IFCOPENINGELEMENT") == len(hosted)
    assert _count(types, "IFCRELVOIDSELEMENT") == len(hosted)
    assert _count(types, "IFCRELFILLSELEMENT") == len(hosted)


def test_frame_members_and_roof_present():
    plan = _example("frame_demo.barn")
    model = to_revit_model(plan)
    _, types, _ = _split_data(to_ifc(plan))
    assert _count(types, "IFCCOLUMN") == len(model.columns)
    assert _count(types, "IFCBEAM") == len(model.framing)
    assert _count(types, "IFCROOF") == 1


def test_multi_level_plan_has_a_storey_per_level():
    plan = _example("gallery/two_story.barn")
    model = to_revit_model(plan)
    _, types, _ = _split_data(to_ifc(plan))
    assert len(model.levels) >= 2
    assert _count(types, "IFCBUILDINGSTOREY") == len(model.levels)


def test_multi_level_walls_stay_one_per_run_but_gain_height_pieces():
    # The gap-band / wall-to-roof fix keeps every run one IfcWall (the count the
    # exchange pins), while a split run (covered part + uncovered part) carries
    # several box solids — so the extrusion count exceeds the wall count.
    from barndsl.wallheights import wall_top_intervals

    plan = _example("gallery/two_story.barn")
    model = to_revit_model(plan)
    _, types, _ = _split_data(to_ifc(plan))
    assert _count(types, "IFCWALL") == len(model.walls)
    # At least one lower run is split into covered/uncovered height pieces.
    assert any(
        w.level == 0 and len(wall_top_intervals(w, model)) > 1 for w in model.walls
    ), "expected a level-0 run split into multiple height pieces"


# --- GlobalIds ---------------------------------------------------------------


def _global_ids(ifc: str) -> list[str]:
    """Every IfcRoot GlobalId — the first string attribute of a rooted entity."""
    ids: list[str] = []
    for ln in ifc.splitlines():
        m = _ENTITY.match(ln)
        if not m:
            continue
        # Rooted entities take (GlobalId, OwnerHistory=#, ...); the GUID is the
        # leading 22-char quoted token when the second attribute is a reference.
        gm = re.match(r"'([^']{22})',#\d+,", m.group(3))
        if gm:
            ids.append(gm.group(1))
    return ids


def test_global_ids_are_valid_and_unique():
    ifc = to_ifc(_plan())
    ids = _global_ids(ifc)
    # project, site, building, storey, walls, openings, doors, windows, spaces...
    assert len(ids) >= 15
    alphabet = set(_GUID_ALPHABET)
    for gid in ids:
        assert len(gid) == 22
        assert set(gid) <= alphabet
        assert gid[0] in "0123"  # first char carries only 2 bits
    assert len(ids) == len(set(ids)), "GlobalIds must be unique"


def test_export_is_deterministic():
    a = to_ifc(_plan())
    b = to_ifc(_plan())
    assert a == b


def test_guid_compression_roundtrips_and_matches_length():
    u = uuid.UUID("4e8e5f24-3f6a-4b7c-8d9e-0a1b2c3d4e5f")
    g = compress_guid(u)
    assert len(g) == 22
    assert set(g) <= set(_GUID_ALPHABET)

    def decompress(s: str) -> uuid.UUID:
        num = 0
        for i, ch in enumerate(s):
            num = (num << (2 if i == 0 else 6)) | _GUID_ALPHABET.index(ch)
        return uuid.UUID(int=num)

    assert decompress(g) == u


# --- units -------------------------------------------------------------------


def test_units_declare_imperial_feet():
    ifc = to_ifc(_plan())
    _, types, body = _split_data(ifc)
    assert _count(types, "IFCUNITASSIGNMENT") == 1
    assert "IFCCONVERSIONBASEDUNIT" in body
    assert "'foot'" in body
    assert "IFCLENGTHMEASURE(0.3048)" in body
    assert "'square foot'" in body


# --- string escaping ---------------------------------------------------------


def test_tricky_plan_name_survives_escaping():
    # An apostrophe, a double quote and non-ASCII (a smart quote + café + emoji).
    # Set it on the plan directly — the DSL string grammar can't carry a literal
    # double quote, but the IFC writer must escape whatever name it is handed.
    name = "O'Brien's \"Café\" barn — 100² \U0001f3e0"
    plan = _plan()
    plan.name = name
    ifc = to_ifc(plan)
    header, types, _ = _split_data(ifc)
    # The apostrophe is doubled per STEP; the whole file still parses cleanly.
    htext = "\n".join(header)
    assert "O''Brien''s" in htext
    # Non-ASCII is encoded with the \X2\...\X0\ control directive.
    assert "\\X2\\" in htext
    assert _count(types, "IFCPROJECT") == 1


# --- gallery sweep -----------------------------------------------------------


def test_gallery_sweep_exports_every_plan():
    files = sorted(
        glob.glob(os.path.join(EXAMPLES, "*.barn"))
        + glob.glob(os.path.join(EXAMPLES, "gallery", "*.barn"))
    )
    assert files
    for path in files:
        with open(path, encoding="utf-8") as fh:
            plan = compile_source(fh.read()).plan
        assert plan is not None, path
        ifc = to_ifc(plan)
        _, types, body = _split_data(ifc)  # header well-formed + parseable
        # Every reference resolves for every plan.
        defined = set(types)
        for ref in _REF.findall(body):
            assert int(ref) in defined, path


# --- CLI wiring --------------------------------------------------------------


def test_cli_ifc_writes_file(tmp_path, capsys):
    from barndsl.cli import main

    out = tmp_path / "plan.ifc"
    rc = main(["ifc", os.path.join(EXAMPLES, "cedar_ridge.barn"), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("ISO-10303-21;")
    assert "IFC4:" in capsys.readouterr().out


def test_cli_ifc_default_out_is_file_stem(tmp_path, monkeypatch):
    from barndsl.cli import main

    src = tmp_path / "myhouse.barn"
    src.write_text(SIMPLE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = main(["ifc", str(src)])
    assert rc == 0
    assert (tmp_path / "myhouse.ifc").exists()


# --- IfcOpenShell oracle (skipped when the optional dep is absent) ------------
#
# The structural tests above run everywhere (stdlib only). These extra tests open
# the file with IfcOpenShell as a validation oracle; they are skipped — not
# failed — when it is not installed, so CI/users with `pip install ifcopenshell`
# get the coverage without it becoming an install dependency.

try:  # pragma: no cover - import guard
    import ifcopenshell as _ifc

    _HAVE_IFC = True
except Exception:  # pragma: no cover - exercised only without the optional dep
    _ifc = None
    _HAVE_IFC = False

_needs_oracle = pytest.mark.skipif(not _HAVE_IFC, reason="ifcopenshell not installed")


def _open(plan, tmp_path):
    path = tmp_path / "oracle.ifc"
    path.write_text(to_ifc(plan), encoding="utf-8")
    return _ifc.open(str(path))


@_needs_oracle
def test_oracle_parses_as_ifc4(tmp_path):
    f = _open(_plan(), tmp_path)
    assert f.schema == "IFC4"


@_needs_oracle
def test_oracle_spatial_tree_and_counts(tmp_path):
    plan = _plan()
    model = to_revit_model(plan)
    f = _open(plan, tmp_path)

    project = f.by_type("IfcProject")[0]
    site = f.by_type("IfcSite")[0]
    building = f.by_type("IfcBuilding")[0]
    # project → site → building → storeys, walked via the aggregation inverses.
    assert site in [r.RelatedObjects[0] for r in project.IsDecomposedBy]
    assert building in [r.RelatedObjects[0] for r in site.IsDecomposedBy]
    storeys = [o for r in building.IsDecomposedBy for o in r.RelatedObjects]
    assert len(storeys) == len(model.levels)

    assert len(f.by_type("IfcWall")) == len(model.walls)
    assert len(f.by_type("IfcSpace")) == len(model.rooms)
    assert len(f.by_type("IfcDoor")) == sum(1 for o in model.openings if o.category != "window")
    assert len(f.by_type("IfcWindow")) == sum(1 for o in model.openings if o.category == "window")

    # Door/window OverallWidth carries the real opening width.
    for door in f.by_type("IfcDoor"):
        assert door.OverallWidth and door.OverallWidth > 0
        assert door.OverallHeight and door.OverallHeight > 0


@_needs_oracle
def test_oracle_units_are_feet(tmp_path):
    f = _open(_plan(), tmp_path)
    units = {u.Name for u in f.by_type("IfcProject")[0].UnitsInContext.Units if hasattr(u, "Name")}
    assert "foot" in units


@_needs_oracle
def test_oracle_two_story_walls_close_gap_and_reach_roof(tmp_path):
    # Tessellate the two-storey model and read each IfcWall's world-space z-extent.
    # The lower walls must no longer stop at the ceiling (9): covered runs reach
    # the level-1 base (10) and uncovered exterior runs reach the roof plate (19),
    # so the inter-floor gap band and the wall-to-roof void are both gone.
    import ifcopenshell.geom as geom

    plan = _example("gallery/two_story.barn")
    path = tmp_path / "two.ifc"
    path.write_text(to_ifc(plan), encoding="utf-8")
    f = _ifc.open(str(path))
    settings = geom.settings()

    ft = 0.3048  # ifcopenshell tessellates in SI metres; the model declares feet
    exterior_tops: list[float] = []
    for wall in f.by_type("IfcWall"):
        shape = geom.create_shape(settings, wall)
        base_z = list(shape.transformation.matrix)[14]  # storey elevation, metres
        top = base_z + max(shape.geometry.verts[2::3])
        if wall.Name == "Exterior Wall":
            exterior_tops.append(top)

    # Every exterior run reaches at least the level-1 base (10 ft) — no gap band —
    # and the uncovered ones reach the roof plate (19 ft) — no wall-to-roof void.
    assert min(exterior_tops) >= 10.0 * ft - 1e-3
    assert max(exterior_tops) >= 19.0 * ft - 1e-3


@_needs_oracle
def test_oracle_geometry_tessellates(tmp_path):
    import ifcopenshell.geom as geom

    plan = _example("frame_demo.barn")
    path = tmp_path / "geom.ifc"
    path.write_text(to_ifc(plan), encoding="utf-8")
    f = _ifc.open(str(path))
    settings = geom.settings()
    it = geom.iterator(settings, f)
    shapes = 0
    assert it.initialize()
    while True:
        assert len(it.get().geometry.verts) > 0
        shapes += 1
        if not it.next():
            break
    assert shapes > 0
