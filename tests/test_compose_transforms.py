"""Cross-file composition transforms (the ``use ... mirror|rotate`` remap) — Phase 7b.

Grammar accept/reject for ``mirror x|y`` / ``rotate 90|180|270``; the §5 remap
table (geometry, wall directions, wall offsets re-derived from geometry, fixture
rotation, interior-door offsets); the four property-test families (mirror∘mirror,
rotate⁴, rotate-decomposition, the offset oracle); emit verbatim + flatten for a
transformed use; inline equivalence for a mirrored/rotated instance; and the
``set_use`` transform round-trips.
"""

from __future__ import annotations

import dataclasses

import pytest

from barndsl import compose
from barndsl.compiler import compile_source
from barndsl.compose import PartComponent, _Xform, stamp_instance
from barndsl.edits import apply_edit, edit_from_json
from barndsl.elements import Barndominium, UseSpec
from barndsl.emit import emit_dsl
from barndsl.geometry import shared_edge


# --- helpers -----------------------------------------------------------------


def _fragment(text: str) -> Barndominium:
    """Fragment-compile a part body into its normalized (SW-origin) plan."""
    r = compile_source(text, fragment=True)
    assert r.plan is not None and not r.errors, r.summary()
    return r.plan


def _component(part: Barndominium) -> PartComponent:
    width = max((rm.x + rm.width for rm in part.rooms), default=0.0)
    length = max((rm.y + rm.length for rm in part.rooms), default=0.0)
    return PartComponent("mem", "mem.barn", part, [], width, length, False)


def _stamp(part: Barndominium, *, mirror=None, rotate=0, at=(0.0, 0.0)):
    """Stamp ``part`` at ``at`` with the given transform into a fresh host and
    return ``(host, instance)``. The alias is ``q``."""
    host = Barndominium(name="H")
    use = UseSpec("mem.barn", "q", at[0], at[1], mirror=mirror, rotate=rotate,
                  line=1, col=1, end_col=4)
    inst = stamp_instance(host, _component(part), use)
    return host, inst


def _unprefix(name: str) -> str:
    return name.split(".", 1)[1] if "." in name else name


def _part_from_stamp(host: Barndominium) -> Barndominium:
    """Rebuild a fragment part from a host stamped at the origin — the stamped
    elements are already in local coords (``at`` was 0,0), so this simply strips
    the ``q.`` prefixes back off, letting a transform be applied a second time."""
    p = Barndominium(name="R")
    for rm in host.rooms:
        p.rooms.append(dataclasses.replace(rm, id=_unprefix(rm.id)))
    for d in host.interior_doors:
        p.interior_doors.append(dataclasses.replace(
            d, room_a=_unprefix(d.room_a), room_b=_unprefix(d.room_b),
            swing_into=_unprefix(d.swing_into) if d.swing_into else None))
    for xd in host.exterior_doors:
        p.exterior_doors.append(dataclasses.replace(xd, room=_unprefix(xd.room)))
    for w in host.windows:
        p.windows.append(dataclasses.replace(w, room=_unprefix(w.room)))
    for f in host.fixtures:
        p.fixtures.append(dataclasses.replace(f, room=_unprefix(f.room)))
    for o in host.outlets:
        p.outlets.append(dataclasses.replace(o, room=_unprefix(o.room)))
    for s in host.switches:
        p.switches.append(dataclasses.replace(s, room=_unprefix(s.room)))
    for lt in host.lights:
        p.lights.append(dataclasses.replace(lt, room=_unprefix(lt.room)))
    for al in host.alarms:
        p.alarms.append(dataclasses.replace(al, room=_unprefix(al.room)))
    for nm in host.note_marks:
        p.note_marks.append(dataclasses.replace(nm))
    return p


