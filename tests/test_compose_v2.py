"""Cross-file composition v2 (Phase 20) — parametric parts, nested `use` (depth 2),
multi-level parts, and the sandbox guarantees under nesting.

Covers: param grammar/defaults/override/unknown/undeclared/did-you-mean, ft-in param
values, memo-vs-params correctness, emit fixpoints (part params, host `with`, nested,
multi-level), depth-2 ok / depth-3 USE_NESTED, cycle + self-cycle, the instance cap
across depths, transform-composition oracle, level offsets seen by validation, the
nested sandbox-escape attempts, playground read-only nested members, and the
`use ... with ` LSP completion.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from barndsl import compose
from barndsl.compiler import compile_file, compile_source
from barndsl.elements import Direction
from barndsl.emit import emit_dsl


def _write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _codes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


def _host(uses: str, extra: str = "", envelope: str = "48 x 40") -> str:
    return (
        f'plan "H"\nenvelope {envelope}\nceiling 9\n'
        "room living: living at 0,0 size 20 x 18\n"
        "entry living south width 3 offset 8\n" + uses + extra
    )


# --- 1. parametric parts: grammar / defaults / override ----------------------


def test_param_default_used_when_not_passed(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nparam depth = 6\nroom bath: bathroom at 0,0 size width x depth\n")
    r = compile_source(_host('use "parts/bath.barn" as b at 20,0\n'), base_dir=str(tmp_path))
    bath = r.plan.room("b.bath")
    assert (bath.width, bath.length) == (8.0, 6.0)


def test_param_override_from_with_clause(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nparam depth = 6\nroom bath: bathroom at 0,0 size width x depth\n")
    r = compile_source(
        _host('use "parts/bath.barn" as b at 20,0 with width=10, depth=7\n'),
        base_dir=str(tmp_path))
    bath = r.plan.room("b.bath")
    assert (bath.width, bath.length) == (10.0, 7.0)


def test_param_ft_in_value(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param depth = 6\nroom bath: bathroom at 0,0 size 8 x depth\n")
    r = compile_source(
        _host('use "parts/bath.barn" as b at 20,0 with depth=7-6\n'), base_dir=str(tmp_path))
    assert r.plan.room("b.bath").length == 7.5


def test_param_ft_in_default(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param depth = 7-6\nroom bath: bathroom at 0,0 size 8 x depth\n")
    r = compile_source(_host('use "parts/bath.barn" as b at 20,0\n'), base_dir=str(tmp_path))
    assert r.plan.room("b.bath").length == 7.5


def test_param_unknown_name_is_error_with_did_you_mean(tmp_path):
    # `widht` is a typo for the declared `width` → PARAM_UNKNOWN + did-you-mean.
    r = compile_source(
        "param width = 8\nroom bath: bathroom at 0,0 size widht x 6\n", fragment=True)
    diag = next(d for d in r.diagnostics if d.code == "PARAM_UNKNOWN")
    assert "width" in diag.message  # the did-you-mean names the real param


def test_param_undeclared_in_with_anchored_to_use_line(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nroom bath: bathroom at 0,0 size width x 6\n")
    src = _host('use "parts/bath.barn" as b at 20,0 with height=9\n')
    r = compile_source(src, base_dir=str(tmp_path))
    diag = next(d for d in r.diagnostics if d.code == "PARAM_UNDECLARED")
    use_line = next(i for i, ln in enumerate(src.splitlines(), 1) if ln.startswith("use"))
    assert diag.line == use_line and "height" in diag.message


def test_param_in_a_plan_is_rejected():
    r = compile_source('plan "P"\nenvelope 20 x 20\nparam width = 8\n')
    assert "PARAM_IN_PLAN" in _codes(r)


def test_param_declared_twice_is_error():
    r = compile_source("param w = 8\nparam w = 9\nroom a: office at 0,0 size w x 8\n",
                       fragment=True)
    assert "PARAM_DUP" in _codes(r)


def test_with_same_param_twice_is_error(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param w = 8\nroom bath: bathroom at 0,0 size w x 6\n")
    r = compile_source(_host('use "parts/bath.barn" as b at 20,0 with w=8, w=9\n'),
                       base_dir=str(tmp_path))
    assert "PARAM_DUP" in _codes(r)


# --- 2. memoization keyed on params ------------------------------------------


def test_two_instances_different_params_differ_and_both_compile(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nroom bath: bathroom at 0,0 size width x 6\n")
    uses = ('use "parts/bath.barn" as a at 20,0 with width=8\n'
            'use "parts/bath.barn" as c at 20,10 with width=12\n')
    compose.reset_fragment_compiles()
    r = compile_source(_host(uses), base_dir=str(tmp_path))
    assert r.plan.room("a.bath").width == 8.0
    assert r.plan.room("c.bath").width == 12.0
    # Different param values → two distinct compiles (memo keyed on params).
    assert compose.fragment_compiles() == 2


def test_two_instances_same_params_compile_once(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nroom bath: bathroom at 0,0 size width x 6\n")
    uses = ('use "parts/bath.barn" as a at 20,0 with width=9\n'
            'use "parts/bath.barn" as c at 20,10 with width=9\n')
    compose.reset_fragment_compiles()
    compile_source(_host(uses), base_dir=str(tmp_path))
    assert compose.fragment_compiles() == 1


# --- 3. emit fixpoints -------------------------------------------------------


def _emit_fixpoint(text, base_dir, fragment=False):
    r = compile_source(text, base_dir=base_dir, fragment=fragment)
    e1 = emit_dsl(r.plan, fragment=fragment)
    r2 = compile_source(e1, base_dir=base_dir, fragment=fragment)
    e2 = emit_dsl(r2.plan, fragment=fragment)
    return e1, e2


def test_emit_host_with_clause_fixpoint(tmp_path):
    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nparam depth = 6\nroom bath: bathroom at 0,0 size width x depth\n")
    src = _host('use "parts/bath.barn" as b at 20,0 with width=10, depth=7-6\n')
    e1, e2 = _emit_fixpoint(src, str(tmp_path))
    assert e1 == e2
    assert "with width=10, depth=7.5" in e1


def test_emit_part_with_params_fixpoint(tmp_path):
    part = "param width = 8\nparam depth = 6\nroom bath: bathroom at 0,0 size width x depth\n"
    e1, e2 = _emit_fixpoint(part, str(tmp_path), fragment=True)
    assert e1 == e2
    assert "param width = 8" in e1 and "param depth = 6" in e1


def test_emit_nested_and_multilevel_fixpoint(tmp_path):
    _write(tmp_path, "parts/inner.barn",
           "param w = 8\nroom bath: bathroom at 0,0 size w x 6\n")
    _write(tmp_path, "parts/mid.barn",
           'room bed: bedroom at 0,0 size 12 x 12\nuse "inner.barn" as i at 12,0 with w=8\n')
    _write(tmp_path, "parts/tower.barn",
           "room shop: shop at 0,0 size 20 x 20\n"
           "room loft: loft at 0,0 size 20 x 12 level 1\n"
           "stair st at 15,0 size 4 x 12 from 0 to 1\n")
    src = _host('use "parts/mid.barn" as m at 20,0\n'
                'use "parts/tower.barn" as t at 0,20\n', envelope="60 x 44")
    e1, e2 = _emit_fixpoint(src, str(tmp_path))
    assert e1 == e2


# --- 4. nested use: depth, cycle -------------------------------------------


def _nest_env(tmp_path):
    _write(tmp_path, "parts/inner.barn", "room bath: bathroom at 0,0 size 8 x 6\n")
    _write(tmp_path, "parts/outer.barn",
           'room bed: bedroom at 0,0 size 12 x 12\nuse "inner.barn" as i at 12,0\n')
    return _host('use "parts/outer.barn" as m at 20,0\n')


def test_nested_depth_2_composes_and_prefixes_compose(tmp_path):
    src = _nest_env(tmp_path)
    p = _write(tmp_path, "host.barn", src)
    r = compile_file(str(p))
    ids = {rm.id for rm in r.plan.rooms}
    assert "m.bed" in ids and "m.i.bath" in ids  # two-level prefix composes


def test_depth_3_is_use_nested(tmp_path):
    _write(tmp_path, "parts/d3.barn", "room r3: office at 0,0 size 10 x 10\n")
    _write(tmp_path, "parts/d2.barn",
           'room r2: office at 0,0 size 10 x 10\nuse "d3.barn" as n at 10,0\n')
    _write(tmp_path, "parts/d1.barn",
           'room r1: office at 0,0 size 10 x 10\nuse "d2.barn" as n at 10,0\n')
    p = _write(tmp_path, "host.barn", _host('use "parts/d1.barn" as x at 24,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_NESTED" in _codes(r)
    assert any("deeper than 2" in d.message for d in r.diagnostics if d.code == "USE_NESTED")


def test_mutual_cycle_is_clean_use_cycle(tmp_path):
    _write(tmp_path, "parts/a.barn",
           'room ra: office at 0,0 size 10 x 10\nuse "b.barn" as bb at 10,0\n')
    _write(tmp_path, "parts/b.barn",
           'room rb: office at 0,0 size 10 x 10\nuse "a.barn" as aa at 10,0\n')
    p = _write(tmp_path, "host.barn", _host('use "parts/a.barn" as x at 24,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_CYCLE" in _codes(r)
    cyc = next(d for d in r.diagnostics if d.code == "USE_CYCLE")
    assert "a.barn" in cyc.message and "b.barn" in cyc.message


def test_self_cycle_is_clean_use_cycle(tmp_path):
    _write(tmp_path, "parts/self.barn",
           'room r: office at 0,0 size 10 x 10\nuse "self.barn" as s at 10,0\n')
    p = _write(tmp_path, "host.barn", _host('use "parts/self.barn" as x at 24,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_CYCLE" in _codes(r)


def test_instance_cap_is_global_across_depths(tmp_path):
    # The cap counts every `use` executed at every depth against ONE shared budget.
    # A branch part stamps 3 nested leaves (counted once — memoized); the host then
    # stamps the branch enough times that the nested + host stamps together cross 64.
    _write(tmp_path, "parts/leaf.barn", "room r: office at 0,0 size 4 x 4\n")
    inner = "".join(f'use "leaf.barn" as l{i} at {6 + i * 6},0\n' for i in range(3))
    _write(tmp_path, "parts/branch.barn", "room b: office at 0,0 size 4 x 4\n" + inner)
    uses = "".join(f'use "parts/branch.barn" as br{i} at 0,{40 + i * 5}\n' for i in range(70))
    r = compile_source(_host(uses, envelope="80 x 500"), base_dir=str(tmp_path))
    # The 3 nested leaf stamps come out of the same budget as the host stamps, so
    # fewer than 64 host instances land (61 = 64 − 3) and the overflow is refused.
    assert len(r.plan.instances) < compose.MAX_INSTANCES
    assert len(r.plan.instances) == compose.MAX_INSTANCES - 3
    assert "USE_UNRESOLVED" in _codes(r)  # the cap message reuses the resolver code


# --- 5. transform composition oracle ----------------------------------------
#
# A window's wall remaps through each transform in turn (inner first, then outer).
# The wall tables (§5) are: rotate 90 ccw S→E,E→N,N→W,W→S; mirror y E↔W (N/S fixed).
# So for an inner window on SOUTH:
#   rotate-INSIDE-mirror: inner rotate 90 (S→E), then outer mirror y (E→W) → WEST.
#   mirror-INSIDE-rotate: inner mirror y (S→S),  then outer rotate 90 (S→E) → EAST.
# These differ (composition is non-commutative), and both are hand-derived.


def _nested_window_wall(tmp_path, inner_xform: str, outer_xform: str) -> Direction:
    _write(tmp_path, "parts/pt.barn",
           "room r: office at 0,0 size 6 x 2\nwindow r south width 2 offset 1\n")
    _write(tmp_path, "parts/mid.barn", f'use "pt.barn" as i at 0,0 {inner_xform}\n')
    p = _write(tmp_path, "host.barn",
               _host(f'use "parts/mid.barn" as o at 20,0 {outer_xform}\n', envelope="60 x 40"))
    r = compile_file(str(p))
    win = next(w for w in r.plan.windows if w.room == "o.i.r")
    return win.wall


def test_transform_oracle_rotate_inside_mirror(tmp_path):
    # inner rotate 90 (S→E), outer mirror y (E→W) → WEST
    assert _nested_window_wall(tmp_path, "rotate 90", "mirror y") is Direction.WEST


def test_transform_oracle_mirror_inside_rotate(tmp_path):
    # inner mirror y (S→S), outer rotate 90 (S→E) → EAST
    assert _nested_window_wall(tmp_path, "mirror y", "rotate 90") is Direction.EAST


def test_nested_transform_swaps_dims(tmp_path):
    # A 6x2 inner part, rotated 90 by the middle then mirrored y by the host, is a
    # 2x6 block (rotate swaps w/l; mirror preserves them).
    _write(tmp_path, "parts/pt.barn", "room r: office at 0,0 size 6 x 2\n")
    _write(tmp_path, "parts/mid.barn", 'use "pt.barn" as i at 0,0 rotate 90\n')
    p = _write(tmp_path, "host.barn",
               _host('use "parts/mid.barn" as o at 20,0 mirror y\n', envelope="60 x 40"))
    r = compile_file(str(p))
    rm = r.plan.room("o.i.r")
    assert (rm.width, rm.length) == (2.0, 6.0)


# --- 6. multi-level parts ----------------------------------------------------


def test_part_level1_room_offsets_by_instance_level(tmp_path):
    _write(tmp_path, "parts/tower.barn",
           "room base: office at 0,0 size 12 x 12\n"
           "room up: loft at 0,0 size 12 x 12 level 1\n")
    p = _write(tmp_path, "host.barn",
               _host('use "parts/tower.barn" as t at 20,0 level 1\n', envelope="40 x 40"))
    r = compile_file(str(p))
    assert r.plan.room("t.base").level == 1  # 0 + instance level 1
    assert r.plan.room("t.up").level == 2    # 1 + instance level 1


def test_cross_level_rule_sees_final_levels(tmp_path):
    # A part carrying a level-1 loft + a stair: the per-storey smoke-alarm rule
    # (a cross-level, whole-building check) fires on the composed plan because the
    # loft lands on a real upper level with no alarm.
    _write(tmp_path, "parts/tower.barn",
           "room shop: shop at 0,0 size 20 x 20\n"
           "room loft: loft at 0,0 size 20 x 12 level 1\n"
           "stair st at 15,0 size 4 x 13 from 0 to 1\n")
    p = _write(tmp_path, "host.barn",
               _host('use "parts/tower.barn" as t at 20,0\nalarm smoke in living\n',
                     envelope="44 x 40"))
    r = compile_file(str(p))
    # ALARM_LEVEL is a whole-building rule that only exists once levels compose.
    assert "ALARM_LEVEL" in _codes(r)


def test_stair_in_a_part_stamps_and_offsets(tmp_path):
    _write(tmp_path, "parts/tower.barn",
           "room shop: shop at 0,0 size 20 x 20\n"
           "room loft: loft at 0,0 size 20 x 12 level 1\n"
           "stair st at 15,0 size 4 x 13 from 0 to 1\n")
    p = _write(tmp_path, "host.barn",
               _host('use "parts/tower.barn" as t at 20,0 level 1\n', envelope="44 x 40"))
    r = compile_file(str(p))
    st = next(s for s in r.plan.stairs if s.id == "t.st")
    assert (st.from_level, st.to_level) == (1, 2)


# --- 7. sandbox guarantees under nesting (SECURITY) --------------------------


def test_nested_dotdot_escape_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _write(tmp_path, "secret.barn", "room s: office at 0,0 size 8 x 6\n")
    _write(root, "parts/inner.barn", "room r: office at 0,0 size 8 x 6\n")
    # outer resolves fine; its NESTED use escapes the host root via `..`.
    _write(root, "parts/outer.barn",
           'room o: office at 0,0 size 8 x 6\nuse "../../secret.barn" as e at 10,0\n')
    p = _write(root, "host.barn", _host('use "parts/outer.barn" as m at 20,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_UNRESOLVED" in _codes(r)
    assert not any(rm.id.startswith("m.e") for rm in r.plan.rooms)


def test_nested_absolute_path_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = _write(root, "parts/leaf.barn", "room r: office at 0,0 size 8 x 6\n")
    _write(root, "parts/outer.barn",
           f'room o: office at 0,0 size 8 x 6\nuse "{target}" as e at 10,0\n')
    p = _write(root, "host.barn", _host('use "parts/outer.barn" as m at 20,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_UNRESOLVED" in _codes(r)


def test_nested_symlink_dir_escape_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = _write(tmp_path, "outside/evil.barn", "room r: office at 0,0 size 8 x 6\n")
    link = root / "parts" / "linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside.parent, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not constructible here")
    _write(root, "parts/outer.barn",
           'room o: office at 0,0 size 8 x 6\nuse "linked/evil.barn" as e at 10,0\n')
    p = _write(root, "host.barn", _host('use "parts/outer.barn" as m at 20,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_UNRESOLVED" in _codes(r)
    assert not any(rm.id.startswith("m.e") for rm in r.plan.rooms)


def test_nested_sibling_outside_root_via_relative_rejected(tmp_path):
    # The host root is proj/; a part in proj/parts reaches a sibling dir OUTSIDE
    # proj through a relative path — must be an escape, not a read outside root.
    root = tmp_path / "proj"
    root.mkdir()
    _write(tmp_path, "sibling/leaf.barn", "room r: office at 0,0 size 8 x 6\n")
    _write(root, "parts/outer.barn",
           'room o: office at 0,0 size 8 x 6\nuse "../../sibling/leaf.barn" as e at 10,0\n')
    p = _write(root, "host.barn", _host('use "parts/outer.barn" as m at 20,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_UNRESOLVED" in _codes(r)


def test_nested_use_resolves_relative_to_using_part_dir(tmp_path):
    # A nested `use` resolves relative to the USING part's dir, not the host's — a
    # part in parts/lib can reach a sibling in parts/lib by bare name.
    _write(tmp_path, "parts/lib/leaf.barn", "room r: office at 0,0 size 8 x 6\n")
    _write(tmp_path, "parts/lib/outer.barn",
           'room o: office at 0,0 size 8 x 6\nuse "leaf.barn" as e at 10,0\n')
    p = _write(tmp_path, "host.barn",
               _host('use "parts/lib/outer.barn" as m at 20,0\n', envelope="60 x 30"))
    r = compile_file(str(p))
    assert "USE_UNRESOLVED" not in _codes(r)
    assert any(rm.id == "m.e.r" for rm in r.plan.rooms)


# --- 8. playground read-only nested members ---------------------------------


def test_nested_stamped_members_are_read_only(tmp_path):
    src = _nest_env(tmp_path)
    p = _write(tmp_path, "host.barn", src)
    r = compile_file(str(p))
    # Every stamped room — including the deeply nested one — is a read-only member.
    assert "m.i.bath" in r.plan.stamped_rooms
    assert "m.bed" in r.plan.stamped_rooms


# --- 9. LSP: with-clause param completion ------------------------------------


def test_lsp_completes_part_params_after_with(tmp_path):
    from barndsl.lsp import completions

    _write(tmp_path, "parts/bath.barn",
           "param width = 8\nparam depth = 6\nroom bath: bathroom at 0,0 size width x depth\n")
    line = 'use "parts/bath.barn" as b at 0,0 with '
    text = line
    items = completions(text, 0, len(line), compile_source(text, base_dir=str(tmp_path)),
                        str(tmp_path))
    labels = {it["label"] for it in items}
    assert "width=" in labels and "depth=" in labels


def test_scan_part_params_is_sandboxed(tmp_path):
    _write(tmp_path, "parts/bath.barn", "param w = 8\nroom bath: bathroom at 0,0 size w x 6\n")
    assert compose.scan_part_params(str(tmp_path), "parts/bath.barn") == ["w"]
    # An escaping path yields nothing (never reads outside the root).
    assert compose.scan_part_params(str(tmp_path / "proj"), "../parts/bath.barn") == []


# --- 10. the shipped v2 showcase ---------------------------------------------

REPO = Path(__file__).resolve().parent.parent


def test_v2_showcase_compiles_clean():
    r = compile_file(str(REPO / "examples" / "composed" / "cedar_ridge_v2.barn"))
    errs = [d for d in r.diagnostics if d.severity.name in ("ERROR", "WARNING")]
    assert not errs, [(d.code, d.message) for d in errs]
    ids = {rm.id for rm in r.plan.rooms}
    assert {"fb.bath", "g.bed", "g.gb.bath", "sl.shop", "sl.loft"} <= ids


def test_v2_showcase_flatten_recompiles_identically():
    r = compile_file(str(REPO / "examples" / "composed" / "cedar_ridge_v2.barn"))
    flat = emit_dsl(r.plan, flatten=True)
    again = compile_source(flat)
    assert emit_dsl(again.plan, flatten=True) == flat
