"""Serialize a :class:`~barndsl.elements.Barndominium` back to DSL source.

The inverse of :func:`barndsl.compiler.compile_source`. Round-trips: compiling
the emitted text reproduces an equivalent plan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from .constants import FLOOR_ASSEMBLY_DEPTH, WALK_DEFAULT_WIDTH
from .elements import (
    OVERHEAD_DOOR_HEIGHT,
    Alarm,
    Barndominium,
    ExteriorDoor,
    FrameSpec,
    InteriorDoor,
    Light,
    Note,
    Outlet,
    PlacedFixture,
    Porch,
    ProgramSpec,
    Requirement,
    Room,
    SiteSpec,
    Stair,
    Switch,
    UseSpec,
    WallSpec,
    Window,
)

T = TypeVar("T")


def _n(value: float) -> str:
    return f"{value:g}"


def _q(text: str) -> str:
    """Quote a string literal, escaping backslashes and quotes for the lexer."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _instance_emitters() -> tuple[tuple[type, str, Callable[[Any], str]], ...]:
    return (
        (Room, "rooms", _room_line),
        (WallSpec, "walls", _wall_line),
        (InteriorDoor, "doors", _interior_door_line),
        (ExteriorDoor, "exts", _exterior_door_line),
        (Window, "wins", _window_line),
        (PlacedFixture, "fixts", _fixture_line),
        (Outlet, "elec", _outlet_line),
        (Switch, "elec", _switch_line),
        (Light, "elec", _light_line),
        (Alarm, "alarms", _alarm_line),
        (Note, "notes", _note_line),
        (Porch, "porches", _porch_line),
    )


def instance_lines(inst: object) -> list[str]:
    """Canonical DSL statement lines for one stamped :class:`~barndsl.elements.Instance`.

    The flattened form of a single ``use`` — every stamped element as a literal
    host statement, dotted ids kept (so references keep resolving). Used by the
    ``inline_use`` edit to replace one ``use`` line with its members in place, and
    ordered rooms-first so relative reads never precede their target (stamped rooms
    are absolute ``at`` anyway). Part-file comments don't survive (same rule as
    :func:`emit_dsl`).
    """
    order = (
        "rooms", "walls", "doors", "exts", "wins", "fixts", "elec", "alarms", "notes", "porches",
    )
    buckets: dict[str, list[str]] = {name: [] for name in order}
    emitters = _instance_emitters()
    for o in getattr(inst, "objects", []):
        for cls, bucket, formatter in emitters:
            if isinstance(o, cls):
                buckets[bucket].append(formatter(o))
                break
    return [line for name in order for line in buckets[name]]


def _room_line(r: Room) -> str:
    line = (
        f"room {r.id}: {r.type.value} at {_n(r.x)},{_n(r.y)} "
        f"size {_n(r.width)} x {_n(r.length)}"
    )
    if getattr(r, "level", 0):
        line += f" level {r.level}"
    ch = getattr(r, "ceiling_height", None)
    if ch is not None:
        line += f" ceiling {_n(ch)}"
    if getattr(r, "vaulted", False):
        line += " vaulted"
    return line


def _interior_door_line(d: InteriorDoor) -> str:
    kind = getattr(d, "kind", "swing" if getattr(d, "leaf", True) else "cased")
    if kind == "cased":
        line = f"open {d.room_a} - {d.room_b} width {_n(d.width)}"
    else:
        line = f"door {d.room_a} - {d.room_b}"
        if kind != "swing":
            line += f" {kind}"
        line += f" width {_n(d.width)}"
    if d.offset is not None:
        line += f" offset {_n(d.offset)}"
    if getattr(d, "swing_into", None) is not None:
        line += f" into {d.swing_into}"
    if getattr(d, "hinge", None) is not None:
        line += f" hinge {d.hinge}"
    return line


def _exterior_door_line(xd: ExteriorDoor) -> str:
    if getattr(xd, "kind", "entry") == "overhead":
        h = xd.height if xd.height is not None else OVERHEAD_DOOR_HEIGHT
        return (
            f"door {xd.room} {xd.wall.value} overhead width {_n(xd.width)} "
            f"height {_n(h)} offset {_n(xd.offset)}"
        )
    line = f"entry {xd.room} {xd.wall.value}"
    if getattr(xd, "kind", "entry") in ("double", "french"):
        line += f" {xd.kind}"
    line += f" width {_n(xd.width)} offset {_n(xd.offset)}"
    if not xd.egress:
        line += " no-egress"
    return line


