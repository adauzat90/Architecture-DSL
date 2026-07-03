"""Serialize a :class:`~barndsl.elements.Barndominium` back to DSL source.

The inverse of :func:`barndsl.compiler.compile_source`. Round-trips: compiling
the emitted text reproduces an equivalent plan.
"""

from __future__ import annotations

from .constants import FLOOR_ASSEMBLY_DEPTH
from .elements import OVERHEAD_DOOR_HEIGHT, Barndominium


def _n(value: float) -> str:
    return f"{value:g}"


def _q(text: str) -> str:
    """Quote a string literal, escaping backslashes and quotes for the lexer."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def emit_dsl(plan: Barndominium) -> str:
    """Return canonical DSL source for ``plan``."""
    out: list[str] = [f"plan {_q(plan.name)}"]
    out.append(f"envelope {_n(plan.envelope_width)} x {_n(plan.envelope_length)}")
    for wing in plan.wings:
        out.append(
            f"wing {_n(wing.width)} x {_n(wing.length)} at {_n(wing.x)},{_n(wing.y)}"
        )
    out.append(f"ceiling {_n(plan.ceiling_height)}")
    if abs(plan.floor_depth - FLOOR_ASSEMBLY_DEPTH) > 1e-9:
        out.append(f"floor {_n(plan.floor_depth)}")
    if plan.accessible:
        out.append("accessible")
    if getattr(plan, "orientation", 0.0):
        out.append(f"orientation {_n(plan.orientation)}")
    if getattr(plan, "siding", None) or getattr(plan, "roofing", None):
        line = "finish"
        if plan.siding:
            line += f" siding {_q(plan.siding)}"
        if plan.roofing:
            line += f" roof {_q(plan.roofing)}"
        out.append(line)
    if getattr(plan, "roof_style", "gable") != "gable" or getattr(plan, "roof_pitch", None):
        line = f"roof {getattr(plan, 'roof_style', 'gable')}"
        rp = getattr(plan, "roof_pitch", None)
        if rp:
            line += f" pitch {_n(rp)}"
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
    if plan.program_spec is not None:
        spec = plan.program_spec
        line = f"program {spec.beds} bed"
        if spec.baths is not None:
            line += f" {spec.baths} bath"
        for rtype, n in spec.required.items():
            line += f" {n} {rtype.value}"
        if spec.min_area is not None:
            line += f" area {_n(spec.min_area)}"
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
    for ws in getattr(plan, "wall_specs", None) or []:
        # Declared wall attributes sit in the same contract block; attributes
        # are stored in canonical order, so this is already deterministic.
        out.append(f"wall {ws.room_a} - {ws.room_b} {' '.join(ws.attributes)}")
    for s in getattr(plan, "suites", None) or []:
        # Declared groupings ride the contract block, in declaration order.
        out.append(f"suite {s.id}: {' '.join(s.members)}")
    for z in getattr(plan, "zones", None) or []:
        out.append(f"zone {z.id}: {' '.join(z.members)}")
    for note in (plan.notes or "").splitlines():
        if note.strip():
            out.append(f"note {_q(note.strip())}")
    if plan.frame_spec is not None:
        fs = plan.frame_spec
        # `post` is stored in feet; emit it back in inches (how it's authored).
        line = f"frame bay {_n(fs.bay)} span {_n(fs.span)} post {_n(fs.post * 12)}"
        if not fs.ridge:
            line += " no-ridge"
        out.append(line)

    if plan.rooms:
        out.append("")
        for r in plan.rooms:
            line = (
                f"room {r.id}: {r.type.value} at {_n(r.x)},{_n(r.y)} "
                f"size {_n(r.width)} x {_n(r.length)}"
            )
            if getattr(r, "level", 0):
                line += f" level {r.level}"
            rch = getattr(r, "ceiling_height", None)
            if rch is not None:
                line += f" ceiling {_n(rch)}"
            if getattr(r, "vaulted", False):
                line += " vaulted"
            out.append(line)

    if plan.interior_doors:
        out.append("")
        for d in plan.interior_doors:
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

    if plan.exterior_doors:
        out.append("")
        for xd in plan.exterior_doors:
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

    if plan.windows:
        out.append("")
        for w in plan.windows:
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
            out.append(line)

    if plan.porches:
        out.append("")
        for p in plan.porches:
            tag = "covered" if p.covered else "open"
            out.append(
                f"porch {p.id} at {_n(p.x)},{_n(p.y)} "
                f"size {_n(p.width)} x {_n(p.length)} {tag}"
            )

    if plan.stairs:
        out.append("")
        for s in plan.stairs:
            out.append(
                f"stair {s.id} at {_n(s.x)},{_n(s.y)} "
                f"size {_n(s.width)} x {_n(s.length)} from {s.from_level} to {s.to_level}"
            )

    return "\n".join(out) + "\n"
