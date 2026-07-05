"""Cross-file composition — the ``use`` loader and stamper (Phase 7a).

A ``use "<relpath>" as <alias> at <x>,<y> [level <n>]`` statement instantiates a
**part** — any ``.barn`` file with no ``plan`` header — into the host plan. This
module owns the compile-time machinery behind that:

* **The resolver** (:func:`_resolve`) is rooted at the top-level file's directory:
  relative paths only, ``realpath`` containment (symlink escapes rejected), a size
  cap, never an OS error — a bad path is a teaching ``USE_UNRESOLVED`` diagnostic.
* **The loader** (:func:`load_part`) fragment-compiles each part **once** per
  top-level compile (memoized by resolved path) into a :class:`PartComponent`:
  its normalized rooms/openings/fixtures/devices and its own *local* diagnostics.
* **The stamper** (:func:`stamp_instance`) transforms every element (translate in
  7a; mirror/rotate arrive in 7b) and prefixes every id **and** every internal
  reference with ``<alias>.``, then appends the copies to the host plan *before*
  validation — so overlap, envelope bounds, egress, adjacency, electrical spacing
  all run on the composed plan with no new code downstream.

Two diagnostic classes fall out (see :func:`compose_uses`): *part-internal*
findings (fire inside the part regardless of placement) are reported **once per
part file**, anchored to the part's own ``file:line``; *instance* findings
(placement-dependent — overlap, out of envelope, egress) fire **per use**,
anchored to the ``use`` line with the alias named.

Translation-only in Phase 7a. ``mirror``/``rotate`` (the §5 remap table) are 7b.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field

from .elements import Barndominium, Instance, UseSpec
from .validation import Issue, Severity

#: Depth-1 in v1: parts can't ``use``. Instance count and file-size guards keep a
#: pathological plan from turning into a resource problem (each is a teaching
#: diagnostic, never a crash — see §8).
MAX_INSTANCES = 64
MAX_PART_BYTES = 256 * 1024

#: Diagnostic codes that are *part-internal*: a finding about a part's own rooms,
#: fixtures, openings or devices that fires **regardless of where the part is
#: placed**. Only these survive a fragment compile as the part's own diagnostics;
#: every other (whole-building / placement-dependent) code is dropped in fragment
#: mode and instead surfaces per-instance on the composed plan. Kept deliberately
#: conservative — a code left out here simply surfaces per-use instead of once,
#: never the reverse (which would wrongly dedupe a real placement issue).
PART_LOCAL_CODES = frozenset({
    "DUP_ID",
    # room geometry (a room's own shape/size — independent of placement)
    "ROOM_SIZE", "ROOM_TIGHT", "ROOM_PROPORTION", "ROOM_GEOMETRY", "ROOM_CLEAR",
    "BEDROOM_AREA", "BEDROOM_DIM", "CLOSET_SHAPE", "BATH_CLEARANCE",
    "BATH_OVERSIZE", "OPEN_BATH", "BATH_DISTANCE",
    # fixtures & furniture clearances (room-local)
    "FIXTURE_TOILET_CLEARANCE", "FIXTURE_FRONT", "FIXTURE_BACKING",
    "FIXTURE_DOOR", "FIXTURE_OVERLAP", "FIXTURE_OOB", "FIXTURE_ROOM",
    "FIXTURE_ROOM_TYPE", "FIXTURE_EGRESS", "BED_CLEARANCE", "DINING_CLEARANCE",
    "KITCHEN_TRIANGLE", "KITCHEN_FIT", "RANGE_LANDING", "DRYER_VENT",
    # openings that clash with the part's own geometry/fixtures
    "OPENING_CLASH", "DOOR_HITS_FIXTURE", "DOOR_SWING_CLASH",
    # room overlaps within the part
    "OVERLAP",
    # devices (room-local rules)
    "OUTLET_SPACING", "OUTLET_GFCI", "ROOM_NO_LIGHT",
})


@dataclass
class PartComponent:
    """A fragment-compiled part, cached once per resolved path.

    :attr:`plan` is the normalized fragment plan (SW corner at 0,0);
    :attr:`diagnostics` are its *local* diagnostics (already anchored to the part's
    own lines, pragmas applied); :attr:`width`/:attr:`length` are its stamped
    bounding-box size; :attr:`has_errors` is True if the part doesn't compile
    cleanly on its own (then it isn't stamped — see :func:`compose_uses`).
    """

    path: str
    relpath: str
    plan: Barndominium
    diagnostics: list[Issue]
    width: float
    length: float
    has_errors: bool


@dataclass
class Composition:
    """The result of stamping every ``use`` — the bookkeeping the host compile
    needs to reclassify the composed plan's diagnostics (see :func:`compose_uses`)."""

    #: Stamped room id -> ``(local_id, Instance)`` (``local_id`` is the id inside
    #: the part, without the ``<alias>.`` prefix).
    stamped_map: dict[str, tuple[str, Instance]] = field(default_factory=dict)
    #: Resolved part path -> the set of ``(code, local_id)`` its fragment compile
    #: reported — the keys a composed stamped-room diagnostic is deduped against.
    part_keys: dict[str, set[tuple[str, str]]] = field(default_factory=dict)


# --- fragment-compile counter (memoization is observable in tests) ------------

#: Incremented on every *actual* fragment compile (a memo hit doesn't bump it), so
#: a test can assert one part compile for N uses of the same file. Reset via
#: :func:`reset_fragment_compiles`.
_fragment_compiles = 0


def reset_fragment_compiles() -> None:
    global _fragment_compiles
    _fragment_compiles = 0


def fragment_compiles() -> int:
    return _fragment_compiles


# --- origin normalization -----------------------------------------------------


def normalize_part_origin(plan: Barndominium) -> tuple[float, float]:
    """Shift a part so its south-west-most room corner sits at ``(0, 0)``.

    Returns the ``(dx, dy)`` applied (``(0, 0)`` when the part already starts at
    the origin). Translates rooms, positioned notes and porches; room-local
    offsets (fixtures, lights, alarms, wall-relative devices/openings) are
    unaffected by definition.
    """
    if not plan.rooms:
        return (0.0, 0.0)
    minx = min(r.x for r in plan.rooms)
    miny = min(r.y for r in plan.rooms)
    if abs(minx) < 1e-9 and abs(miny) < 1e-9:
        return (0.0, 0.0)
    for r in plan.rooms:
        r.x -= minx
        r.y -= miny
    for nm in plan.note_marks:
        nm.x -= minx
        nm.y -= miny
    for p in plan.porches:
        p.x -= minx
        p.y -= miny
    return (minx, miny)


# --- the resolver -------------------------------------------------------------


def _resolve(relpath: str, base_dir: str | None, root: str | None) -> tuple[str | None, str | None]:
    """Resolve ``relpath`` against the sandbox ``root``. Returns
    ``(resolved_path, None)`` or ``(None, reason)`` where ``reason`` is one of
    ``no_base_dir`` / ``absolute`` / ``escape`` / ``missing`` / ``too_big``."""
    if base_dir is None or root is None:
        return None, "no_base_dir"
    if not relpath or os.path.isabs(relpath):
        return None, "absolute"
    candidate = os.path.realpath(os.path.join(root, relpath))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None, "escape"
    if not os.path.isfile(candidate):
        return None, "missing"
    try:
        if os.path.getsize(candidate) > MAX_PART_BYTES:
            return None, "too_big"
    except OSError:
        return None, "missing"
    return candidate, None


_UNRESOLVED_TEXT = {
    "no_base_dir": (
        "can't resolve a part — this source has no home directory to resolve "
        "against (a pasted or browser-opened buffer).",
        "Compile the file from disk, or serve its folder, so `use` paths have a "
        "root to resolve against.",
    ),
    "absolute": (
        "part paths must be relative (resolved against the including file's "
        "directory) — an absolute path isn't allowed.",
        'Write it relative, e.g. `use "parts/bath_core.barn" as b at 0,0`.',
    ),
    "escape": (
        "part path escapes the including file's directory (`..` or a symlink "
        "leaving the root).",
        "Keep parts under the folder you compile/serve; the loader never reads "
        "outside it.",
    ),
    "missing": (
        "no such part file under the including file's directory.",
        "Check the path (relative to the including file), or create the part.",
    ),
    "too_big": (
        f"part file exceeds the {MAX_PART_BYTES // 1024} KiB size limit.",
        "A reusable block should be small — split it, or trim it down.",
    ),
    "cap": (
        f"this plan would stamp more than {MAX_INSTANCES} part instances.",
        f"Composition is capped at {MAX_INSTANCES} instances per plan — inline "
        "some, or reduce the count.",
    ),
}


def _unresolved(relpath: str, reason: str, use: UseSpec) -> Issue:
    body, hint = _UNRESOLVED_TEXT[reason]
    return Issue(
        Severity.ERROR,
        "USE_UNRESOLVED",
        f"use \"{relpath}\" — {body}",
        line=use.line,
        col=use.col,
        end_col=use.end_col,
        hint=hint,
    )


# --- the loader ---------------------------------------------------------------


def load_part(
    use: UseSpec,
    base_dir: str | None,
    root: str | None,
    memo: dict[str, PartComponent],
    profile: object | None,
) -> tuple[PartComponent | None, Issue | None]:
    """Resolve + fragment-compile the part named by ``use`` (memoized).

    Returns ``(component, None)`` on success or ``(None, issue)`` for an
    unresolvable path (a ``USE_UNRESOLVED`` anchored to the ``use`` line). The
    part is compiled at most once per resolved path per top-level compile.
    """
    from .compiler import compile_source

    candidate, reason = _resolve(use.relpath, base_dir, root)
    if reason is not None:
        return None, _unresolved(use.relpath, reason, use)
    assert candidate is not None
    if candidate in memo:
        return memo[candidate], None
    global _fragment_compiles
    try:
        with open(candidate, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, _unresolved(use.relpath, "missing", use)
    _fragment_compiles += 1
    res = compile_source(
        text, name=use.relpath, fragment=True,
        base_dir=os.path.dirname(candidate), profile=profile,  # type: ignore[arg-type]
    )
    plan = res.plan
    width = length = 0.0
    if plan is not None and plan.rooms:
        width = max(r.x + r.width for r in plan.rooms)
        length = max(r.y + r.length for r in plan.rooms)
    has_errors = plan is None or any(d.severity is Severity.ERROR for d in res.diagnostics)
    component = PartComponent(
        candidate, use.relpath, plan if plan is not None else Barndominium(name=use.relpath),
        list(res.diagnostics), width, length, has_errors,
    )
    memo[candidate] = component
    return component, None


# --- the stamper --------------------------------------------------------------


def _pref(alias: str, name: str) -> str:
    return f"{alias}.{name}"


def stamp_instance(plan: Barndominium, component: PartComponent, use: UseSpec) -> Instance:
    """Stamp a transformed, id-prefixed copy of ``component`` into ``plan``.

    Translation-only (7a): every element is shifted by ``use.x, use.y`` (the part
    is normalized to the origin, so that lands its SW corner where the ``use``
    named), lifted to ``use.level``, and has every id **and every internal
    reference** prefixed ``<alias>.``. Appends the copies to ``plan`` and returns
    the :class:`Instance` describing them.
    """
    alias = use.alias
    dx, dy = float(use.x), float(use.y)
    lvl = int(use.level)
    src = component.plan
    objects: list = []
    room_ids: list[str] = []

    for r in src.rooms:
        nr = dataclasses.replace(
            r, id=_pref(alias, r.id), x=r.x + dx, y=r.y + dy,
            level=r.level + lvl, placement=None,
        )
        plan.rooms.append(nr)
        objects.append(nr)
        room_ids.append(nr.id)
    for d in src.interior_doors:
        nd = dataclasses.replace(
            d, room_a=_pref(alias, d.room_a), room_b=_pref(alias, d.room_b),
            swing_into=_pref(alias, d.swing_into) if d.swing_into is not None else None,
            line=None, col=None, end_col=None,
        )
        plan.interior_doors.append(nd)
        objects.append(nd)
    for xd in src.exterior_doors:
        nxd = dataclasses.replace(xd, room=_pref(alias, xd.room), line=None, col=None, end_col=None)
        plan.exterior_doors.append(nxd)
        objects.append(nxd)
    for w in src.windows:
        nw = dataclasses.replace(w, room=_pref(alias, w.room), line=None, col=None, end_col=None)
        plan.windows.append(nw)
        objects.append(nw)
    for f in src.fixtures:
        nf = dataclasses.replace(f, room=_pref(alias, f.room), line=None, col=None, end_col=None)
        plan.fixtures.append(nf)
        objects.append(nf)
    for o in src.outlets:
        no = dataclasses.replace(o, room=_pref(alias, o.room), line=None, col=None, end_col=None)
        plan.outlets.append(no)
        objects.append(no)
    for sw in src.switches:
        nsw = dataclasses.replace(sw, room=_pref(alias, sw.room), line=None, col=None, end_col=None)
        plan.switches.append(nsw)
        objects.append(nsw)
    for lt in src.lights:
        nlt = dataclasses.replace(lt, room=_pref(alias, lt.room), line=None, col=None, end_col=None)
        plan.lights.append(nlt)
        objects.append(nlt)
    for al in src.alarms:
        nal = dataclasses.replace(al, room=_pref(alias, al.room), line=None, col=None, end_col=None)
        plan.alarms.append(nal)
        objects.append(nal)
    for nm in src.note_marks:
        nnm = dataclasses.replace(nm, x=nm.x + dx, y=nm.y + dy, level=nm.level + lvl,
                                  line=None, col=None, end_col=None)
        plan.note_marks.append(nnm)
        objects.append(nnm)
    for p in src.porches:
        npr = dataclasses.replace(p, id=_pref(alias, p.id), x=p.x + dx, y=p.y + dy)
        plan.porches.append(npr)
        objects.append(npr)
    for ws in src.wall_specs:
        nws = dataclasses.replace(ws, room_a=_pref(alias, ws.room_a),
                                  room_b=_pref(alias, ws.room_b), line=None, col=None, end_col=None)
        plan.wall_specs.append(nws)
        objects.append(nws)
    for s in src.suites:
        ns = dataclasses.replace(s, id=_pref(alias, s.id),
                                 members=tuple(_pref(alias, m) for m in s.members),
                                 line=None, col=None, end_col=None)
        plan.suites.append(ns)
        objects.append(ns)
    for z in src.zones:
        nz = dataclasses.replace(z, id=_pref(alias, z.id),
                                 members=tuple(_pref(alias, m) for m in z.members),
                                 line=None, col=None, end_col=None)
        plan.zones.append(nz)
        objects.append(nz)

    inst = Instance(
        alias=alias, relpath=use.relpath, part_path=component.path,
        x=dx, y=dy, level=lvl,
        bbox=(dx, dy, dx + component.width, dy + component.length),
        room_ids=room_ids, objects=objects,
        line=use.line, col=use.col, end_col=use.end_col,
    )
    return inst


# --- the entry point ----------------------------------------------------------


def _prefix_part_internal(iss: Issue, component: PartComponent, use: UseSpec) -> Issue:
    """A copy of a part-internal diagnostic, tagged with the part file and anchored
    (in the host buffer) to the first ``use`` line that pulls the part in."""
    part_line = iss.line
    where = f"{component.relpath}:{part_line}" if part_line else component.relpath
    return dataclasses.replace(
        iss,
        message=f"in part {where} — {iss.message}",
        file=component.path,
        part=component.relpath,
        line=use.line,
        col=use.col,
        end_col=use.end_col,
    )


def compose_uses(
    plan: Barndominium,
    base_dir: str | None,
    diagnostics: list[Issue],
    profile: object | None = None,
) -> Composition:
    """Resolve and stamp every ``use`` on ``plan`` (in place), appending
    resolution errors and (deduped) part-internal diagnostics to ``diagnostics``.

    Called by :func:`barndsl.compiler.compile_source` **before** validation, so the
    composed plan is validated as one building. Returns the :class:`Composition`
    the caller uses to reclassify the composed plan's stamped-room diagnostics.
    """
    root = os.path.realpath(base_dir) if base_dir is not None else None
    memo: dict[str, PartComponent] = {}
    comp = Composition()
    aliases_seen: set[str] = set()
    reported_parts: set[str] = set()
    total = 0

    for use in plan.uses:
        if use.alias in aliases_seen:
            diagnostics.append(Issue(
                Severity.ERROR, "USE_ALIAS_DUP",
                f"alias '{use.alias}' is already used by another `use` statement.",
                line=use.line, col=use.col, end_col=use.end_col,
                hint="Give each instance a unique alias (m, m2, bath_1, ...).",
            ))
            continue
        aliases_seen.add(use.alias)

        component, err = load_part(use, base_dir, root, memo, profile)
        if err is not None:
            diagnostics.append(err)
            continue
        assert component is not None

        # Part-internal diagnostics — reported once per part file, anchored to the
        # (first) use line, tagged with the part path.
        if component.path not in reported_parts:
            reported_parts.add(component.path)
            keys: set[tuple[str, str]] = set()
            for iss in component.diagnostics:
                keys.add((iss.code, iss.room or ""))
                diagnostics.append(_prefix_part_internal(iss, component, use))
            comp.part_keys[component.path] = keys

        if component.has_errors:
            diagnostics.append(Issue(
                Severity.ERROR, "USE_PART_INVALID",
                f"part \"{use.relpath}\" doesn't compile cleanly on its own; "
                f"instance '{use.alias}' can't be stamped.",
                line=use.line, col=use.col, end_col=use.end_col,
                hint="Fix the part-internal error(s) reported above, then re-use it.",
            ))
            continue

        if total >= MAX_INSTANCES:
            diagnostics.append(_unresolved(use.relpath, "cap", use))
            continue

        inst = stamp_instance(plan, component, use)
        plan.instances.append(inst)
        for rid in inst.room_ids:
            plan.stamped_rooms.add(rid)
            local = rid[len(use.alias) + 1:]
            comp.stamped_map[rid] = (local, inst)
        total += 1

    return comp