def _window_line(w: Window) -> str:
    line = f"window {w.room} {w.wall.value}"
    if getattr(w, "kind", "casement") != "casement":
        line += f" {w.kind}"
    line += f" width {_n(w.width)} offset {_n(w.offset)}"
    if abs(w.sill_height - 3.0) > 1e-6:
        line += f" sill {_n(w.sill_height)}"
    if abs(w.head_height - 6.67) > 1e-6:
        line += f" head {_n(w.head_height)}"
    if getattr(w, "tempered", False):
        line += " tempered"
    return line


def _fixture_line(f: PlacedFixture) -> str:
    line = f"fixture {f.kind} in {f.room}"
    along = f.along
    if along is not None:  # an `along` counter run
        line += f" along {along.value[0].upper()}"
        if f.run_from is not None and f.run_to is not None:
            line += f" from {_n(f.run_from)} to {_n(f.run_to)}"
        if f.run_depth is not None:
            line += f" depth {_n(f.run_depth)}"
        return line
    if f.x is not None and f.y is not None:
        line += f" at {_n(f.x)},{_n(f.y)}"
    if f.wall is not None:
        line += f" wall {f.wall.value[0].upper()}"
        offset = f.offset
        if offset is not None:
            line += f" offset {_n(offset)}"
    if f.rotation:
        line += f" rotate {_n(f.rotation)}"
    if f.width is not None:
        line += f" width {_n(f.width)}"
    return line


def _outlet_line(o: Outlet) -> str:
    line = f"outlet in {o.room} wall {o.wall.value[0].upper()} offset {_n(o.offset)}"
    if o.gfci:
        line += " gfci"
    return line


def _switch_line(sw: Switch) -> str:
    return f"switch in {sw.room} wall {sw.wall.value[0].upper()} offset {_n(sw.offset)}"


def _light_line(lt: Light) -> str:
    line = f"light in {lt.room} at {_n(lt.x)},{_n(lt.y)}"
    if lt.kind != "ceiling":
        line += f" kind {lt.kind}"
    return line


def _alarm_line(a: Alarm) -> str:
    line = f"alarm {a.kind} in {a.room}"
    if a.x is not None and a.y is not None:
        line += f" at {_n(a.x)},{_n(a.y)}"
    return line


def _note_line(nm: Note) -> str:
    line = f"note {_q(nm.text)} at {_n(nm.x)},{_n(nm.y)}"
    if nm.level:
        line += f" level {nm.level}"
    return line


def _porch_line(p: Porch) -> str:
    tag = "covered" if p.covered else "open"
    return (
        f"porch {p.id} at {_n(p.x)},{_n(p.y)} "
        f"size {_n(p.width)} x {_n(p.length)} {tag}"
    )


def _stair_line(s: Stair) -> str:
    return (
        f"stair {s.id} at {_n(s.x)},{_n(s.y)} "
        f"size {_n(s.width)} x {_n(s.length)} from {s.from_level} to {s.to_level}"
    )


def _wall_line(ws: WallSpec) -> str:
    return f"wall {ws.room_a} - {ws.room_b} {' '.join(ws.attributes)}"


def _append_section(out: list[str], lines: list[str]) -> None:
    if lines:
        out.append("")
        out.extend(lines)


def _keep(objs: list[T], *, flatten: bool, stamped: set[int]) -> list[T]:
    return objs if flatten else [o for o in objs if id(o) not in stamped]


def _program_line(spec: ProgramSpec) -> str:
    line = f"program {spec.beds} bed"
    if spec.baths is not None:
        line += f" {spec.baths} bath"
    for rtype, n in spec.required.items():
        line += f" {n} {rtype.value}"
    if spec.min_area is not None:
        line += f" area {_n(spec.min_area)}"
    if spec.min_storage is not None:
        line += f" storage {_n(spec.min_storage)}"
    return line


def _requirement_line(req: Requirement) -> str:
    if req.kind in ("adjacent", "separate"):
        return f"require {req.kind} {req.a} {req.b}"
    if req.kind == "exterior":
        line = f"require exterior {req.a}"
        if req.wall is not None:
            line += f" {req.wall.value}"
        return line
    assert req.min_area is not None
    return f"require area {req.a} >= {_n(req.min_area)}"


def _frame_line(fs: FrameSpec) -> str:
    # `post` is stored in feet; emit it back in inches (how it's authored).
    line = f"frame bay {_n(fs.bay)} span {_n(fs.span)} post {_n(fs.post * 12)}"
    if not fs.ridge:
        line += " no-ridge"
    return line


