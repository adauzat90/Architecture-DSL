"""Serialize a :class:`~barndsl.elements.Barndominium` back to DSL source.

The inverse of :func:`barndsl.compiler.compile_source`. Round-trips: compiling
the emitted text reproduces an equivalent plan.
"""

from __future__ import annotations

from .elements import Barndominium


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
    for note in (plan.notes or "").splitlines():
        if note.strip():
            out.append(f"note {_q(note.strip())}")

    if plan.rooms:
        out.append("")
        for r in plan.rooms:
            line = (
                f"room {r.id}: {r.type.value} at {_n(r.x)},{_n(r.y)} "
                f"size {_n(r.width)} x {_n(r.length)}"
            )
            if getattr(r, "level", 0):
                line += f" level {r.level}"
            out.append(line)

    if plan.interior_doors:
        out.append("")
        for d in plan.interior_doors:
            kw = "door" if getattr(d, "leaf", True) else "open"
            line = f"{kw} {d.room_a} - {d.room_b} width {_n(d.width)}"
            if getattr(d, "offset", None) is not None:
                line += f" offset {_n(d.offset)}"
            out.append(line)

    if plan.exterior_doors:
        out.append("")
        for d in plan.exterior_doors:
            line = f"entry {d.room} {d.wall.value} width {_n(d.width)} offset {_n(d.offset)}"
            if not d.egress:
                line += " no-egress"
            out.append(line)

    if plan.windows:
        out.append("")
        for w in plan.windows:
            line = f"window {w.room} {w.wall.value} width {_n(w.width)} offset {_n(w.offset)}"
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
