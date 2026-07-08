"""Serialize a :class:`~barndsl.elements.Barndominium` back to DSL source.

The inverse of :func:`barndsl.compiler.compile_source`. Round-trips: compiling
the emitted text reproduces an equivalent plan.
"""

from __future__ import annotations

from .constants import FLOOR_ASSEMBLY_DEPTH, WALK_DEFAULT_WIDTH
from .elements import OVERHEAD_DOOR_HEIGHT, Barndominium


def _n(value: float) -> str:
    return f"{value:g}"


def _q(text: str) -> str:
    """Quote a string literal, escaping backslashes and quotes for the lexer."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def instance_lines(inst: object) -> list[str]:
    """Canonical DSL statement lines for one stamped :class:`~barndsl.elements.Instance`.

    The flattened form of a single ``use`` — every stamped element as a literal
    host statement, dotted ids kept (so references keep resolving). Used by the
    ``inline_use`` edit to replace one ``use`` line with its members in place, and
    ordered rooms-first so relative reads never precede their target (stamped rooms
    are absolute ``at`` anyway). Part-file comments don't survive (same rule as
    :func:`emit_dsl`).
    """
    from .elements import (
        Alarm, ExteriorDoor, InteriorDoor, Light, Note, Outlet, PlacedFixture,
        Porch, Room, Switch, WallSpec, Window,
    )

    rooms, doors, exts, wins, fixts, elec, alarms, notes, porches, walls = (
        [], [], [], [], [], [], [], [], [], [])
    for o in getattr(inst, "objects", []):
        if isinstance(o, Room):
            rooms.append(_room_line(o))
        elif isinstance(o, InteriorDoor):
            doors.append(_interior_door_line(o))
        elif isinstance(o, ExteriorDoor):
            exts.append(_exterior_door_line(o))
        elif isinstance(o, Window):
            wins.append(_window_line(o))
        elif isinstance(o, PlacedFixture):
            fixts.append(_fixture_line(o))
        elif isinstance(o, Outlet):
            elec.append(_outlet_line(o))
        elif isinstance(o, Switch):
            elec.append(_switch_line(o))
        elif isinstance(o, Light):
            elec.append(_light_line(o))
        elif isinstance(o, Alarm):
            alarms.append(_alarm_line(o))
        elif isinstance(o, Note):
            notes.append(_note_line(o))
        elif isinstance(o, Porch):
            porches.append(_porch_line(o))
        elif isinstance(o, WallSpec):
            walls.append(f"wall {o.room_a} - {o.room_b} {' '.join(o.attributes)}")
    return rooms + walls + doors + exts + wins + fixts + elec + alarms + notes + porches


def _room_line(r: object) -> str:
    line = (
        f"room {r.id}: {r.type.value} at {_n(r.x)},{_n(r.y)} "  # type: ignore[attr-defined]
        f"size {_n(r.width)} x {_n(r.length)}"  # type: ignore[attr-defined]
    )
    if getattr(r, "level", 0):
        line += f" level {r.level}"  # type: ignore[attr-defined]
    ch = getattr(r, "ceiling_height", None)
    if ch is not None:
        line += f" ceiling {_n(ch)}"
    if getattr(r, "vaulted", False):
        line += " vaulted"
    return line


def _interior_door_line(d: object) -> str:
    kind = getattr(d, "kind", "swing")
    if kind == "cased":
        line = f"open {d.room_a} - {d.room_b} width {_n(d.width)}"  # type: ignore[attr-defined]
    else:
        line = f"door {d.room_a} - {d.room_b}"  # type: ignore[attr-defined]
        if kind != "swing":
            line += f" {kind}"
        line += f" width {_n(d.width)}"  # type: ignore[attr-defined]
    if d.offset is not None:  # type: ignore[attr-defined]
        line += f" offset {_n(d.offset)}"  # type: ignore[attr-defined]
    if getattr(d, "swing_into", None) is not None:
        line += f" into {d.swing_into}"  # type: ignore[attr-defined]
    if getattr(d, "hinge", None) is not None:
        line += f" hinge {d.hinge}"  # type: ignore[attr-defined]
    return line


def _exterior_door_line(xd: object) -> str:
    if getattr(xd, "kind", "entry") == "overhead":
        h = xd.height if xd.height is not None else OVERHEAD_DOOR_HEIGHT  # type: ignore[attr-defined]
        return (
            f"door {xd.room} {xd.wall.value} overhead width {_n(xd.width)} "  # type: ignore[attr-defined]
            f"height {_n(h)} offset {_n(xd.offset)}"  # type: ignore[attr-defined]
        )
    line = f"entry {xd.room} {xd.wall.value}"  # type: ignore[attr-defined]
    if getattr(xd, "kind", "entry") in ("double", "french"):
        line += f" {xd.kind}"  # type: ignore[attr-defined]
    line += f" width {_n(xd.width)} offset {_n(xd.offset)}"  # type: ignore[attr-defined]
    if not xd.egress:  # type: ignore[attr-defined]
        line += " no-egress"
    return line


def _window_line(w: object) -> str:
    line = f"window {w.room} {w.wall.value}"  # type: ignore[attr-defined]
    if getattr(w, "kind", "casement") != "casement":
        line += f" {w.kind}"  # type: ignore[attr-defined]
    line += f" width {_n(w.width)} offset {_n(w.offset)}"  # type: ignore[attr-defined]
    if abs(w.sill_height - 3.0) > 1e-6:  # type: ignore[attr-defined]
        line += f" sill {_n(w.sill_height)}"  # type: ignore[attr-defined]
    if abs(w.head_height - 6.67) > 1e-6:  # type: ignore[attr-defined]
        line += f" head {_n(w.head_height)}"  # type: ignore[attr-defined]
    if getattr(w, "tempered", False):
        line += " tempered"
    return line


def _fixture_line(f: object) -> str:
    line = f"fixture {f.kind} in {f.room}"  # type: ignore[attr-defined]
    if getattr(f, "along", None) is not None:  # an `along` counter run
        line += f" along {f.along.value[0].upper()}"  # type: ignore[attr-defined]
        if f.run_from is not None and f.run_to is not None:  # type: ignore[attr-defined]
            line += f" from {_n(f.run_from)} to {_n(f.run_to)}"  # type: ignore[attr-defined]
        if f.run_depth is not None:  # type: ignore[attr-defined]
            line += f" depth {_n(f.run_depth)}"  # type: ignore[attr-defined]
        return line
    if f.x is not None and f.y is not None:  # type: ignore[attr-defined]
        line += f" at {_n(f.x)},{_n(f.y)}"  # type: ignore[attr-defined]
    if f.wall is not None:  # type: ignore[attr-defined]
        line += f" wall {f.wall.value[0].upper()}"  # type: ignore[attr-defined]
        if getattr(f, "offset", None) is not None:
            line += f" offset {_n(f.offset)}"  # type: ignore[attr-defined]
    if f.rotation:  # type: ignore[attr-defined]
        line += f" rotate {_n(f.rotation)}"  # type: ignore[attr-defined]
    if f.width is not None:  # type: ignore[attr-defined]
        line += f" width {_n(f.width)}"  # type: ignore[attr-defined]
    return line


def _outlet_line(o: object) -> str:
    line = f"outlet in {o.room} wall {o.wall.value[0].upper()} offset {_n(o.offset)}"  # type: ignore[attr-defined]
    if o.gfci:  # type: ignore[attr-defined]
        line += " gfci"
    return line


def _switch_line(sw: object) -> str:
    return f"switch in {sw.room} wall {sw.wall.value[0].upper()} offset {_n(sw.offset)}"  # type: ignore[attr-defined]


def _light_line(lt: object) -> str:
    line = f"light in {lt.room} at {_n(lt.x)},{_n(lt.y)}"  # type: ignore[attr-defined]
    if lt.kind != "ceiling":  # type: ignore[attr-defined]
        line += f" kind {lt.kind}"  # type: ignore[attr-defined]
    return line


def _alarm_line(a: object) -> str:
    line = f"alarm {a.kind} in {a.room}"  # type: ignore[attr-defined]
    if a.x is not None and a.y is not None:  # type: ignore[attr-defined]
        line += f" at {_n(a.x)},{_n(a.y)}"  # type: ignore[attr-defined]
    return line


def _note_line(nm: object) -> str:
    line = f"note {_q(nm.text)} at {_n(nm.x)},{_n(nm.y)}"  # type: ignore[attr-defined]
    if nm.level:  # type: ignore[attr-defined]
        line += f" level {nm.level}"  # type: ignore[attr-defined]
    return line


def _porch_line(p: object) -> str:
    tag = "covered" if p.covered else "open"  # type: ignore[attr-defined]
    return (
        f"porch {p.id} at {_n(p.x)},{_n(p.y)} "  # type: ignore[attr-defined]
        f"size {_n(p.width)} x {_n(p.length)} {tag}"  # type: ignore[attr-defined]
    )


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

    def keep(objs: list) -> list:
        return objs if flatten else [o for o in objs if id(o) not in stamped]

    out: list[str] = []
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
        # `ceiling` is host-only (a part borrows the host's); every other header
        # line below is already guarded by a field a part never sets, so this is
        # the only unconditional one to skip in fragment mode.
        out.append(f"ceiling {_n(plan.ceiling_height)}")
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
    if getattr(plan, "siding", None) or getattr(plan, "roofing", None):
        line = "finish"
        if plan.siding:
            line += f" siding {_q(plan.siding)}"
        if plan.roofing:
            line += f" roof {_q(plan.roofing)}"
        out.append(line)
    if getattr(plan, "overhang", 0.0):
        out.append(f"overhang {_n(plan.overhang)}")
    if getattr(plan, "climate", None) is not None:
        out.append(f"climate {plan.climate}")
    if getattr(plan, "roof_style", "gable") != "gable" or getattr(plan, "roof_pitch", None):
        line = f"roof {getattr(plan, 'roof_style', 'gable')}"
        pitch = getattr(plan, "roof_pitch", None)
        if pitch:
            line += f" pitch {_n(pitch)}"
        out.append(line)
    ss = getattr(plan, "site_spec", None)
    if ss is not None and ss.has_dims:
        out.append(f"site {_n(ss.width)} x {_n(ss.length)}")
    if ss is not None and ss.has_setback:
        line = "setback"
        if ss.front is not None:
            line += f" front {_n(ss.front)}"
        if ss.side is not None:
            line += f" side {_n(ss.side)}"
        if ss.rear is not None:
            line += f" rear {_n(ss.rear)}"
        out.append(line)
    if ss is not None and ss.has_building:
        out.append(f"building at {_n(ss.building_x)},{_n(ss.building_y)}")
    if plan.grade is not None:
        out.append(f"grade {_n(plan.grade)}")
    if ss is not None:
        _side_letter = {"north": "N", "south": "S", "east": "E", "west": "W"}
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
            out.append(f"service {sv.utility} from {_side_letter[sv.side.value]}")
    if plan.program_spec is not None:
        spec = plan.program_spec
        line = f"program {spec.beds} bed"
        if spec.baths is not None:
            line += f" {spec.baths} bath"
        for rtype, n in spec.required.items():
            line += f" {n} {rtype.value}"
        if spec.min_area is not None:
            line += f" area {_n(spec.min_area)}"
        if spec.min_storage is not None:
            line += f" storage {_n(spec.min_storage)}"
        out.append(line)
    for req in getattr(plan, "requirements", None) or []:
        # Declared spatial intent rides next to `program` — the plan's contract
        # block, ahead of the geometry it constrains.
        if req.kind in ("adjacent", "separate"):
            out.append(f"require {req.kind} {req.a} {req.b}")
        elif req.kind == "exterior":
            line = f"require exterior {req.a}"
            if req.wall is not None:
                line += f" {req.wall.value}"
            out.append(line)
        else:  # area
            out.append(f"require area {req.a} >= {_n(req.min_area)}")
    for ws in keep(getattr(plan, "wall_specs", None) or []):
        # Declared wall attributes sit in the same contract block; attributes
        # are stored in canonical order, so this is already deterministic.
        out.append(f"wall {ws.room_a} - {ws.room_b} {' '.join(ws.attributes)}")
    for s in keep(getattr(plan, "suites", None) or []):
        # Declared groupings ride the contract block, in declaration order.
        out.append(f"suite {s.id}: {' '.join(s.members)}")
    for z in keep(getattr(plan, "zones", None) or []):
        out.append(f"zone {z.id}: {' '.join(z.members)}")
    for note in (plan.notes or "").splitlines():
        if note.strip():
            out.append(f"note {_q(note.strip())}")
    for nm in keep(getattr(plan, "note_marks", None) or []):
        line = f"note {_q(nm.text)} at {_n(nm.x)},{_n(nm.y)}"
        if nm.level:
            line += f" level {nm.level}"
        out.append(line)
    if plan.frame_spec is not None:
        fs = plan.frame_spec
        # `post` is stored in feet; emit it back in inches (how it's authored).
        line = f"frame bay {_n(fs.bay)} span {_n(fs.span)} post {_n(fs.post * 12)}"
        if not fs.ridge:
            line += " no-ridge"
        out.append(line)

    if not flatten and plan.uses:
        # Cross-file composition — emit the `use` lines verbatim; the stamped
        # elements they pull in are skipped below (see `keep`).
        out.append("")
        for u in plan.uses:
            line = f"use {_q(u.relpath)} as {u.alias} at {_n(u.x)},{_n(u.y)}"
            if u.level:
                line += f" level {u.level}"
            if getattr(u, "mirror", None):
                line += f" mirror {u.mirror}"
            if getattr(u, "rotate", 0):
                line += f" rotate {u.rotate}"
            uparams = getattr(u, "params", None)
            if uparams:
                # Emit exactly the pairs the author passed, in source order (Phase
                # 20). ft-in values canonicalize to decimal feet; recompiling +
                # re-emitting is a fixpoint.
                pairs = ", ".join(f"{k}={_n(v)}" for k, v in uparams.items())
                line += f" with {pairs}"
            out.append(line)

    if keep(plan.rooms):
        out.append("")
        for r in keep(plan.rooms):
            line = (
                f"room {r.id}: {r.type.value} at {_n(r.x)},{_n(r.y)} "
                f"size {_n(r.width)} x {_n(r.length)}"
            )
            if getattr(r, "level", 0):
                line += f" level {r.level}"
            ceiling_height = getattr(r, "ceiling_height", None)
            if ceiling_height is not None:
                line += f" ceiling {_n(ceiling_height)}"
            if getattr(r, "vaulted", False):
                line += " vaulted"
            out.append(line)

    if keep(plan.interior_doors):
        out.append("")
        for d in keep(plan.interior_doors):
            kind = getattr(d, "kind", "swing" if getattr(d, "leaf", True) else "cased")
            if kind == "cased":
                # Emit the terse `open` shorthand for a cased opening.
                line = f"open {d.room_a} - {d.room_b} width {_n(d.width)}"
            else:
                line = f"door {d.room_a} - {d.room_b}"
                if kind != "swing":  # name pocket/sliding; swing is the default
                    line += f" {kind}"
                line += f" width {_n(d.width)}"
            if d.offset is not None:
                line += f" offset {_n(d.offset)}"
            if getattr(d, "swing_into", None) is not None:
                line += f" into {d.swing_into}"
            if getattr(d, "hinge", None) is not None:
                line += f" hinge {d.hinge}"
            out.append(line)

    if keep(plan.exterior_doors):
        out.append("")
        for xd in keep(plan.exterior_doors):
            if getattr(xd, "kind", "entry") == "overhead":
                # An overhead door has no egress flag (no-egress is implied);
                # height is always emitted (7 is the stock default).
                h = xd.height if xd.height is not None else OVERHEAD_DOOR_HEIGHT
                out.append(
                    f"door {xd.room} {xd.wall.value} overhead width {_n(xd.width)} "
                    f"height {_n(h)} offset {_n(xd.offset)}"
                )
                continue
            line = f"entry {xd.room} {xd.wall.value}"
            if getattr(xd, "kind", "entry") in ("double", "french"):
                line += f" {xd.kind}"  # a pair of half-width leaves
            line += f" width {_n(xd.width)} offset {_n(xd.offset)}"
            if not xd.egress:
                line += " no-egress"
            out.append(line)

    if keep(plan.windows):
        out.append("")
        for w in keep(plan.windows):
            line = f"window {w.room} {w.wall.value}"
            if getattr(w, "kind", "casement") != "casement":
                line += f" {w.kind}"  # the kind rides right after the wall
            line += f" width {_n(w.width)} offset {_n(w.offset)}"
            # Only emit sill/head when they differ from the defaults, to keep the
            # common case terse while round-tripping a custom (e.g. transom) window.
            if abs(w.sill_height - 3.0) > 1e-6:
                line += f" sill {_n(w.sill_height)}"
            if abs(w.head_height - 6.67) > 1e-6:
                line += f" head {_n(w.head_height)}"
            if getattr(w, "tempered", False):
                line += " tempered"  # declared safety glazing (R308.4 escape hatch)
            out.append(line)

    if keep(plan.fixtures):
        # Author-placed fixtures only. Auto-seeds (bath/kitchen/laundry footprints
        # the layout derives) are never stored in `plan.fixtures`, so they never
        # reach here — the emitted source carries exactly what the author wrote.
        out.append("")
        for f in keep(plan.fixtures):
            out.append(_fixture_line(f))

    if keep(plan.porches):
        out.append("")
        for p in keep(plan.porches):
            tag = "covered" if p.covered else "open"
            out.append(
                f"porch {p.id} at {_n(p.x)},{_n(p.y)} "
                f"size {_n(p.width)} x {_n(p.length)} {tag}"
            )

    if keep(plan.stairs):
        out.append("")
        for s in keep(plan.stairs):
            out.append(
                f"stair {s.id} at {_n(s.x)},{_n(s.y)} "
                f"size {_n(s.width)} x {_n(s.length)} from {s.from_level} to {s.to_level}"
            )

    outlets, switches, lights = keep(plan.outlets), keep(plan.switches), keep(plan.lights)
    if outlets or switches or lights:
        out.append("")
        for o in outlets:
            line = f"outlet in {o.room} wall {o.wall.value[0].upper()} offset {_n(o.offset)}"
            if o.gfci:
                line += " gfci"
            out.append(line)
        for sw in switches:
            out.append(
                f"switch in {sw.room} wall {sw.wall.value[0].upper()} offset {_n(sw.offset)}"
            )
        for lt in lights:
            line = f"light in {lt.room} at {_n(lt.x)},{_n(lt.y)}"
            if lt.kind != "ceiling":
                line += f" kind {lt.kind}"
            out.append(line)

    if keep(plan.alarms):
        out.append("")
        for a in keep(plan.alarms):
            line = f"alarm {a.kind} in {a.room}"
            if a.x is not None and a.y is not None:
                line += f" at {_n(a.x)},{_n(a.y)}"
            out.append(line)

    return "\n".join(out) + "\n"