def _use_line(u: UseSpec) -> str:
    line = f"use {_q(u.relpath)} as {u.alias} at {_n(u.x)},{_n(u.y)}"
    if u.level:
        line += f" level {u.level}"
    if getattr(u, "mirror", None):
        line += f" mirror {u.mirror}"
    if getattr(u, "rotate", 0):
        line += f" rotate {u.rotate}"
    uparams = getattr(u, "params", None)
    if uparams:
        # Emit exactly the pairs the author passed, in source order (Phase 20).
        # ft-in values canonicalize to decimal feet; recompiling + re-emitting is
        # a fixpoint.
        pairs = ", ".join(f"{k}={_n(v)}" for k, v in uparams.items())
        line += f" with {pairs}"
    return line


def _append_site_declaration_lines(out: list[str], ss: SiteSpec) -> None:
    if ss.has_dims:
        assert ss.width is not None and ss.length is not None
        out.append(f"site {_n(ss.width)} x {_n(ss.length)}")
    if ss.has_setback:
        line = "setback"
        if ss.front is not None:
            line += f" front {_n(ss.front)}"
        if ss.side is not None:
            line += f" side {_n(ss.side)}"
        if ss.rear is not None:
            line += f" rear {_n(ss.rear)}"
        out.append(line)
    if ss.has_building:
        assert ss.building_x is not None and ss.building_y is not None
        out.append(f"building at {_n(ss.building_x)},{_n(ss.building_y)}")


def _append_site_feature_lines(out: list[str], ss: SiteSpec) -> None:
    side_letter = {"north": "N", "south": "S", "east": "E", "west": "W"}
    for d in ss.drives:
        line = f"drive at {_n(d.x)},{_n(d.y)} size {_n(d.width)} x {_n(d.length)}"
        if d.surface != "gravel":  # gravel is the default, so it's implicit
            line += f" {d.surface}"
        out.append(line)
    for wk in ss.walks:
        line = f"walk from {wk.room} to drive"
        if abs(wk.width - WALK_DEFAULT_WIDTH) > 1e-9:
            line += f" width {_n(wk.width)}"
        out.append(line)
    for wl in ss.wells:
        out.append(f"well at {_n(wl.x)},{_n(wl.y)}")
    for sp in ss.septics:
        line = f"septic at {_n(sp.x)},{_n(sp.y)}"
        if sp.field_width is not None and sp.field_length is not None:
            line += f" field {_n(sp.field_width)} x {_n(sp.field_length)}"
        out.append(line)
    for sv in ss.services:
        out.append(f"service {sv.utility} from {side_letter[sv.side.value]}")


def _append_site_lines(out: list[str], ss: SiteSpec | None) -> None:
    if ss is None:
        return
    _append_site_declaration_lines(out, ss)
    _append_site_feature_lines(out, ss)


def _append_identity_lines(out: list[str], plan: Barndominium, *, fragment: bool) -> None:
    if fragment:
        # A part has no plan/envelope; its params lead (Phase 20).
        for pname, pval in getattr(plan, "params", {}).items():
            out.append(f"param {pname} = {_n(pval)}")
    else:
        out.append(f"plan {_q(plan.name)}")
        out.append(f"envelope {_n(plan.envelope_width)} x {_n(plan.envelope_length)}")

    for wing in plan.wings:
        out.append(
            f"wing {_n(wing.width)} x {_n(wing.length)} at {_n(wing.x)},{_n(wing.y)}"
        )
    if not fragment:
        # `ceiling` is host-only (a part borrows the host's).
        out.append(f"ceiling {_n(plan.ceiling_height)}")


def _finish_line(plan: Barndominium) -> str | None:
    if not (getattr(plan, "siding", None) or getattr(plan, "roofing", None)):
        return None
    line = "finish"
    if plan.siding:
        line += f" siding {_q(plan.siding)}"
    if plan.roofing:
        line += f" roof {_q(plan.roofing)}"
    return line


def _roof_line(plan: Barndominium) -> str | None:
    if getattr(plan, "roof_style", "gable") == "gable" and not getattr(plan, "roof_pitch", None):
        return None
    line = f"roof {getattr(plan, 'roof_style', 'gable')}"
    pitch = getattr(plan, "roof_pitch", None)
    if pitch:
        line += f" pitch {_n(pitch)}"
    return line


def _append_option_lines(out: list[str], plan: Barndominium) -> None:
    if abs(plan.floor_depth - FLOOR_ASSEMBLY_DEPTH) > 1e-9:
        out.append(f"floor {_n(plan.floor_depth)}")
    if plan.accessible:
        out.append("accessible")
    if getattr(plan, "electrical", False):
        out.append("electrical")
    if plan.orientation is not None:
        # A declared `orientation 0` round-trips (distinct from undeclared/None).
        out.append(f"orientation {_n(plan.orientation)}")
    if plan.street is not None:
        out.append(f"street {plan.street.value}")
    if finish := _finish_line(plan):
        out.append(finish)
    if getattr(plan, "overhang", 0.0):
        out.append(f"overhang {_n(plan.overhang)}")
    if getattr(plan, "climate", None) is not None:
        out.append(f"climate {plan.climate}")
    if roof := _roof_line(plan):
        out.append(roof)