def _snap(host: Barndominium):
    """A canonical, order-independent snapshot of every stamped element's
    transform-relevant state — ids, geometry, walls, offsets, rotations."""
    def rnd(v):
        return None if v is None else round(float(v), 5)

    rooms = sorted((rm.id, rnd(rm.x), rnd(rm.y), rnd(rm.width), rnd(rm.length),
                    rm.level) for rm in host.rooms)
    wins = sorted((w.room, w.wall.value, rnd(w.offset), rnd(w.width))
                  for w in host.windows)
    exts = sorted((x.room, x.wall.value, rnd(x.offset), rnd(x.width))
                  for x in host.exterior_doors)
    doors = sorted((d.room_a, d.room_b, rnd(d.width), rnd(d.offset),
                    d.swing_into) for d in host.interior_doors)
    fixts = sorted((f.kind, f.room, rnd(f.x), rnd(f.y),
                    f.wall.value if f.wall else None, rnd(f.rotation),
                    rnd(f.width)) for f in host.fixtures)
    outs = sorted((o.room, o.wall.value, rnd(o.offset)) for o in host.outlets)
    sws = sorted((s.room, s.wall.value, rnd(s.offset)) for s in host.switches)
    lights = sorted((lt.room, rnd(lt.x), rnd(lt.y)) for lt in host.lights)
    alarms = sorted((a.room, a.kind, rnd(a.x), rnd(a.y)) for a in host.alarms)
    notes = sorted((n.text, rnd(n.x), rnd(n.y)) for n in host.note_marks)
    return (rooms, wins, exts, doors, fixts, outs, sws, lights, alarms, notes)


#: A rich asymmetric part exercising every remappable attribute: two rooms with a
#: connecting door at an offset, an exterior window + door on the bath, an outlet,
#: a switch, a light, an alarm, an explicitly-placed rotated fixture, and a note.
RICH = """\
room bath: bathroom at 0,0 size 9 x 7
room wic: closet east-of bath align near size 4 x 7
door bath - wic width 2.5 offset 0.75 into wic hinge near
window bath south casement width 2.5 offset 1 sill 4
entry bath west width 3 offset 1.5
outlet in bath wall south offset 3 gfci
switch in bath wall east offset 1
light in bath at 3,4
alarm smoke in bath at 2,2
fixture wardrobe in wic at 0.5,0.5 wall E rotate 90
note "vent" at 1,6
"""


# --- grammar accept / reject -------------------------------------------------


@pytest.mark.parametrize("clause", [
    "mirror x", "mirror y", "rotate 90", "rotate 180", "rotate 270",
    "mirror y rotate 90", "rotate 270 mirror x",
])
def test_transform_clauses_parse(tmp_path, clause):
    (tmp_path / "p.barn").write_text("room a: bathroom at 0,0 size 8 x 6\n")
    src = ('plan "H"\nenvelope 30 x 20\nceiling 9\n'
           'room r: living at 0,0 size 10 x 10\n'
           f'use "p.barn" as q at 12,0 {clause}\n')
    r = compile_source(src, base_dir=str(tmp_path))
    assert "BAD_OPTION" not in {d.code for d in r.diagnostics}, r.summary()
    u = r.plan.uses[0]
    assert (u.mirror, u.rotate) != (None, 0)


@pytest.mark.parametrize("clause,teaches", [
    ("rotate 45", "90"),       # 45 isn't a 90-multiple
    ("rotate 0", "90"),        # 0 is a no-op, rejected as ill-formed
    ("mirror z", "x or y"),    # z isn't an axis
    ("mirror", "x or y"),      # missing axis
    ("rotate 900", "90"),      # not one of the three turns
])
def test_bad_transform_clauses_are_rejected(clause, teaches):
    src = ('plan "H"\nenvelope 30 x 20\nceiling 9\n'
           f'use "p.barn" as q at 0,0 {clause}\n')
    r = compile_source(src)
    bad = [d for d in r.diagnostics if d.code in ("BAD_OPTION", "SYNTAX", "BAD_LEVEL")]
    assert bad, r.summary()
    assert any(teaches in (d.hint or "") + d.message for d in bad), bad[0].message


# --- geometry basics ---------------------------------------------------------


def test_stamp_lands_transformed_bbox_sw_corner_at_at():
    part = _fragment("room a: bathroom at 0,0 size 8 x 6\n")
    for mirror, rotate in [(None, 0), ("y", 0), ("x", 0), (None, 90),
                           (None, 180), (None, 270), ("y", 90)]:
        host, inst = _stamp(part, mirror=mirror, rotate=rotate, at=(5.0, 3.0))
        xs = [rm.x for rm in host.rooms]
        ys = [rm.y for rm in host.rooms]
        assert min(xs) == pytest.approx(5.0) and min(ys) == pytest.approx(3.0)


