"""Cross-file composition (the ``use`` statement) — Phase 7a.

Fragment-mode legality, the sandboxed resolver, memoization, stamping, the two
diagnostic classes (part-internal vs instance) with dedupe + pragma interaction,
the new ``use`` edit kinds (incl. inline round-trip equivalence), emit verbatim +
flatten, fmt idempotence, the starter parts library, and the composed example.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from barndsl import compose
from barndsl.compiler import compile_file, compile_source
from barndsl.edits import apply_edit, edit_from_json
from barndsl.emit import emit_dsl, instance_lines
from barndsl.fmt import format_source
from barndsl.validation import Severity

REPO = Path(__file__).resolve().parent.parent
COMPOSED = REPO / "examples" / "composed"
PARTS = COMPOSED / "parts"


# --- helpers -----------------------------------------------------------------


def _write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _codes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


BATH = "room bath: bathroom at 0,0 size 8 x 6\n"


def _host(uses: str, extra: str = "") -> str:
    return (
        'plan "H"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 20 x 18\n"
        "entry living south width 3 offset 8\n" + uses + extra
    )


# --- fragment-mode legality (§3.1, §4) ---------------------------------------


HOST_ONLY = [
    'plan "P"', "envelope 20 x 20", "wing 10 x 10 at 0,0", "ceiling 9",
    "program 2 bed", "require adjacent a b", "site 100 x 100",
    "setback front 20", "building at 0,0", "street south", "orientation 90",
    "roof gable", "overhang 1", 'finish siding "metal"', "frame bay 12",
    "electrical", "stair s at 0,0 size 4 x 8 from 0 to 1",
]


@pytest.mark.parametrize("stmt", HOST_ONLY, ids=lambda s: s.split()[0])
def test_host_only_statement_rejected_in_a_part(stmt):
    r = compile_source(BATH + stmt + "\n", fragment=True)
    assert "PART_HOST_STMT" in _codes(r), r.summary()


def test_use_in_a_part_is_nested_error():
    r = compile_source(BATH + 'use "x.barn" as z at 0,0\n', fragment=True)
    assert "USE_NESTED" in _codes(r)


def test_part_with_no_rooms_is_empty():
    r = compile_source("# just a comment\n", fragment=True)
    assert "PART_EMPTY" in _codes(r)


def test_part_origin_normalized_with_info():
    r = compile_source("room bath: bathroom at 12,7 size 8 x 6\n", fragment=True)
    assert "PART_ORIGIN" in _codes(r)
    # normalized so the SW corner is the origin
    assert r.plan.room("bath").x == 0.0 and r.plan.room("bath").y == 0.0


def test_fragment_skips_whole_building_checks():
    # A lone bath in a fragment: no envelope, no entry, no bathroom-count nag.
    r = compile_source(BATH, fragment=True)
    assert not r.errors, r.report("part")
    assert "NO_ENTRY" not in _codes(r) and "ENVELOPE" not in _codes(r)


def test_fragment_runs_local_checks():
    # A 3x3 bath trips the local fixture/room clearance checks (placement-free).
    r = compile_source("room bath: bathroom at 0,0 size 3 x 3\n", fragment=True)
    assert "BATH_CLEARANCE" in _codes(r), r.report("part")


# --- the sandboxed resolver (§8) ---------------------------------------------


def test_resolver_missing_file(tmp_path):
    src = _host('use "parts/none.barn" as b at 20,0\n')
    r = compile_source(src, base_dir=str(tmp_path))
    assert "USE_UNRESOLVED" in _codes(r)


def test_resolver_absolute_path_rejected(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    abs_path = str(tmp_path / "parts" / "bath.barn")
    r = compile_source(_host(f'use "{abs_path}" as b at 20,0\n'), base_dir=str(tmp_path))
    assert "USE_UNRESOLVED" in _codes(r)


def test_resolver_dotdot_escape_rejected(tmp_path):
    root = tmp_path / "proj"
    _write(tmp_path, "secret.barn", BATH)
    root.mkdir(exist_ok=True)
    r = compile_source(_host('use "../secret.barn" as b at 20,0\n'), base_dir=str(root))
    assert "USE_UNRESOLVED" in _codes(r)


def test_resolver_symlink_escape_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = _write(tmp_path, "outside/bath.barn", BATH)
    link = root / "parts"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside.parent, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not constructible here")
    r = compile_source(_host('use "parts/bath.barn" as b at 20,0\n'), base_dir=str(root))
    assert "USE_UNRESOLVED" in _codes(r)


def test_resolver_no_base_dir_teaches(tmp_path):
    r = compile_source(_host('use "parts/bath.barn" as b at 20,0\n'), base_dir=None)
    diag = next(d for d in r.diagnostics if d.code == "USE_UNRESOLVED")
    assert "home directory" in diag.message or "serve" in (diag.hint or "")


def test_resolver_oversize_file_rejected(tmp_path):
    big = "room bath: bathroom at 0,0 size 8 x 6\n" + ("# pad\n" * 60000)
    assert len(big.encode()) > compose.MAX_PART_BYTES
    _write(tmp_path, "parts/big.barn", big)
    r = compile_source(_host('use "parts/big.barn" as b at 20,0\n'), base_dir=str(tmp_path))
    assert "USE_UNRESOLVED" in _codes(r)


# --- memoization (§4) --------------------------------------------------------


def test_part_compiled_once_for_many_uses(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    uses = "".join(f'use "parts/bath.barn" as b{i} at {20 + i},0\n' for i in range(5))
    compose.reset_fragment_compiles()
    compile_source(_host(uses), base_dir=str(tmp_path))
    assert compose.fragment_compiles() == 1


# --- stamping correctness (§4) -----------------------------------------------


def test_stamping_prefixes_ids_and_references(tmp_path):
    part = (
        "room bed: bedroom at 0,0 size 12 x 11\n"
        "room bath: bathroom east-of bed size 8 x 6\n"
        "door bed - bath width 2.5 into bath\n"
    )
    _write(tmp_path, "parts/suite.barn", part)
    r = compile_source(_host('use "parts/suite.barn" as m at 20,0\n'), base_dir=str(tmp_path))
    ids = {rm.id for rm in r.plan.rooms}
    assert {"m.bed", "m.bath"} <= ids
    door = next(d for d in r.plan.interior_doors if d.room_a == "m.bed")
    assert door.room_b == "m.bath" and door.swing_into == "m.bath"


def test_stamping_translates_and_lifts_level(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    r = compile_source(_host('use "parts/bath.barn" as b at 25,7 level 1\n'), base_dir=str(tmp_path))
    bath = r.plan.room("b.bath")
    assert (bath.x, bath.y, bath.level) == (25.0, 7.0, 1)


def test_host_door_resolves_a_stamped_id(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    src = _host('use "parts/bath.barn" as b at 20,0\n', "door living - b.bath width 2.5\n")
    r = compile_source(src, base_dir=str(tmp_path))
    assert "DOOR_NOADJ" not in _codes(r)  # they abut → the door resolves
    assert not r.errors, r.report("host")


def test_alias_dup_rejected(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    uses = 'use "parts/bath.barn" as b at 20,0\nuse "parts/bath.barn" as b at 20,10\n'
    r = compile_source(_host(uses), base_dir=str(tmp_path))
    assert "USE_ALIAS_DUP" in _codes(r)


def test_invalid_part_reports_and_is_not_stamped(tmp_path):
    _write(tmp_path, "parts/bad.barn",
           "room a: bathroom at 0,0 size 8 x 6\nroom a: office at 10,0 size 8 x 6\n")
    r = compile_source(_host('use "parts/bad.barn" as b at 20,0\n'), base_dir=str(tmp_path))
    assert "USE_PART_INVALID" in _codes(r)
    assert not r.plan.instances  # the broken part isn't stamped


def test_instance_cap(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    uses = "".join(
        f'use "parts/bath.barn" as b{i} at {i % 8 * 8},{i // 8 * 6 + 40}\n'
        for i in range(compose.MAX_INSTANCES + 3)
    )
    r = compile_source(_host(uses), base_dir=str(tmp_path))
    assert len(r.plan.instances) == compose.MAX_INSTANCES
    assert "USE_UNRESOLVED" in _codes(r)  # the overflow uses are refused


# --- the two diagnostic classes: dedupe + anchoring + pragmas (§4) -----------


def test_part_internal_reported_once_and_tagged(tmp_path):
    # A skinny room trips a geometric proportion nudge — placement-independent.
    part = "room den: office at 0,0 size 30 x 4\n"
    _write(tmp_path, "parts/skinny.barn", part)
    uses = 'use "parts/skinny.barn" as a at 20,0\nuse "parts/skinny.barn" as c at 20,20\n'
    r = compile_source(_host(uses), base_dir=str(tmp_path))
    props = [d for d in r.diagnostics if d.code == "ROOM_PROPORTION"]
    assert len(props) == 1, "a part-internal finding is reported once, not per use"
    assert props[0].part == "parts/skinny.barn" and props[0].file is not None
    assert props[0].message.startswith("in part parts/skinny.barn")


def test_instance_finding_anchored_to_use_line_with_alias(tmp_path):
    # An interior bedroom (no exterior wall) trips egress — placement-dependent.
    part = "room bed: bedroom at 0,0 size 11 x 11\n"
    _write(tmp_path, "parts/bed.barn", part)
    use_line = 6
    src = _host('use "parts/bed.barn" as z at 6,6\n')
    r = compile_source(src, base_dir=str(tmp_path))
    egress = [d for d in r.diagnostics if d.code == "BEDROOM_EGRESS"]
    assert egress and egress[0].line == use_line
    assert egress[0].message.startswith("instance z:")


def test_part_file_pragma_accepts_part_internal(tmp_path):
    part = (
        "# barndsl: accept ROOM_PROPORTION\n"
        "room den: office at 0,0 size 30 x 4\n"
    )
    _write(tmp_path, "parts/skinny.barn", part)
    r = compile_source(_host('use "parts/skinny.barn" as a at 20,0\n'), base_dir=str(tmp_path))
    props = [d for d in r.diagnostics if d.code == "ROOM_PROPORTION"]
    assert props and all(d.accepted for d in props)


def test_use_line_pragma_accepts_instance_finding(tmp_path):
    # An interior bath (no exterior window) trips BATH_VENT (an info) — a
    # placement-dependent instance finding accepted on the `use` line.
    _write(tmp_path, "parts/bath.barn", BATH)
    src = _host('use "parts/bath.barn" as z at 6,6   # barndsl: accept BATH_VENT\n')
    r = compile_source(src, base_dir=str(tmp_path))
    vent = [d for d in r.diagnostics if d.code == "BATH_VENT"]
    assert vent and all(d.accepted for d in vent)


# --- emit verbatim + flatten (§6/§7) -----------------------------------------


def test_emit_verbatim_keeps_use_lines_and_skips_stamps(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    r = compile_source(_host('use "parts/bath.barn" as b at 20,0\n'), base_dir=str(tmp_path))
    out = emit_dsl(r.plan)
    assert 'use "parts/bath.barn" as b at 20,0' in out
    assert "room b.bath" not in out  # the stamped room is described by the use


def test_emit_flatten_inlines_and_drops_use(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    src = _host('use "parts/bath.barn" as b at 20,0\n', "door living - b.bath width 2.5\n")
    r = compile_source(src, base_dir=str(tmp_path))
    flat = emit_dsl(r.plan, flatten=True)
    assert "use " not in flat and "room b.bath: bathroom at 20,0" in flat
    # recompiles (no base_dir needed) to the same composed rooms + errors
    again = compile_source(flat)
    assert {rm.id for rm in again.plan.rooms} == {rm.id for rm in r.plan.rooms}
    assert _codes(again) == _codes(r) - {"USE_UNRESOLVED"}


# --- fmt idempotence with use lines (§7) -------------------------------------


def test_fmt_normalizes_use_line_idempotently():
    src = 'USE  "parts/bath.barn"  as   b   at 20,0  level 1\n'
    f1 = format_source(src)
    assert f1 == 'use "parts/bath.barn" as b at 20,0 level 1\n'
    assert format_source(f1) == f1


# --- the new edit kinds (§6) -------------------------------------------------


@pytest.fixture()
def edit_env(tmp_path):
    _write(tmp_path, "parts/bath.barn", BATH)
    src = _host(
        'use "parts/bath.barn" as b1 at 20,0\nuse "parts/bath.barn" as b2 at 20,10\n'
    )
    return src, str(tmp_path)


def _apply(src, obj, base_dir):
    return apply_edit(src, edit_from_json(obj), base_dir=base_dir)


def test_add_use_appends_a_line(edit_env):
    src, bd = edit_env
    r = _apply(src, {"kind": "add_use", "relpath": "parts/bath.barn",
                     "alias": "b3", "x": 0, "y": 24}, bd)
    assert r.changed and 'as b3 at 0,24' in r.source


def test_move_use_rewrites_at(edit_env):
    src, bd = edit_env
    r = _apply(src, {"kind": "move_use", "alias": "b1", "x": 21, "y": 1}, bd)
    assert r.changed and 'as b1 at 21,1' in r.source


def test_set_use_level_only(edit_env):
    src, bd = edit_env
    r = _apply(src, {"kind": "set_use", "alias": "b1", "level": 1}, bd)
    assert r.changed and 'as b1 at 20,0 level 1' in r.source


def test_delete_use_removes_the_line(edit_env):
    src, bd = edit_env
    r = _apply(src, {"kind": "delete_use", "alias": "b2"}, bd)
    assert r.changed and "as b2 " not in r.source and "as b1 " in r.source


def test_member_edit_refused_with_teaching_error(edit_env):
    src, bd = edit_env
    r = _apply(src, {"kind": "move_room", "room": "b1.bath", "x": 5, "y": 5}, bd)
    assert r.error is not None and r.error.kind == "not_editable"
    assert "Inline" in r.error.message and "parts/bath.barn" in r.error.message


def test_inline_use_round_trips_to_identical_plan(edit_env):
    src, bd = edit_env
    before = compile_source(src, base_dir=bd)
    r = _apply(src, {"kind": "inline_use", "alias": "b1"}, bd)
    assert r.changed and "as b1 " not in r.source and "room b1.bath" in r.source
    after = compile_source(r.source, base_dir=bd)
    b = lambda p: sorted((rm.id, rm.x, rm.y, rm.width, rm.length) for rm in p.plan.rooms)
    assert b(before) == b(after)
    assert len(after.plan.instances) == len(before.plan.instances) - 1


def test_instance_lines_are_canonical(edit_env):
    src, bd = edit_env
    r = compile_source(src, base_dir=bd)
    inst = next(i for i in r.plan.instances if i.alias == "b1")
    lines = instance_lines(inst)
    assert any(line.startswith("room b1.bath:") for line in lines)


# --- the starter parts library + the composed example ------------------------


PART_FILES = sorted(PARTS.glob("*.barn"))


def test_parts_library_exists():
    assert {p.stem for p in PART_FILES} == {
        "bath_core", "master_suite", "kitchen_l", "laundry_core"
    }


@pytest.mark.parametrize("path", PART_FILES, ids=lambda p: p.stem)
def test_part_compiles_clean_in_fragment_mode(path):
    r = compile_source(path.read_text(encoding="utf-8"), fragment=True)
    active = [d for d in r.diagnostics if not d.accepted]
    assert not active, r.report(path.name)


def test_composed_example_is_pristine():
    r = compile_file(str(COMPOSED / "cedar_ridge.barn"))
    assert r.plan is not None, r.report("cedar_ridge")
    assert len(r.plan.instances) >= 3  # stamps 2+ parts (4 instances of 4 parts)
    active = [d for d in r.diagnostics if not d.accepted]
    for sev in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        assert not [d for d in active if d.severity is sev], r.report("cedar_ridge")


def test_playground_markup_has_instance_ui_and_stays_offline():
    from barndsl.playground import render_app

    html = render_app("plan \"X\"\nenvelope 20 x 20\nceiling 9\n")
    # instance panel group + inspector + the whole-instance drag machine
    for token in ("data-instance", "inst:", "moveuse", "inline_use", "move_use",
                  "add_use", "delete_use", "dupinst", "inlineinst", "ov-stamped",
                  "'move part'", "Instance —"):
        assert token in html, token
    # the offline guarantee holds — no external references
    assert "http://" not in html and "https://" not in html


def test_composed_example_flattens_and_recompiles_identically():
    r = compile_file(str(COMPOSED / "cedar_ridge.barn"))
    flat = emit_dsl(r.plan, flatten=True)
    again = compile_source(flat)  # no base_dir needed once flattened
    assert not again.errors, again.report("flat")
    b = lambda p: sorted((rm.id, rm.x, rm.y) for rm in p.plan.rooms)
    assert b(r) == b(again)