def _append_contract_lines(
    out: list[str],
    plan: Barndominium,
    keep: Callable[[Any], Any],
) -> None:
    if plan.program_spec is not None:
        out.append(_program_line(plan.program_spec))
    for req in getattr(plan, "requirements", None) or []:
        # Declared spatial intent rides next to `program` — the plan's contract
        # block, ahead of the geometry it constrains.
        out.append(_requirement_line(req))
    for ws in keep(getattr(plan, "wall_specs", None) or []):
        # Declared wall attributes sit in the same contract block; attributes
        # are stored in canonical order, so this is already deterministic.
        out.append(_wall_line(ws))
    for s in keep(getattr(plan, "suites", None) or []):
        # Declared groupings ride the contract block, in declaration order.
        out.append(f"suite {s.id}: {' '.join(s.members)}")
    for z in keep(getattr(plan, "zones", None) or []):
        out.append(f"zone {z.id}: {' '.join(z.members)}")
    for note in (plan.notes or "").splitlines():
        if note.strip():
            out.append(f"note {_q(note.strip())}")
    for nm in keep(getattr(plan, "note_marks", None) or []):
        out.append(_note_line(nm))
    if plan.frame_spec is not None:
        out.append(_frame_line(plan.frame_spec))


def _append_use_lines(out: list[str], plan: Barndominium, *, flatten: bool) -> None:
    if flatten or not plan.uses:
        return
    # Cross-file composition — emit the `use` lines verbatim; the stamped elements
    # they pull in are skipped below.
    out.append("")
    for u in plan.uses:
        out.append(_use_line(u))


def _append_model_sections(
    out: list[str],
    plan: Barndominium,
    keep: Callable[[Any], Any],
) -> None:
    _append_section(out, [_room_line(r) for r in keep(plan.rooms)])
    _append_section(out, [_interior_door_line(d) for d in keep(plan.interior_doors)])
    _append_section(out, [_exterior_door_line(xd) for xd in keep(plan.exterior_doors)])
    _append_section(out, [_window_line(w) for w in keep(plan.windows)])

    # Author-placed fixtures only. Auto-seeds (bath/kitchen/laundry footprints
    # the layout derives) are never stored in `plan.fixtures`, so they never reach
    # here — the emitted source carries exactly what the author wrote.
    _append_section(out, [_fixture_line(f) for f in keep(plan.fixtures)])
    _append_section(out, [_porch_line(p) for p in keep(plan.porches)])
    _append_section(out, [_stair_line(s) for s in keep(plan.stairs)])

    outlets, switches, lights = keep(plan.outlets), keep(plan.switches), keep(plan.lights)
    _append_section(
        out,
        [_outlet_line(o) for o in outlets]
        + [_switch_line(sw) for sw in switches]
        + [_light_line(lt) for lt in lights],
    )
    _append_section(out, [_alarm_line(a) for a in keep(plan.alarms)])


def emit_dsl(plan: Barndominium, flatten: bool = False, fragment: bool = False) -> str:
    """Return canonical DSL source for ``plan``.

    Cross-file composition (see :mod:`barndsl.compose`) round-trips two ways:

    * default (``flatten=False``) — emit each ``use`` line **verbatim**
      (path/alias/at/level/mirror/rotate/with) and *skip* the elements it stamped,
      so the composed plan is described by reference (host text + ``use`` lines).
    * ``flatten=True`` — drop the ``use`` lines and emit the stamped elements as
      literal host statements (dotted ids kept), inlining every part. Recompiling
      the flattened form reproduces the same composed plan.

    ``fragment=True`` (Phase 20) emits a **part** file: no ``plan``/``envelope``
    header, its ``param`` declarations first, then its statements — the round-trip
    form for a part authored on disk (used by ``fmt``-style tooling and the
    showcase's emit fixpoint).
    """
    stamped = set() if flatten else {id(o) for inst in plan.instances for o in inst.objects}

    def keep(objs: list[T]) -> list[T]:
        return _keep(objs, flatten=flatten, stamped=stamped)

    out: list[str] = []
    _append_identity_lines(out, plan, fragment=fragment)
    _append_option_lines(out, plan)
    _append_site_lines(out, getattr(plan, "site_spec", None))
    if plan.grade is not None:
        out.append(f"grade {_n(plan.grade)}")
    _append_contract_lines(out, plan, keep)
    _append_use_lines(out, plan, flatten=flatten)
    _append_model_sections(out, plan, keep)
    return "\n".join(out) + "\n"