def test_rotation_swaps_width_and_length():
    part = _fragment("room a: bathroom at 0,0 size 8 x 6\n")  # bbox 8 x 6
    _, i0 = _stamp(part, rotate=0)
    _, i90 = _stamp(part, rotate=90)
    _, i180 = _stamp(part, rotate=180)
    assert (i0.bbox[2], i0.bbox[3]) == pytest.approx((8, 6))
    assert (i90.bbox[2], i90.bbox[3]) == pytest.approx((6, 8))   # swapped
    assert (i180.bbox[2], i180.bbox[3]) == pytest.approx((8, 6))  # back


def test_wall_direction_remap_table():
    part = _fragment(
        "room a: bathroom at 0,0 size 8 x 6\n"
        "window a south casement width 2 offset 3\n")
    # rotate 90 ccw: S -> E
    _, _i = _stamp(part, rotate=90)
    hosts = {
        (None, 90): "east", (None, 180): "north", (None, 270): "west",
        ("y", 0): "south", ("x", 0): "north",
    }
    for (mirror, rotate), wall in hosts.items():
        host, _ = _stamp(part, mirror=mirror, rotate=rotate)
        assert host.windows[0].wall.value == wall, (mirror, rotate)


# --- property tests (§5 bullet 5) --------------------------------------------


def test_mirror_y_is_an_involution_full_element_set():
    part = _fragment(RICH)
    once, _ = _stamp(part, mirror="y")
    twice, _ = _stamp(_part_from_stamp(once), mirror="y")
    ident, _ = _stamp(part)
    assert _snap(twice) == _snap(ident)


def test_mirror_x_is_an_involution_full_element_set():
    part = _fragment(RICH)
    once, _ = _stamp(part, mirror="x")
    twice, _ = _stamp(_part_from_stamp(once), mirror="x")
    assert _snap(twice) == _snap(_stamp(part)[0])


def test_rotate_90_four_times_is_identity():
    part = _fragment(RICH)
    cur = part
    for _ in range(4):
        host, _ = _stamp(cur, rotate=90)
        cur = _part_from_stamp(host)
    assert _snap(_stamp(cur)[0]) == _snap(_stamp(part)[0])


def test_rotate_180_twice_is_identity():
    part = _fragment(RICH)
    once, _ = _stamp(part, rotate=180)
    twice, _ = _stamp(_part_from_stamp(once), rotate=180)
    assert _snap(twice) == _snap(_stamp(part)[0])


def test_rotate_270_equals_rotate_90_thrice():
    part = _fragment(RICH)
    single, _ = _stamp(part, rotate=270)
    cur = part
    for _ in range(3):
        host, _ = _stamp(cur, rotate=90)
        cur = _part_from_stamp(host)
    thrice, _ = _stamp(cur)
    assert _snap(single) == _snap(thrice)


@pytest.mark.parametrize("mirror,rotate", [
    (None, 0), ("y", 0), ("x", 0), (None, 90), (None, 180), (None, 270),
    ("y", 90), ("x", 90), ("y", 180), ("x", 270),
])
def test_offset_oracle_wall_features_measured_from_transformed_geometry(mirror, rotate):
    """For every wall-attached feature, the stamped offset equals the offset
    re-derived from the transformed span measured on the new wall's start corner."""
    part = _fragment(RICH)
    xf = _Xform(rotate, mirror)
    dims = {rm.id: (rm.width, rm.length) for rm in part.rooms}
    host, _ = _stamp(part, mirror=mirror, rotate=rotate)

    # windows / exterior doors / outlets / switches carry (room, wall, offset).
    src_wins = {(w.room, w.wall): w for w in part.windows}
    for w in host.windows:
        local = _unprefix(w.room)
        sw = next(s for (r, wl), s in src_wins.items() if r == local)
        rw, rl = dims[local]
        _, expect = compose._remap_wall_offset(xf, sw.wall, sw.offset, sw.width, rw, rl)
        assert w.offset == pytest.approx(expect)

    for o in host.outlets:
        local = _unprefix(o.room)
        so = next(s for s in part.outlets if s.room == local)
        rw, rl = dims[local]
        _, expect = compose._remap_wall_offset(xf, so.wall, so.offset, 0.0, rw, rl)
        assert o.offset == pytest.approx(expect)


def test_interior_door_offset_re_derived_from_shared_edge():
    part = _fragment(RICH)
    # The bath–wic door sits 0.75 ft from the shared wall's south (low) end.
    for mirror, rotate in [("y", 0), ("x", 0), (None, 90), (None, 270), ("y", 90)]:
        host, _ = _stamp(part, mirror=mirror, rotate=rotate)
        d = host.interior_doors[0]
        a = host.room(d.room_a)
        b = host.room(d.room_b)
        edge = shared_edge(a, b)
        assert edge is not None
        # the door span lies inside the shared edge, offset measured from its lo end
        assert 0 <= d.offset <= edge.length - d.width + 1e-6


def test_mirror_y_keeps_part_internal_diagnostics(tmp_path):
    """A mirrored bath core reports the same part-internal diagnostic set as the
    untransformed one — clearances are mirror-invariant (§5 property 4)."""
    (tmp_path / "parts").mkdir()
    bath = (tmp_path / "parts" / "bath_core.barn")
    bath.write_text(
        "room bath: bathroom at 0,0 size 8 x 6\n"
        "window bath south casement width 2.5 offset 2.75 sill 4 tempered\n")
    base = ('plan "H"\nenvelope 40 x 20\nceiling 9\n'
            'room living: living at 0,0 size 20 x 18\n'
            'entry living south width 3 offset 8\n')
    plain = compile_source(base + 'use "parts/bath_core.barn" as b at 24,0\n',
                           base_dir=str(tmp_path))
    mirr = compile_source(base + 'use "parts/bath_core.barn" as b at 24,0 mirror y\n',
                          base_dir=str(tmp_path))
    part_codes = lambda r: sorted(d.code for d in r.diagnostics if d.part)
    assert part_codes(plain) == part_codes(mirr)


# --- emit verbatim + flatten -------------------------------------------------


def _env(tmp_path):
    (tmp_path / "p.barn").write_text(
        "room bath: bathroom at 0,0 size 9 x 7\n"
        "room wic: closet east-of bath align near size 4 x 7\n"
        "door bath - wic width 2.5 offset 0.75 into wic hinge near\n")
    src = ('plan "H"\nenvelope 40 x 24\nceiling 9\n'
           'room hall: hallway at 0,12 size 40 x 8\n'
           'entry hall west width 3 offset 2\n'
           'use "p.barn" as q at 0,0 mirror y rotate 90\n'
           'door hall - q.bath cased width 3\n')
    return src, str(tmp_path)


def test_emit_verbatim_carries_mirror_and_rotate(tmp_path):
    src, bd = _env(tmp_path)
    r = compile_source(src, base_dir=bd)
    out = emit_dsl(r.plan)
    assert 'use "p.barn" as q at 0,0 mirror y rotate 90' in out
    # verbatim emit skips the stamped elements
    assert "room q.bath" not in out


def test_flatten_writes_transformed_elements_and_recompiles_identically(tmp_path):
    src, bd = _env(tmp_path)
    r = compile_source(src, base_dir=bd)
    flat = emit_dsl(r.plan, flatten=True)
    assert "use " not in flat and "room q.bath" in flat
    again = compile_source(flat, base_dir=bd)
    snap = lambda p: sorted((rm.id, round(rm.x, 4), round(rm.y, 4),
                             round(rm.width, 4), round(rm.length, 4))
                            for rm in p.plan.rooms)
    assert snap(r) == snap(again)
    doors = lambda p: sorted((d.room_a, d.room_b, d.offset)
                             for d in p.plan.interior_doors)
    assert doors(r) == doors(again)


# --- inline equivalence for a transformed instance ---------------------------


def test_inline_use_round_trips_a_transformed_instance(tmp_path):
    src, bd = _env(tmp_path)
    before = compile_source(src, base_dir=bd)
    r = apply_edit(src, edit_from_json({"kind": "inline_use", "alias": "q"}), base_dir=bd)
    assert r.changed and "as q " not in r.source and "room q.bath" in r.source
    after = compile_source(r.source, base_dir=bd)
    everything = lambda p: (
        sorted((rm.id, round(rm.x, 4), round(rm.y, 4), round(rm.width, 4),
                round(rm.length, 4)) for rm in p.plan.rooms),
        sorted((d.room_a, d.room_b, d.offset) for d in p.plan.interior_doors),
    )
    assert everything(before) == everything(after)


# --- set_use transform round-trips -------------------------------------------


def _set(src, bd, obj):
    return apply_edit(src, edit_from_json(obj), base_dir=bd)


def test_set_use_adds_and_clears_mirror_and_rotate(tmp_path):
    (tmp_path / "p.barn").write_text("room a: bathroom at 0,0 size 8 x 6\n")
    src = ('plan "H"\nenvelope 30 x 20\nceiling 9\n'
           'room r: living at 0,0 size 10 x 10\n'
           'use "p.barn" as q at 12,0\n')
    bd = str(tmp_path)
    r = _set(src, bd, {"kind": "set_use", "alias": "q", "mirror": "y"})
    assert r.changed and 'as q at 12,0 mirror y' in r.source
    r = _set(r.source, bd, {"kind": "set_use", "alias": "q", "rotate": 90})
    assert r.changed and 'mirror y rotate 90' in r.source
    # clearing: mirror:null drops the flag, rotate:0 drops the turn
    r = _set(r.source, bd, {"kind": "set_use", "alias": "q", "mirror": None})
    assert r.changed and "mirror" not in r.source and "rotate 90" in r.source
    r = _set(r.source, bd, {"kind": "set_use", "alias": "q", "rotate": 0})
    assert r.changed and "rotate" not in r.source


def test_set_use_rejects_non_ninety_rotation(tmp_path):
    (tmp_path / "p.barn").write_text("room a: bathroom at 0,0 size 8 x 6\n")
    src = ('plan "H"\nenvelope 30 x 20\nceiling 9\n'
           'use "p.barn" as q at 0,0\n')
    r = _set(src, str(tmp_path), {"kind": "set_use", "alias": "q", "rotate": 45})
    assert r.error is not None and r.error.kind == "bad_value"


def test_move_use_preserves_transform(tmp_path):
    (tmp_path / "p.barn").write_text("room a: bathroom at 0,0 size 8 x 6\n")
    src = ('plan "H"\nenvelope 30 x 20\nceiling 9\n'
           'use "p.barn" as q at 0,0 mirror y rotate 90\n')
    r = _set(src, str(tmp_path), {"kind": "move_use", "alias": "q", "x": 4, "y": 2})
    assert r.changed and 'as q at 4,2 mirror y rotate 90' in r.source


def test_add_use_can_carry_transform(tmp_path):
    (tmp_path / "p.barn").write_text("room a: bathroom at 0,0 size 8 x 6\n")
    src = ('plan "H"\nenvelope 30 x 20\nceiling 9\n'
           'room r: living at 0,0 size 10 x 10\n')
    r = _set(src, str(tmp_path), {"kind": "add_use", "relpath": "p.barn",
                                  "alias": "q", "x": 12, "y": 0,
                                  "mirror": "x", "rotate": 270})
    assert r.changed and 'as q at 12,0 mirror x rotate 270' in r.source


# --- render-level fixture rotation under mirror ------------------------------


def test_fixture_rotation_reflects_under_mirror_and_resolves(tmp_path):
    """The stamped fixture layout of a mirrored part is the mirror of the plain
    one — checked through the *resolver* (render level), not just the stored
    rotation: the resolved footprints reflect east↔west about the room."""
    from barndsl.fixtures import resolve_room_fixtures

    part = _fragment(
        "room k: kitchen at 0,0 size 12 x 10\n"
        "fixture kitchen_island in k at 2,3 rotate 90\n")
    plain, _ = _stamp(part)
    mirr, _ = _stamp(part, mirror="y")
    pr = plain.room("q.k")
    mr = mirr.room("q.k")
    pf = next(f for f in resolve_room_fixtures(plain, pr) if f.kind == "kitchen_island")
    mf = next(f for f in resolve_room_fixtures(mirr, mr) if f.kind == "kitchen_island")
    # mirror y about the room's vertical centre line: x' = room.width - (x + w)
    assert mf.x == pytest.approx(pr.width - (pf.x + pf.width))
    assert mf.y == pytest.approx(pf.y)
    assert mf.width == pytest.approx(pf.width) and mf.length == pytest.approx(pf.length)
