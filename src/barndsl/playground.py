"""A local web playground for barndsl — DSL editor, live diagnostics, 2D/3D views.

``barndsl serve`` starts a small, dependency-free HTTP server (stdlib
:mod:`http.server`) that serves a single-page app and one JSON API. It is a
**local tool**, not a hosted service: it binds ``127.0.0.1`` by default and calls
the installed compiler directly, so there is no Pyodide, no CDN, and it works
fully offline. (A static Pyodide build is a possible later deploy target; see the
IDEAS follow-ups.)

The shell is Tiers 2–3 of ``docs/design/AGENT_FIRST_APP.md``: an agent chat pane
on the left, the DSL editor with inline diagnostics in the middle, the viewport
on the right (2D plan, the shared inline WebGL 3D renderer, elevations + section).

Routes (the *only* routes; there is no static-file serving or directory listing):

``GET /``
    the single-page app (HTML/CSS/JS, all inline, no external references).
``POST /api/compile``
    body ``{"source": "..."}`` (capped at 1 MB) → a JSON compile result:
    ``ok``, ``counts``, ``diagnostics`` (mirroring ``CompileResult.to_dict``),
    and — when the source built a plan — ``svg``, ``scene`` (the same blob the
    single-file viewer embeds), ``score``, ``metrics``, ``elevations``,
    ``section`` and ``report`` (the cost / schedules / areas / climate data the
    Report tab renders). Bad DSL is a normal ``200`` response with diagnostics,
    never a ``500``; only malformed/oversize JSON is ``400``.
``GET /api/examples``
    the bundled ``examples/*.barn`` (and ``examples/gallery/*.barn``) as
    ``[{"name", "source"}]`` for the load-example menu.
``GET /api/reference``
    the DSL grammar reference (:data:`~barndsl.compiler.DSL_REFERENCE`) for the
    help panel.
``GET /api/agent``
    ``{available, reason}`` — whether the Claude agent can run here
    (:func:`barndsl.agent.agent_availability`: ``anthropic`` importable and
    ``ANTHROPIC_API_KEY`` set). The key's value is never read or returned; only
    its presence is probed. The SPA lights up (or disables, with the reason) the
    chat pane from this.
``POST /api/design``
    body ``{"brief": "...", "source"?: "...", "iterations"?: N}`` → a
    **Server-Sent Events** stream of the ``agent.py`` compile-critique-revise
    loop: ``status`` phase updates, one ``iteration`` per round (round, score,
    counts and the FULL :func:`compile_payload` so the viewport evolves live),
    a final ``done`` (the best-scoring iteration, not the last), or a structured
    ``error`` (``kind`` ∈ unavailable/missing_dependency/api_error/cancelled).
    ``source`` seeds a refinement of the current plan; one job at a time (409).
``POST /api/design/cancel``
    body ``{"id": "<job>"}`` → set the running job's cancel flag; the loop stops
    between rounds and the stream ends with a ``cancelled`` error.
``POST /api/edit``
    body ``{"source": "...", "edit": {...}}`` → apply one surgical DSL text edit
    (:mod:`barndsl.edits`) and return
    ``{source, line, changed, ...compile_payload(new_source)}``. The edit
    vocabulary spans the viewport gestures (move/resize a room, slide an opening or
    fixture, materialise a seed) and the graphical design panel's form controls:
    ``set_room_type``, ``rename_room``, ``add_room`` / ``delete_room``,
    ``add_opening`` / ``delete_opening`` / ``set_opening``, ``delete_fixture`` /
    ``set_fixture`` and ``set_plan`` — each rewriting the fewest bytes it can. A
    refused edit
    (unknown room, malformed) is a normal ``200`` with ``{"error": {kind, message}}``
    — bad edits are ordinary UX, not failures; only malformed/oversize JSON is ``400``.
``POST /api/export``
    body ``{"source": "...", "format": "svg|dxf|glb|ifc|viewer|packet"}`` → the
    compiled artifact as a file download: the right ``Content-Type`` and a
    ``Content-Disposition`` attachment filename derived from the plan name. Binary
    formats (``glb``) stream as bytes. The plan is compiled once and reused; a
    source with errors (or a recovered parse) is refused with a normal ``200`` and
    a typed ``{"error": {kind, message}}`` (matching ``/api/edit``) — the SPA
    disables the menu on a bad compile, so this is a backstop. Unknown format or
    malformed/oversize JSON is ``400``. No exporter is re-implemented: it reuses
    :func:`barndsl.render.render_svg`, :func:`barndsl.dxf.to_dxf`,
    :func:`barndsl.gltf.to_glb`, :func:`barndsl.ifc.to_ifc`,
    :func:`barndsl.viewer.viewer_html` and :func:`barndsl.packet.build_packet`.

The server is stateless apart from a single-job design lock: it writes no files
and holds no session. The frontend keeps the last good render when the current
source is broken.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .compare import compare_plans
from .compiler import DSL_REFERENCE, compile_source
from .compose import (  # scan_parts moved to compose (beside the `use` loader); re-exported here
    MAX_LISTED_PARTS,  # noqa: F401 — re-exported for backward compatibility
    MAX_PART_SNIFF_BYTES,  # noqa: F401 — re-exported for backward compatibility
    scan_parts,
)
from .elements import RoomType
from .cost import estimate_cost
from .dxf import to_dxf
from .edits import EditError, apply_edit, edit_from_json, opening_overlays
from .energy import describe_targets, envelope_targets
from .fixtures import FIXTURES, resolve_room_fixtures
from .gltf import build_scene, to_glb
from .ifc import to_ifc
from .packet import build_packet
from .render import (
    ROOM_COLORS,
    RenderConfig,
    render_site_svg,
    render_svg,
    sheet_scale,
)
from .scaffold import starter_dsl
from .schedule import _schedules
from .score import design_score
from .viewer import RENDERER_JS, _LAYER_LABELS, scene_json, viewer_html
from .views import elevation_svg, section_svg

#: Maximum accepted request body (bytes) for POST /api/compile — a generous cap
#: for hand-written DSL; anything larger is rejected with 400 rather than parsed.
MAX_BODY = 1_000_000

_ELEVATION_SIDES = ("south", "north", "east", "west")

#: Statement heads the compiler's ``_parse_statement`` dispatch recognises — kept
#: in step with that if/elif chain in :mod:`barndsl.compiler`. These are the DSL's
#: line-leading keywords; the editor's syntax highlighter colours them.
_STATEMENT_KEYWORDS = (
    "plan", "envelope", "wing", "ceiling", "floor", "accessible", "electrical",
    "street", "overhang", "climate", "orientation", "finish", "site", "setback",
    "roof", "note", "program", "require", "room", "wall", "suite", "zone", "door",
    "open", "entry", "window", "porch", "stair", "frame", "fixture", "alarm",
    "drive", "walk", "well", "septic", "service", "grade", "param",
)

#: Secondary keywords — placement anchors, opening modifiers and option words that
#: appear mid-statement (from the grammar in :data:`~barndsl.compiler.DSL_REFERENCE`).
#: Highlighted the same muted "keyword" colour as the statement heads.
_MODIFIER_KEYWORDS = (
    "x", "at", "size", "level", "vaulted", "width", "offset", "height", "sill",
    "head", "into", "hinge", "near", "far", "center", "align", "from", "to",
    "exterior", "overhead", "no-egress", "covered", "bay", "span", "post",
    "no-ridge", "pitch", "gable", "shed", "monitor", "siding", "adjacent",
    "separate", "area", "storage", "bed", "bath", "front", "side", "rear",
    "swing", "cased", "pocket", "sliding", "double", "french", "casement",
    "slider", "fixed", "double-hung", "of",
    "east-of", "west-of", "north-of", "south-of",
    "right-of", "left-of", "above-of", "below-of",
    "gravel", "concrete", "asphalt", "gas", "electric", "water", "field",
    "with", "mirror", "rotate",
)


def _highlight_tokens() -> dict:
    """The token vocabulary the editor's syntax highlighter uses.

    ``types`` comes straight from :class:`~barndsl.elements.RoomType` (the real
    source of room-type names) and ``keywords`` from the compiler's statement
    dispatch plus the grammar's modifier words — derived, not re-invented, so the
    highlighting tracks the language rather than drifting from it.

    ``statements`` is the statement-head subset alone (``keywords`` folds heads and
    modifiers together for one colour, but the editor's autocomplete and the
    diagnostics quick-fix need to tell a line-leading keyword from a mid-statement
    modifier). ``fixtures`` is the fixture catalog, so ``fixture <kind>`` can
    autocomplete against the same set the parser validates.
    """
    return {
        "keywords": sorted(set(_STATEMENT_KEYWORDS) | set(_MODIFIER_KEYWORDS)),
        "types": [t.value for t in RoomType],
        "statements": sorted(_STATEMENT_KEYWORDS),
        "fixtures": sorted(FIXTURES),
    }


# --- example / starter source ------------------------------------------------


def _examples_dir() -> str:
    """The repo's ``examples/`` directory (three levels up from this package)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(repo_root, "examples")


def load_examples() -> list[dict]:
    """Return the bundled examples as ``[{"name", "source"}]`` (sorted, gallery last).

    Names are ``<file>.barn`` and ``gallery/<file>.barn``. Missing/unreadable
    files are skipped, so a trimmed install (no ``examples/``) just yields ``[]``.
    """
    base = _examples_dir()
    entries: list[tuple[str, str]] = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if name.endswith(".barn"):
                entries.append((name, os.path.join(base, name)))
        gallery = os.path.join(base, "gallery")
        if os.path.isdir(gallery):
            for name in sorted(os.listdir(gallery)):
                if name.endswith(".barn"):
                    entries.append((f"gallery/{name}", os.path.join(gallery, name)))
    out: list[dict] = []
    for label, path in entries:
        try:
            with open(path, encoding="utf-8") as fh:
                out.append({"name": label, "source": fh.read()})
        except OSError:
            continue
    return out


def default_source() -> str:
    """Starter DSL to preload a fresh (no-file) session: the known-clean scaffold.

    A first-ever visitor lands on the reassuring 0/0/0 starter — the same plan the
    "New" button loads — rather than a rich example carrying diagnostics. The
    Cedar Ridge example stays one click away in Load example (it's still bundled in
    :func:`load_examples`); this only changes what an empty editor opens with."""
    from .scaffold import starter_dsl

    return starter_dsl("My Barndo")


# --- offline auto-layout (POST /api/layout) ----------------------------------
#
# The browser's Design panel has an offline, rule-based path that needs no API
# key: a small form (bedrooms/bathrooms/envelope/extra rooms/open-kitchen) is
# turned into an adjacency brief and run through the same space-filling solver the
# CLI's ``barndsl layout`` uses. The result is DSL the editor loads as one
# undoable checkpoint. All inputs are clamped and the room program is bounded, so
# a hostile payload can't blow up the (localhost-only) solve.

#: Wall-clock budget (seconds) for one offline solve — a hard deadline so a
#: pathological program can't wedge the request thread.
LAYOUT_DEADLINE = 6.0

#: Extra rooms the offline form offers: checkbox value -> (room type, target area
#: sqft). Interior room types only — the fill engine tiles them into the envelope.
#: A `porch` is an exterior landing, not a fill room, so it's intentionally absent.
_LAYOUT_EXTRAS: dict[str, tuple[str, int]] = {
    "garage": ("garage", 400),
    "shop": ("shop", 300),
    "office": ("office", 120),
    "dining": ("dining", 160),
    "mudroom": ("mudroom", 80),
    "laundry": ("laundry", 60),
}


def _agent_availability() -> tuple[bool, str | None]:
    """Whether the Claude design loop can run, surviving a trimmed install.

    The base package is dependency-free; the agent's libraries (anthropic,
    pydantic) live in the ``agent`` extra, so the import itself may fail — that
    is an ordinary "not available" answer, never a traceback."""
    try:
        from .agent import agent_availability
    except ImportError:
        return False, 'the design agent needs the agent extra — pip install "barndsl[agent]"'
    return agent_availability()


def _clamp_int(value: object, lo: int, hi: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_float(value: object, lo: float, hi: float, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return max(lo, min(hi, f))


def layout_brief_text(
    name: str, beds: int, baths: int, width: float, length: float,
    open_kitchen: bool, extras: list[str],
) -> str:
    """Compose an adjacency brief (the ``barndsl layout`` grammar) from the offline
    Design form's fields. Living + kitchen are always present; a hall spine appears
    once there are two or more private rooms so bedrooms/baths hang off it and get
    doors. Extras attach to a sensible neighbour."""
    rooms: list[tuple[str, str, int, int | None]] = [
        ("living", "living", 360, None),
        ("kitchen", "kitchen", 240, None),
    ]
    for e in extras:
        rtype, area = _LAYOUT_EXTRAS[e]
        rooms.append((e, rtype, area, None))
    private = beds + baths
    hall = private >= 2
    if hall:
        rooms.append(("hall", "hallway", max(96, private * 40), 4))
    bed_ids = [f"bed{i + 1}" for i in range(beds)]
    bath_ids = [f"bath{i + 1}" for i in range(baths)]
    for i, rid in enumerate(bed_ids):
        rooms.append((rid, "bedroom", 176 if i == 0 else 150, None))
    for i, rid in enumerate(bath_ids):
        rooms.append((rid, "bathroom", 84 if i == 0 else 70, None))

    lines = [f'plan "{name}"', f"envelope {width:g} x {length:g}", "ceiling 9", ""]
    for rid, rtype, area, mn in rooms:
        stmt = f"room {rid}: {rtype} area {area}"
        if mn:
            stmt += f" min {mn}"
        lines.append(stmt)
    lines.append("")

    adj: list[tuple[str, ...]] = [("living", "kitchen")]
    if hall:
        adj.append(("living", "hall"))
        privates = bed_ids + bath_ids
        if privates:
            adj.append(tuple(["hall", *privates]))
    else:
        if bed_ids:
            adj.append(("living", bed_ids[0]))
        if bath_ids:
            adj.append((bed_ids[0] if bed_ids else "living", bath_ids[0]))
    for e in extras:
        if e == "garage":
            adj.append(("mudroom" if "mudroom" in extras else "kitchen", "garage"))
        elif e == "shop":
            adj.append(("garage" if "garage" in extras else "living", "shop"))
        elif e == "office":
            adj.append(("hall" if hall else "living", "office"))
        elif e == "dining":
            adj.append(("kitchen", "dining"))
        elif e == "mudroom":
            adj.append(("kitchen", "mudroom"))
        elif e == "laundry":
            adj.append(("hall" if hall else "kitchen", "laundry"))
    for pair in adj:
        lines.append("adjacent " + " ".join(pair))
    lines.append("entry living")
    return "\n".join(lines) + "\n"


def layout_source_from_form(
    name: str, beds: int, baths: int, width: float, length: float,
    open_kitchen: bool, extras: list[str],
) -> str:
    """Solve the offline brief and return emitted ``.barn`` source. Runs the
    space-filling engine (the CLI ``barndsl layout`` default); raises on an
    unsolvable program (caught by the handler and reported friendly)."""
    from .emit import emit_dsl
    from .layout2 import parse_brief2, solve_layout2

    text = layout_brief_text(name, beds, baths, width, length, open_kitchen, extras)
    brief = parse_brief2(text)
    result = solve_layout2(brief, engine="auto")
    return emit_dsl(result.plan)


def _run_with_deadline(fn: Callable[[], Any], deadline: float) -> Any:
    """Run ``fn`` on a daemon thread, returning its result or raising. A run that
    overruns ``deadline`` raises :class:`TimeoutError` (the orphaned thread is a
    daemon and dies with the process — acceptable for a bounded local solve)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # propagated to the caller after the join
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        raise TimeoutError("layout solve exceeded its deadline")
    if "error" in box:
        raise box["error"]
    return box["result"]


# --- the compile endpoint payload --------------------------------------------


def _compile_summary(result: Any) -> dict:
    """A compact compile summary (ok / recovered / severity counts) for one side
    of a comparison — enough for the UI to flag a side that rendered but has
    errors, without shipping the whole diagnostics list."""
    return {
        "ok": result.ok,
        "recovered": result.recovered,
        "counts": {
            "error": len(result.errors),
            "warning": len(result.warnings),
            "info": len(result.infos),
        },
    }


def compile_payload(source: str, base_dir: str | None = None) -> dict:
    """Compile ``source`` and build the JSON the playground returns.

    Always includes ``ok``/``counts``/``diagnostics`` (the same shape as
    :meth:`CompileResult.to_dict`). When the source built a real (non-recovered)
    plan it also carries the render artifacts — ``svg``, ``scene``, ``score``,
    ``metrics``, ``elevations``, ``section``, ``title`` — which are pure
    functions of the plan. A parse-recovered partial plan omits them (the
    frontend keeps its last good render), matching the CLI's build contract.
    Never raises on bad DSL; artifact-build failures are reported as
    ``render_error`` rather than propagating.

    ``base_dir`` is the served file's directory — the root ``use "<relpath>"``
    (cross-file composition) resolves against. ``None`` (a browser-opened buffer
    with no folder) makes any ``use`` a teaching ``USE_UNRESOLVED`` diagnostic.
    """
    result = compile_source(source, base_dir=base_dir)
    payload = result.to_dict()
    payload["recovered"] = result.recovered
    plan = result.plan
    if plan is not None and not result.recovered:
        try:
            payload["title"] = plan.name
            payload["svg"] = render_svg(plan)
            # The electrical layer as a separate SVG variant — the plan toolbar's
            # ⚡ toggle swaps to it without a re-compile (kept offline/in-payload).
            payload["electrical_svg"] = render_svg(
                plan, RenderConfig(show_electrical=True)
            )
            # The face-of-stud dimension variant — the plan toolbar's "Dims"
            # toggle swaps to it without a re-compile (Phase 18, in-payload like
            # the electrical variant; one server-rendered knob, no client math).
            payload["faces_svg"] = render_svg(
                plan, RenderConfig(dim_mode="faces")
            )
            # A schematic site plan, rendered only when a `site` is declared, shown
            # on the Elevations tab beside the elevations.
            if plan.site_spec is not None and plan.site_spec.has_dims:
                payload["site_svg"] = render_site_svg(plan)
            payload["scene"] = scene_json(build_scene(plan))
            payload["score"] = design_score(result).to_dict()
            payload["metrics"] = plan.metrics()
            payload["elevations"] = {
                side: elevation_svg(plan, side) for side in _ELEVATION_SIDES
            }
            payload["section"] = section_svg(plan)
            # Compact overlay data for Tier 5 edit mode — the frontend draws its
            # interactive SVG from these (not the static plan SVG).
            # Which alias each stamped room belongs to (None for a host room) — the
            # panel greys stamped rows and the overlay drags the whole instance.
            room_instance = {
                rid: inst.alias for inst in plan.instances for rid in inst.room_ids
            }
            payload["rooms"] = [
                {
                    "id": r.id, "type": r.type.value,
                    "x": r.x, "y": r.y, "w": r.width, "l": r.length,
                    "level": r.level, "color": ROOM_COLORS.get(r.type, "#f0f0f0"),
                    "line": result.room_lines.get(r.id),
                    "instance": room_instance.get(r.id),
                }
                for r in plan.rooms
            ]
            # Composed part instances (the `use` statements). Each carries the
            # alias, the part file, the `at` corner + level, the stamped bounding
            # box (for the whole-instance drag ghost), its member room ids and the
            # `use` source line — everything the design panel's instance group and
            # inspector, and the add/move/set/delete/inline_use edits, need.
            payload["instances"] = [
                {
                    "alias": inst.alias, "relpath": inst.relpath,
                    "x": inst.x, "y": inst.y, "level": inst.level,
                    "mirror": inst.mirror, "rotate": inst.rotate,
                    "bbox": list(inst.bbox), "rooms": list(inst.room_ids),
                    "line": inst.line,
                }
                for inst in plan.instances
            ]
            payload["openings"] = opening_overlays(plan)
            payload["levels"] = plan.levels()
            # Stair footprints (compact) so the edit overlay can show each stair on
            # both the level it runs from and the level it lands on — the cross-level
            # anchor an architect aligns an upper floor against.
            payload["stairs"] = [
                {
                    "id": s.id, "x": s.x, "y": s.y, "w": s.width, "l": s.length,
                    "from": s.from_level, "to": s.to_level,
                }
                for s in plan.stairs
            ]
            # Fixtures (authored + surviving seeds) as world rects for the edit
            # overlay. ``line`` is the source line of an explicit fixture (null for
            # a seed — a drag on it inserts a `fixture` line instead of rewriting).
            fixtures: list[dict] = []
            for room in plan.rooms:
                for f in resolve_room_fixtures(plan, room):
                    fixtures.append({
                        "id": f.id, "kind": f.kind, "room": room.id, "level": room.level,
                        "x": f.x, "y": f.y, "w": f.width, "l": f.length,
                        "wall": f.wall, "seed": f.seed, "line": f.source_line,
                        "rotate": f.rotation,
                    })
            payload["fixtures"] = fixtures
            # Positioned notes (leader callouts) for the design panel + edit
            # overlay. Keyed by index — the ordinal among positioned notes — which
            # is the `index` the note edit kinds (add/move/set/delete_note) take.
            payload["notes"] = [
                {
                    "index": i, "text": nm.text, "x": nm.x, "y": nm.y,
                    "level": nm.level, "line": nm.line,
                }
                for i, nm in enumerate(plan.note_marks)
            ]
            # Electrical devices (outlets/switches/lights), keyed by index within
            # each list — the `index` the delete_outlet/switch/light edits take.
            payload["electrical"] = {
                "outlets": [
                    {"index": i, "room": o.room, "wall": o.wall.value,
                     "offset": o.offset, "gfci": o.gfci, "line": o.line}
                    for i, o in enumerate(plan.outlets)
                ],
                "switches": [
                    {"index": i, "room": s.room, "wall": s.wall.value,
                     "offset": s.offset, "line": s.line}
                    for i, s in enumerate(plan.switches)
                ],
                "lights": [
                    {"index": i, "room": lt.room, "x": lt.x, "y": lt.y,
                     "kind": lt.kind, "line": lt.line}
                    for i, lt in enumerate(plan.lights)
                ],
                "alarms": [
                    {"index": i, "room": al.room, "kind": al.kind,
                     "x": al.x, "y": al.y, "line": al.line}
                    for i, al in enumerate(plan.alarms)
                ],
            }
            # Print-to-scale: the architectural scale the plan fits Letter at, the
            # physical width to size the embedded SVG, and a scale-bar render — the
            # frontend's Print uses these so the printed sheet is a true-scale
            # drawing (Letter default; the packet export supports Tabloid too).
            ipf, label, css_w = sheet_scale(plan)
            scale_note = f"SCALE: {label} = 1′-0″ (Letter)"
            payload["print"] = {
                "label": label, "sheet": "Letter",
                "css_width_in": round(css_w, 3), "note": scale_note,
            }
            payload["print_svg"] = render_svg(
                plan, RenderConfig(scale_bar=True, scale_note=scale_note)
            )
            # Plan-level settings the design panel's form edits via `set_plan`.
            payload["settings"] = {
                "name": plan.name,
                "envelope": [plan.envelope_width, plan.envelope_length],
                "ceiling": plan.ceiling_height,
            }
            # The Report tab's data — cost, schedules, areas and (if the plan
            # declares one) the climate envelope. Cheap enough to inline: for the
            # gallery plans it adds <1 ms and <8 KB to the compile payload (measured),
            # so it rides along rather than a lazily-fetched second endpoint.
            payload["report"] = report_data(result)
        except Exception as exc:  # a plan that lowers oddly must not 500 the API
            payload["render_error"] = str(exc)
    # The Parts browser (7b): the plan-less `.barn` part files beside the served
    # file (and in a `parts/` subfolder), so the panel can list them and Insert a
    # `use`. A browser-opened buffer has no `base_dir` → an empty list (the panel
    # then shows its teaching sentence).
    payload["parts_available"] = scan_parts(base_dir)
    return payload



# --- the report (cost / schedules / energy / areas) payload ------------------


def report_data(result: Any) -> dict:
    """Structured Report-tab data for a compiled plan, or ``{}`` when there is none.

    Reuses :func:`barndsl.cost.estimate_cost`, the :mod:`barndsl.schedule` row
    builders and the :mod:`barndsl.energy` reference table **verbatim** — no
    pricing or geometry is recomputed here. Returns ``{}`` for a missing or
    parse-recovered plan, and ``{"error": msg}`` (it never raises) if a source
    module fails, so the Report tab degrades to one explanatory line instead of a
    broken table or a 500. ``energy`` is ``None`` unless the plan declares a
    ``climate`` zone — the tab skips the section entirely then.
    """
    plan = getattr(result, "plan", None)
    if plan is None or getattr(result, "recovered", False):
        return {}
    try:
        est = estimate_cost(plan)
        m = plan.metrics()
        schedules = [
            {
                "title": title,
                "count": len(rows),
                "columns": [c.header for c in columns],
                "rows": [[c.get(row) for c in columns] for row in rows],
            }
            for title, columns, rows in _schedules(plan, True, True, True)
        ]
        areas = {
            "rooms": [
                {
                    "id": r.id, "name": r.display_name, "type": r.type.value,
                    "level": r.level, "width": r.width, "length": r.length,
                    "area": round(r.area, 2),
                }
                for r in plan.rooms
            ],
            "total_area": round(sum(r.area for r in plan.rooms), 2),
            "footprint_sqft": m["footprint_sqft"],
            "interior_sqft": m["interior_sqft"],
            "habitable_sqft": m["habitable_sqft"],
        }
        interior = m["interior_sqft"]
        cost_per_sqft = (
            round(est["total"]["expected"] / interior, 2) if interior else None
        )
        energy = None
        if plan.climate is not None:
            zone = int(plan.climate)
            energy = {
                "zone": zone,
                "summary": describe_targets(zone),
                "targets": envelope_targets(zone),
            }
        return {
            "plan": plan.name,
            "cost": est,
            "cost_per_sqft": cost_per_sqft,
            "schedules": schedules,
            "areas": areas,
            "energy": energy,
        }
    except Exception as exc:  # a source module that raises must not break the render
        return {"error": str(exc)}


# --- the export endpoint ------------------------------------------------------

#: The formats ``POST /api/export`` can produce. Order is the SPA menu order.
EXPORT_FORMATS = ("svg", "dxf", "glb", "ifc", "viewer", "packet")


def _plan_slug(name: str) -> str:
    """A filesystem-safe slug for a plan name (mirrors the CLI's ``_slugify``)."""
    slug = "".join(c.lower() if c.isalnum() else "_" for c in (name or "").strip())
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "barndo"


def export_artifact(fmt: str, plan: Any, result: Any = None) -> tuple[bytes, str, str]:
    """Render ``plan`` into ``fmt`` → ``(body_bytes, content_type, filename)``.

    Reuses the standalone exporters verbatim; ``fmt`` must be in
    :data:`EXPORT_FORMATS` (the caller validates). Binary formats return raw
    bytes; text formats are UTF-8 (DXF/AC1015 is ASCII, matching :func:`~barndsl.dxf.save_dxf`).
    ``result`` (the :class:`~barndsl.compiler.CompileResult`) is only needed by the
    ``packet`` format, which binds the diagnostics appendix in too.
    """
    slug = _plan_slug(plan.name)
    if fmt == "svg":
        return render_svg(plan).encode("utf-8"), "image/svg+xml; charset=utf-8", f"{slug}.svg"
    if fmt == "dxf":
        return (to_dxf(plan).encode("ascii", "replace"),
                "application/dxf; charset=ascii", f"{slug}.dxf")
    if fmt == "glb":
        return to_glb(plan), "model/gltf-binary", f"{slug}.glb"
    if fmt == "ifc":
        return to_ifc(plan).encode("utf-8"), "application/x-step; charset=utf-8", f"{slug}.ifc"
    if fmt == "packet":
        # The permit-sketch packet: one self-contained, print-ready HTML page
        # (cover, dimensioned plan, schedules, cost, diagnostics), reusing
        # packet.build_packet verbatim — the dependency-free deliverable.
        return (build_packet(result).encode("utf-8"),
                "text/html; charset=utf-8", f"{slug}-packet.html")
    # viewer: the self-contained single-file 3D viewer ("share with a client").
    return viewer_html(plan).encode("utf-8"), "text/html; charset=utf-8", f"{slug}-3d.html"


# --- the design (agent) endpoint ---------------------------------------------

#: A design job (brief → best plan) as an injectable callable. The default drives
#: :class:`barndsl.agent.BarndoAgent`; tests pass a fake so the endpoint is
#: exercised with no ``anthropic``, no key and no network. It receives the brief
#: plus the refinement seed, the round cap and the three loop hooks, and returns a
#: :class:`~barndsl.agent.DesignResult`.
Designer = Callable[..., Any]


def _default_designer(
    brief: str,
    *,
    seed_source: str | None,
    max_iterations: int,
    on_step: Callable[[Any], None],
    on_phase: Callable[[str, int], None],
    cancel: Callable[[], bool],
) -> Any:
    """Run the real Claude loop. Imported lazily so ``anthropic`` stays optional."""
    from .agent import BarndoAgent

    return BarndoAgent().design(
        brief,
        max_iterations=max_iterations,
        seed_source=seed_source,
        on_step=on_step,
        on_phase=on_phase,
        cancel=cancel,
    )


def _iteration_event(step: Any, rounds: int) -> dict:
    """The ``iteration`` SSE payload for one design step.

    Carries the round number, the step's score (total + counts + components),
    the compact diagnostic counts, and — reusing :func:`compile_payload` — the
    FULL render payload for that round's source, so the frontend drops it into
    the editor and viewport and the user watches the design evolve live.
    """
    payload = compile_payload(step.source)
    counts = payload.get("counts") or {"error": 0, "warning": 0, "info": 0}
    return {
        "round": step.iteration,
        "rounds": rounds,
        "source": step.source,
        "score": step.score.to_dict() if step.score is not None else None,
        "counts": counts,
        "payload": payload,
    }


def _done_event(result: Any) -> dict:
    """The ``done`` SSE payload: the best-scoring iteration and its render."""
    payload = compile_payload(result.source)
    return {
        "round": result.best_iteration,
        "iterations": result.iterations,
        "source": result.source,
        "score": result.score.to_dict(),
        "counts": payload.get("counts") or {"error": 0, "warning": 0, "info": 0},
        "payload": payload,
    }


# --- the HTTP server ---------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Routes the playground endpoints; everything else is 404.

    Reads config (the precomputed app HTML, cached examples) off the owning
    :class:`_PlaygroundServer`. No filesystem paths are ever served.
    """

    server_version = "barndsl-playground"

    #: Returned by :meth:`_read_json_body` when it has already sent a 4xx — the
    #: caller must stop, but ``None`` is a *valid* parsed body (``null``), so the
    #: error path needs a distinct sentinel.
    _BODY_ERROR = object()

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    # -- response helpers --
    def _client_gone(self, exc: BaseException) -> None:
        """A client that closes the socket mid-response must never crash the
        server thread. Mark the connection unreusable and drop it quietly —
        ``log_message`` is the handler's own (silenced) channel, so this is
        logged-and-ignored rather than fatal (BrokenPipeError/ConnectionReset)."""
        self.close_connection = True
        with contextlib.suppress(Exception):
            self.log_message("client disconnected mid-response: %r", exc)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._client_gone(exc)

    def _json(self, obj: object, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, html: str, status: int = 200) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _download(self, body: bytes, ctype: str, filename: str) -> None:
        """Send ``body`` as a file download (attachment ``Content-Disposition``)."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._client_gone(exc)

    # -- routing --
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        server: _PlaygroundServer = self.server  # type: ignore[assignment]
        if path == "/":
            self._html(server.app_html)
        elif path == "/api/examples":
            self._json(server.examples)
        elif path == "/api/reference":
            self._json({"reference": DSL_REFERENCE})
        elif path == "/api/agent":
            if server.designer is not None:  # an injected loop needs no anthropic/key
                self._json({"available": True, "reason": None})
            else:
                available, reason = _agent_availability()
                self._json({"available": available, "reason": reason})
        else:
            self._json({"error": "not found"}, status=404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def _read_json_body(self) -> object:
        """Read and parse the JSON request body, or send a 400 and return the
        :attr:`_BODY_ERROR` sentinel. Enforces the ``MAX_BODY`` cap."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._json({"error": "missing Content-Length"}, status=400)
            return self._BODY_ERROR
        try:
            length = int(raw)
        except ValueError:
            self._json({"error": "bad Content-Length"}, status=400)
            return self._BODY_ERROR
        if length < 0 or length > MAX_BODY:
            # Drain the (bounded) oversize body first so a localhost client sees a
            # clean 400 rather than a broken pipe; skip only absurd declared sizes.
            if 0 <= length <= MAX_BODY * 16:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            self._json({"error": f"request too large (max {MAX_BODY} bytes)"}, status=400)
            return self._BODY_ERROR
        body = self.rfile.read(length)
        try:
            return json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "malformed JSON"}, status=400)
            return self._BODY_ERROR

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in (
            "/api/compile", "/api/edit", "/api/compare", "/api/fmt",
            "/api/export", "/api/design", "/api/design/cancel", "/api/layout",
        ):
            self._json({"error": "not found"}, status=404)
            return
        data = self._read_json_body()
        if data is self._BODY_ERROR:
            return
        if path == "/api/compile":
            self._handle_compile(data)
        elif path == "/api/fmt":
            self._handle_fmt(data)
        elif path == "/api/edit":
            self._handle_edit(data)
        elif path == "/api/compare":
            self._handle_compare(data)
        elif path == "/api/export":
            self._handle_export(data)
        elif path == "/api/layout":
            self._handle_layout(data)
        elif path == "/api/design":
            self._handle_design(data)
        else:
            self._handle_cancel(data)

    def _handle_layout(self, data: object) -> None:
        """Solve the offline Design form into DSL (no API key required).

        The body is ``{bedrooms, bathrooms, width, length, open_kitchen, extras[],
        name?}``. Every number is clamped and the extra-room list is whitelisted, so
        the room program is bounded before it reaches the solver; the solve runs
        under a wall-clock deadline. Success is ``{"source": "<dsl>"}``; a failed or
        timed-out solve is a normal 200 with a typed ``error`` (never a stack trace).
        A non-object body is a 400."""
        if not isinstance(data, dict):
            self._json({"error": 'expected a JSON object of layout fields'}, status=400)
            return
        raw_name = data.get("name")
        name = (
            raw_name.replace('"', "").strip()[:60]
            if isinstance(raw_name, str) and raw_name.replace('"', "").strip()
            else "My Barndo"
        )
        beds = _clamp_int(data.get("bedrooms"), 0, 8, 2)
        baths = _clamp_int(data.get("bathrooms"), 0, 6, 1)
        width = _clamp_float(data.get("width"), 12.0, 200.0, 40.0)
        length = _clamp_float(data.get("length"), 12.0, 200.0, 30.0)
        open_kitchen = bool(data.get("open_kitchen"))
        raw_extras = data.get("extras")
        extras: list[str] = []
        if isinstance(raw_extras, list):
            for e in raw_extras:
                if isinstance(e, str) and e in _LAYOUT_EXTRAS and e not in extras:
                    extras.append(e)
        try:
            source = _run_with_deadline(
                lambda: layout_source_from_form(
                    name, beds, baths, width, length, open_kitchen, extras
                ),
                LAYOUT_DEADLINE,
            )
        except TimeoutError:
            self._json({"error": {
                "kind": "timeout",
                "message": "The rule-based designer took too long — try fewer "
                           "rooms or a larger envelope.",
            }})
            return
        except Exception as exc:  # an unsolvable program — friendly, never a 500
            self._json({"error": {
                "kind": "layout_error",
                "message": f"Could not lay out that program: {exc}",
            }})
            return
        self._json({"source": source})

    def _handle_compile(self, data: object) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("source"), str):
            self._json({"error": 'expected {"source": "<dsl>"}'}, status=400)
            return
        server: _PlaygroundServer = self.server  # type: ignore[assignment]
        try:
            payload = compile_payload(data["source"], base_dir=server.base_dir)
        except Exception as exc:  # a real bug — bad DSL never reaches here
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        self._json(payload)

    def _handle_fmt(self, data: object) -> None:
        """Canonically reformat ``source`` with the comment-preserving normalizer.

        Returns ``{"source": <formatted>, "changed": bool}``. Refuses (a typed
        ``error``, a normal 200) a source that doesn't compile without parse
        errors, mirroring the CLI — fmt must never mask breakage."""
        if not isinstance(data, dict) or not isinstance(data.get("source"), str):
            self._json({"error": 'expected {"source": "<dsl>"}'}, status=400)
            return
        source = data["source"]
        server: _PlaygroundServer = self.server  # type: ignore[assignment]
        try:
            result = compile_source(source, base_dir=server.base_dir)
            if result.plan is None or result.recovered:
                self._json({"error": {
                    "kind": "parse_error",
                    "message": "fix the parse errors before formatting",
                }})
                return
            from .fmt import format_source

            formatted = format_source(source)
        except Exception as exc:  # a real bug — bad DSL never reaches here
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        self._json({"source": formatted, "changed": formatted != source})

    def _handle_edit(self, data: object) -> None:
        """Apply one surgical DSL edit and return the recompiled payload.

        A refused edit is a normal 200 with a typed ``error`` (bad edits are UX,
        not 500s); only a malformed envelope is 400.
        """
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("source"), str)
            or not isinstance(data.get("edit"), dict)
        ):
            self._json({"error": 'expected {"source": "<dsl>", "edit": {...}}'}, status=400)
            return
        edit = edit_from_json(data["edit"])
        if isinstance(edit, EditError):
            self._json({"error": {"kind": edit.kind, "message": edit.message}})
            return
        server: _PlaygroundServer = self.server  # type: ignore[assignment]
        try:
            result = apply_edit(data["source"], edit, base_dir=server.base_dir)
        except Exception as exc:  # a real bug — refused edits return typed errors
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        if result.error is not None:
            self._json({"error": {"kind": result.error.kind, "message": result.error.message}})
            return
        payload = compile_payload(result.source, base_dir=server.base_dir)
        payload["source"] = result.source
        payload["line"] = result.line
        payload["changed"] = result.changed
        payload["summary"] = result.summary
        payload["placed"] = result.placed  # add_room: "auto" | "fallback" | None
        self._json(payload)

    def _handle_compare(self, data: object) -> None:
        """Compile two sources and return their side-by-side comparison.

        The body is ``{"source_a", "source_b"}``. On success the response is
        :func:`barndsl.compare.compare_plans`' dict (``a``/``b`` sides, ``deltas``,
        ``resolved``/``introduced`` diagnostic multisets) with a ``compile`` block
        summarising each side's compile (ok/recovered/counts). A source that can't
        build a plan (a parse failure or a parse-recovered partial) is refused with
        a normal 200 and a typed ``error`` naming the side — comparing against a
        half-parsed plan is meaningless (the same contract the CLI's ``compare``
        keeps). Only a malformed envelope is 400.
        """
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("source_a"), str)
            or not isinstance(data.get("source_b"), str)
        ):
            self._json(
                {"error": 'expected {"source_a": "<dsl>", "source_b": "<dsl>"}'}, status=400
            )
            return
        try:
            result_a = compile_source(data["source_a"])
            result_b = compile_source(data["source_b"])
        except Exception as exc:  # a real bug — bad DSL never reaches here
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        bad = [
            side
            for side, r in (("a", result_a), ("b", result_b))
            if r.plan is None or r.recovered
        ]
        if bad:
            which = " and ".join("scheme " + s.upper() for s in bad)
            self._json({"error": {
                "kind": "compile_error",
                "side": bad[0] if len(bad) == 1 else "both",
                "message": f"{which} does not build a plan — fix its errors to compare",
            }})
            return
        try:
            payload = compare_plans(result_a, result_b, names=("A", "B"))
        except Exception as exc:  # a plan that scores/measures oddly must not 500 the API
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        payload["compile"] = {
            "a": _compile_summary(result_a),
            "b": _compile_summary(result_b),
        }
        self._json(payload)

    def _handle_export(self, data: object) -> None:
        """Compile ``source`` once and stream the requested artifact as a download.

        A source with errors (or a recovered parse) is refused with a normal 200
        and a typed ``error`` (matching :meth:`_handle_edit`) — the SPA gates the
        menu on a clean compile, so this is a backstop, not the primary guard.
        An unknown format or a malformed envelope is 400.
        """
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("source"), str)
            or not isinstance(data.get("format"), str)
        ):
            self._json(
                {"error": 'expected {"source": "<dsl>", "format": "svg|dxf|glb|ifc|viewer"}'},
                status=400,
            )
            return
        fmt = data["format"]
        if fmt not in EXPORT_FORMATS:
            self._json({"error": f"unknown format: {fmt!r}"}, status=400)
            return
        try:
            result = compile_source(data["source"])
        except Exception as exc:  # a real bug — bad DSL never reaches here
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        if result.plan is None or result.recovered or not result.ok:
            self._json({"error": {
                "kind": "compile_error",
                "message": "fix the plan's errors before exporting",
            }})
            return
        try:
            body, ctype, filename = export_artifact(fmt, result.plan, result)
        except Exception as exc:  # an exporter that lowers oddly must not leak a 500-less path
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        self._download(body, ctype, filename)

    # -- the agent (design) endpoint --
    def _handle_cancel(self, data: object) -> None:
        """Set the running design job's cancel flag (if the id matches)."""
        server: _PlaygroundServer = self.server  # type: ignore[assignment]
        job_id = data.get("id") if isinstance(data, dict) else None
        with server.jobs_lock:
            job = server.current_job
            if job is not None and (job_id is None or job_id == job["id"]):
                job["cancel"].set()
                self._json({"cancelled": True, "id": job["id"]})
                return
        self._json({"cancelled": False}, status=404)

    def _handle_design(self, data: object) -> None:
        """Run one design job and stream it as Server-Sent Events.

        Validates the brief, enforces one-job-at-a-time (409), then streams the
        loop: ``status`` phase updates, an ``iteration`` per round, and a final
        ``done`` or a structured ``error``. The agent (and ``anthropic``) is only
        imported here, so importing the playground never needs the extra.
        """
        if not isinstance(data, dict):
            self._json({"error": 'expected {"brief": "<text>"}'}, status=400)
            return
        brief = data.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            self._json({"error": 'expected {"brief": "<text>"}'}, status=400)
            return
        seed = data.get("source")
        if seed is not None and not isinstance(seed, str):
            self._json({"error": '"source" must be a string'}, status=400)
            return
        seed_source = seed or None  # empty editor → a fresh generation, no seed
        # Default round count comes from $BARNDSL_MAX_ITERATIONS (else 3), clamped
        # to the playground's 1..8 safety range; an explicit request value wins.
        # A trimmed install (no agent extra) just uses the default — the SSE
        # handler below reports the loop unavailable before anything runs.
        try:
            from .agent import resolve_max_iterations

            default_rounds = min(8, max(1, resolve_max_iterations()))
        except ImportError:
            default_rounds = 3
        rounds = data.get("iterations", default_rounds)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 8:
            rounds = default_rounds

        server: _PlaygroundServer = self.server  # type: ignore[assignment]
        if not server.design_lock.acquire(blocking=False):
            self._json({"error": "a design job is already running"}, status=409)
            return
        cancel = threading.Event()
        job_id = uuid.uuid4().hex
        with server.jobs_lock:
            server.current_job = {"id": job_id, "cancel": cancel}
        try:
            self._stream_design(server, brief, seed_source, rounds, cancel, job_id)
        finally:
            with server.jobs_lock:
                server.current_job = None
            server.design_lock.release()

    def _open_sse(self) -> None:
        """Send the SSE response headers (200, ``text/event-stream``)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, cancel: threading.Event, event: str, payload: object) -> None:
        """Write one SSE frame; a broken pipe (client gone) sets ``cancel``."""
        frame = f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
        try:
            self.wfile.write(frame)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            cancel.set()  # the client closed the stream — stop between rounds

    def _stream_design(
        self,
        server: "_PlaygroundServer",
        brief: str,
        seed_source: str | None,
        rounds: int,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        if server.designer is not None:  # an injected loop needs no anthropic/key
            available, reason = True, None
        else:
            available, reason = _agent_availability()
        self._open_sse()
        self._sse(cancel, "status", {"job": job_id, "phase": "starting",
                                     "round": 0, "rounds": rounds})
        if not available:
            self._sse(cancel, "error", {"kind": "unavailable", "message": reason})
            return

        def on_phase(phase: str, rnd: int) -> None:
            self._sse(cancel, "status", {"phase": phase, "round": rnd, "rounds": rounds})

        def on_step(step: Any) -> None:
            self._sse(cancel, "iteration", _iteration_event(step, rounds))

        designer: Designer = server.designer or _default_designer
        try:
            result = designer(
                brief,
                seed_source=seed_source,
                max_iterations=rounds,
                on_step=on_step,
                on_phase=on_phase,
                cancel=cancel.is_set,
            )
        except ImportError as exc:  # anthropic vanished between probe and call
            self._sse(cancel, "error", {"kind": "missing_dependency", "message": str(exc)})
            return
        except Exception as exc:  # network / API / model error — never leak a key
            self._sse(cancel, "error", {"kind": "api_error", "message": str(exc)})
            return

        if cancel.is_set():
            self._sse(cancel, "error", {"kind": "cancelled", "message": "design cancelled"})
            return
        self._sse(cancel, "done", _done_event(result))


class _PlaygroundServer(ThreadingHTTPServer):
    """A threaded HTTP server holding the (immutable) app HTML and examples.

    Its only mutable state is the single design job: ``design_lock`` admits one
    ``/api/design`` at a time (compile stays responsive on other threads), and
    ``current_job`` (guarded by ``jobs_lock``) lets ``/api/design/cancel`` reach
    the running job's cancel event. ``designer`` is the injectable loop driver —
    the real Claude agent by default, a fake in tests.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        initial_source: str,
        designer: Designer | None = None,
        from_file: bool = False,
        base_dir: str | None = None,
    ):
        super().__init__(address, _Handler)
        self.initial_source = initial_source
        self.app_html = render_app(initial_source, from_file=from_file)
        self.examples = load_examples()
        self.designer = designer
        self.design_lock = threading.Lock()
        self.jobs_lock = threading.Lock()
        self.current_job: dict | None = None
        #: The served file's directory — the root ``use "<relpath>"`` composition
        #: resolves against (``barndsl serve plan.barn``). ``None`` when no file was
        #: given (a scratch buffer), so any ``use`` is a ``USE_UNRESOLVED`` teacher.
        self.base_dir = base_dir


def make_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    initial_source: str | None = None,
    designer: Designer | None = None,
    from_file: bool = False,
    base_dir: str | None = None,
) -> _PlaygroundServer:
    """Build (but do not start) the playground server bound to ``host:port``.

    ``port=0`` binds an ephemeral port (used by the tests). ``initial_source``
    preloads the editor; ``None`` uses :func:`default_source`. ``from_file`` marks
    that ``initial_source`` came from an explicit ``FILE`` argument, so the SPA
    prefers it over a newer autosaved session (see :func:`render_app`).
    ``designer`` overrides the agent loop driver (tests inject a keyless fake);
    ``None`` uses the real Claude agent, imported lazily only when a design job
    runs. Localhost by default — this is a local tool, so it never binds
    ``0.0.0.0`` implicitly.
    """
    source = initial_source if initial_source is not None else default_source()
    return _PlaygroundServer(
        (host, port), source, designer=designer, from_file=from_file, base_dir=base_dir
    )


def run(
    initial_source: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
    from_file: bool = False,
    base_dir: str | None = None,
) -> int:
    """Start the playground and serve until interrupted. Returns a process code."""
    httpd = make_server(
        host, port, initial_source=initial_source, from_file=from_file, base_dir=base_dir
    )
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"barndsl playground → {url}  (Ctrl-C to stop)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0


# --- the single-page app -----------------------------------------------------


def _js_string(s: str) -> str:
    """A JS string literal for ``s``, safe to inline inside a ``<script>``."""
    return json.dumps(s).replace("</", "<\\/")


def render_app(initial_source: str, from_file: bool = False) -> str:
    """Return the playground SPA HTML with the renderer and starter source inlined.

    Placeholders are filled by :meth:`str.replace` (not ``str.format``) so the
    embedded CSS/JS braces need no escaping. The 3D view embeds the exact same
    :data:`~barndsl.viewer.RENDERER_JS` the single-file viewer uses.

    ``from_file`` records whether ``initial_source`` came from an explicit ``FILE``
    argument (``barndsl serve plan.barn``). The SPA reads it to resolve autosave
    restore: with a file it keeps the file on screen and merely *offers* any newer
    autosaved session (never clobbering it silently); without one it restores the
    autosaved session outright. The scaffold starter is embedded too, so the
    "New plan" button needs no round-trip.
    """
    return (
        _APP_HTML
        .replace("__RENDERER_JS__", RENDERER_JS)
        .replace("__LAYER_LABELS__", json.dumps(_LAYER_LABELS))
        .replace("__INITIAL_SOURCE__", _js_string(initial_source))
        .replace("__INITIAL_FROM_FILE__", "true" if from_file else "false")
        .replace("__SCAFFOLD_SOURCE__", _js_string(starter_dsl("My Barndo")))
        .replace("__HIGHLIGHT__", json.dumps(_highlight_tokens()))
    )


# The SPA: HTML + CSS + JS, entirely self-contained (no external references). A
# raw string so JS escapes like '\n' survive; filled by render_app via replace,
# so the CSS/JS braces are literal (no doubling).
_APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<!-- Touch: page-wide pinch stays enabled (user scaling is never disabled) so the
     report tab can pinch natively; the plan pane's own pinch handler consumes
     gestures over it.
     interactive-widget=resizes-content shrinks the layout (editor pane, flex:1) when
     the on-screen keyboard opens instead of covering the toolbar. -->
<meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-content">
<title>barndsl playground</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark;
    --bg:#eef1f4; --panel:#ffffff; --ink:#1d2530; --muted:#566072; --faint:#8791a1;
    --line:rgba(20,30,50,.12); --accent:#d1873f; --accent2:#2F6FB0;
    --err:#c8452f; --warn:#c98a1e; --info:#2f6fb0; --okc:#2e8b57;
    --editor:#fbfbfa; --gutter:#f0f1f2; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#171b21; --panel:#1e232b; --ink:#e6ebf2; --muted:#9aa4b4;
      --faint:#7a8494; --line:rgba(255,255,255,.10); --editor:#12151a; --gutter:#1a1f26; }
  }
  /* Explicit theme hooks for the header toggle. Auto (no data-theme) leaves the
     media query above in charge; light/dark pin the palette outright — and set
     color-scheme so native controls follow — winning over the media query on
     attribute specificity. Dark mirrors the media block; light mirrors base :root. */
  :root[data-theme="dark"] { color-scheme:dark;
    --bg:#171b21; --panel:#1e232b; --ink:#e6ebf2; --muted:#9aa4b4;
    --faint:#7a8494; --line:rgba(255,255,255,.10); --editor:#12151a; --gutter:#1a1f26; }
  :root[data-theme="light"] { color-scheme:light;
    --bg:#eef1f4; --panel:#ffffff; --ink:#1d2530; --muted:#566072; --faint:#8791a1;
    --line:rgba(20,30,50,.12); --editor:#fbfbfa; --gutter:#f0f1f2; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    background:var(--bg); color:var(--ink); }
  body { display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:14px; padding:9px 16px; flex:none;
    background:var(--panel); border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .brand { font-weight:700; font-size:15px; letter-spacing:.2px; }
  .brand span { color:var(--accent); font-weight:600; }
  #plan-title { font-size:14px; font-weight:600; color:var(--muted); }
  .chip { font-size:12px; font-weight:600; padding:3px 9px; border-radius:20px;
    border:1px solid var(--line); cursor:default; white-space:nowrap; }
  .chip.good { color:var(--okc); } .chip.mid { color:var(--warn); }
  .chip.low { color:var(--err); }
  #metrics { font-size:12.5px; color:var(--muted); }
  .spacer { flex:1; }
  .examples { font-size:12px; color:var(--faint); display:flex; gap:6px; align-items:center; }
  select { font:inherit; font-size:12.5px; padding:4px 8px; border-radius:7px;
    border:1px solid var(--line); background:var(--panel); color:var(--ink); }
  .toolbar { display:flex; gap:6px; align-items:center; }
  .tbtn { font:inherit; font-size:12.5px; font-weight:600; padding:5px 11px; border-radius:7px;
    border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer;
    white-space:nowrap; }
  .tbtn:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
  .tbtn:disabled { opacity:.45; cursor:default; }
  /* export dropdown (viewport header) */
  .menu { position:relative; }
  .menu-list { position:absolute; right:0; top:calc(100% + 4px); z-index:20; min-width:190px;
    background:var(--panel); border:1px solid var(--line); border-radius:9px;
    box-shadow:0 6px 22px rgba(20,30,50,.18); padding:5px; }
  .menu-list[hidden] { display:none; }
  .menu-item { display:block; width:100%; text-align:left; font:inherit; font-size:12.5px;
    padding:7px 10px; border:0; border-radius:6px; background:transparent; color:var(--ink);
    cursor:pointer; }
  .menu-item:hover { background:rgba(127,127,127,.12); }
  .menu-item small { display:block; color:var(--faint); font-size:11px; margin-top:1px; }
  .menu-item .fmt { color:var(--faint); font:11px ui-monospace,Menlo,Consolas,monospace; }
  /* dismissible non-modal notice bar */
  #notice { display:flex; align-items:center; gap:12px; padding:8px 16px; font-size:12.5px;
    background:rgba(209,135,63,.12); border-bottom:1px solid var(--line); color:var(--ink); }
  #notice[hidden] { display:none; }
  #notice .notice-msg { flex:1; min-width:0; }
  #notice button.na { font:inherit; font-size:12px; font-weight:600; padding:4px 11px;
    border-radius:7px; border:1px solid var(--accent); background:transparent; color:var(--accent);
    cursor:pointer; white-space:nowrap; }
  #notice button.na.ghost { border-color:var(--line); color:var(--muted); }
  #notice button.nx { font:inherit; font-size:16px; line-height:1; padding:0 6px; border:0;
    background:transparent; color:var(--muted); cursor:pointer; }
  .drop-hint { position:absolute; inset:0; z-index:9; display:none; align-items:center;
    justify-content:center; background:rgba(209,135,63,.14); border:2px dashed var(--accent);
    color:var(--accent); font-weight:700; font-size:14px; pointer-events:none; }
  .editor-wrap.dragover .drop-hint { display:flex; }
  main { display:flex; flex:1; min-height:0; }
  .agent { width:308px; flex:none; display:flex; flex-direction:column;
    background:var(--panel); border-right:1px solid var(--line); min-width:0;
    transition:width .16s ease; }
  .agent.collapsed { width:38px; }
  .agent.collapsed .thread, .agent.collapsed .composer, .agent.collapsed .agent-title,
  .agent.collapsed .agent-sub { display:none; }
  .agent-head { display:flex; align-items:center; gap:8px; padding:9px 12px;
    border-bottom:1px solid var(--line); }
  .agent.collapsed .agent-head { padding:9px 7px; justify-content:center; }
  .agent-title { font-weight:700; font-size:13px; }
  .agent-title span { color:var(--accent2); }
  .agent-sub { font-size:11px; color:var(--faint); margin-left:auto; }
  #agent-collapse { font:inherit; font-size:15px; line-height:1; cursor:pointer;
    border:1px solid var(--line); background:transparent; color:var(--muted);
    border-radius:7px; width:24px; height:24px; padding:0; flex:none; }
  #agent-collapse:hover { color:var(--ink); }
  .thread { flex:1; overflow:auto; padding:12px; display:flex; flex-direction:column;
    gap:10px; font-size:12.5px; }
  .msg-user, .msg-agent { padding:8px 11px; border-radius:10px; line-height:1.45;
    max-width:100%; word-wrap:break-word; overflow-wrap:anywhere; }
  .msg-user { align-self:flex-end; background:var(--accent2); color:#fff;
    border-bottom-right-radius:3px; }
  .msg-agent { align-self:flex-start; background:rgba(127,127,127,.12);
    border-bottom-left-radius:3px; }
  .msg-agent.err { background:rgba(200,69,47,.14); color:var(--err); }
  .msg-status { align-self:flex-start; font-size:11.5px; color:var(--muted);
    display:flex; align-items:center; gap:7px; }
  .msg-status .spin { width:9px; height:9px; border-radius:50%;
    border:2px solid var(--line); border-top-color:var(--accent); flex:none;
    animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .iter-row { align-self:stretch; display:flex; align-items:center; gap:8px;
    padding:6px 9px; border:1px solid var(--line); border-radius:9px;
    background:var(--panel); font-size:12px; }
  .iter-row .rn { font-weight:600; color:var(--muted); white-space:nowrap; }
  .iter-row .sc { font-weight:700; padding:2px 8px; border-radius:20px;
    border:1px solid var(--line); }
  .iter-row .sc.good { color:var(--okc); } .iter-row .sc.mid { color:var(--warn); }
  .iter-row .sc.low { color:var(--err); }
  .iter-row .ct { color:var(--faint); font:11px ui-monospace,Menlo,Consolas,monospace;
    margin-left:auto; }
  .iter-row.win { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
  .composer { border-top:1px solid var(--line); padding:10px; display:flex;
    flex-direction:column; gap:8px; }
  #brief { width:100%; min-height:58px; max-height:160px; resize:vertical; border:1px
    solid var(--line); border-radius:9px; background:var(--editor); color:var(--ink);
    padding:8px 10px; font:inherit; font-size:12.5px; outline:none; }
  #brief:disabled { opacity:.55; }
  .composer-row { display:flex; gap:8px; }
  .composer-row button { font:inherit; font-size:12.5px; font-weight:600; padding:7px 14px;
    border-radius:8px; border:1px solid var(--line); cursor:pointer; }
  #send-btn { background:var(--accent2); color:#fff; border-color:transparent; flex:1; }
  #send-btn:disabled { opacity:.5; cursor:default; }
  #stop-btn { background:transparent; color:var(--err); border-color:var(--err); }
  .agent-note { font-size:11px; color:var(--faint); line-height:1.4; }
  .agent-note.bad { color:var(--warn); }
  /* Offline (rule-based) Design form — the no-API-key on-ramp. */
  .offline-design { border:1px solid var(--line); border-radius:9px;
    background:var(--editor); padding:2px 4px; }
  .offline-design > summary { cursor:pointer; font-size:11.5px; font-weight:600;
    color:var(--ink); padding:6px 6px; list-style-position:inside; }
  .od-body { padding:2px 6px 8px; display:flex; flex-direction:column; gap:8px; }
  .od-grid { display:grid; grid-template-columns:auto 1fr auto 1fr; gap:6px 8px;
    align-items:center; }
  .od-grid label { color:var(--muted); font-size:11px; white-space:nowrap; }
  .od-grid input { width:100%; font:inherit; font-size:12px; padding:4px 6px;
    border:1px solid var(--line); border-radius:6px; background:var(--panel);
    color:var(--ink); outline:none; }
  .od-grid input:focus { border-color:var(--accent); }
  .od-check { font-size:11.5px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .od-xt { display:flex; flex-wrap:wrap; gap:5px 10px; }
  .od-xt label { font-size:11.5px; color:var(--muted); display:flex; align-items:center; gap:4px; }
  #od-btn { background:var(--okc); color:#fff; border-color:transparent; flex:1; }
  #od-btn:disabled { opacity:.5; cursor:default; }
  .od-or { font-size:11px; font-weight:600; color:var(--faint); text-transform:uppercase;
    letter-spacing:.04em; margin-top:2px; }
  .left { width:36%; min-width:280px; display:flex; flex-direction:column;
    border-right:1px solid var(--line); }
  .right { flex:1; display:flex; flex-direction:column; min-width:0; }
  /* draggable split handles between the three panes (agent | editor | viewport).
     A slim 6px hit target with a centred hairline that warms on hover; the row is
     flex, so a handle just sits between two panes and dragging rewrites the width
     of the pane on its left. The collapsed agent's handle is hidden (it no-ops). */
  .split-h { flex:none; width:6px; align-self:stretch; cursor:col-resize;
    background:transparent; position:relative; z-index:6; touch-action:none; }
  .split-h::before { content:""; position:absolute; top:0; bottom:0; left:2px; right:2px;
    border-radius:2px; background:transparent; transition:background .12s; }
  .split-h:hover::before { background:var(--line); }
  .split-h.dragging::before { background:var(--accent); }
  .agent.collapsed + .split-h { display:none; }

  .editor-wrap { flex:1; display:flex; min-height:0; background:var(--editor);
    position:relative; overflow:hidden; }
  .gutter { width:46px; flex:none; overflow:hidden; background:var(--gutter);
    color:var(--faint); text-align:right; border-right:1px solid var(--line);
    padding:10px 0; font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .gutter .gln { padding:0 8px 0 4px; position:relative; white-space:nowrap; }
  .gutter .dot { position:absolute; left:4px; top:50%; width:7px; height:7px;
    margin-top:-3.5px; border-radius:50%; }
  .gln.has-error .dot { background:var(--err); }
  .gln.has-warning .dot { background:var(--warn); }
  .gln.has-info .dot { background:var(--info); }
  #editor { flex:1; border:0; outline:0; resize:none; background:transparent;
    color:var(--ink); padding:10px 12px;
    font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    white-space:pre; overflow:auto; tab-size:2; }

  .diagnostics { height:34%; min-height:120px; overflow:auto; background:var(--panel);
    border-top:1px solid var(--line); font-size:12.5px;
    /* a finger must still scroll the list — keep vertical panning with the browser */
    touch-action:pan-y; }
  .diag-head { display:flex; gap:8px; align-items:center; padding:7px 12px;
    position:sticky; top:0; background:var(--panel); border-bottom:1px solid var(--line); }
  .count { font-size:11.5px; font-weight:600; padding:2px 8px; border-radius:20px;
    border:1px solid var(--line); }
  .count.error { color:var(--err); } .count.warning { color:var(--warn); }
  .count.info { color:var(--info); } .count.zero { color:var(--faint); opacity:.65; }
  /* count pills double as severity filters — a live one is a button, an active
     one wears the accent, a zero one stays inert */
  .count[data-filter]:not(.zero) { cursor:pointer; }
  .count.active { border-color:var(--accent); background:rgba(209,135,63,.14); color:var(--accent); }
  .count.zero { cursor:default; }
  /* quick-fix Apply button on a diagnostic row (subtle, right-aligned) */
  .diag-row .qfix { align-self:center; justify-self:end; font:inherit; font-size:11px;
    font-weight:600; padding:3px 9px; border-radius:6px; border:1px solid var(--line);
    background:var(--panel); color:var(--muted); cursor:pointer; white-space:nowrap; }
  .diag-row .qfix:hover { border-color:var(--accent); color:var(--accent); }
  .diag-head .ok { color:var(--okc); font-weight:600; margin-left:auto; }
  .diag-empty { padding:14px 12px; color:var(--faint); }
  .diag-row { display:grid; grid-template-columns:64px auto 1fr; gap:8px;
    padding:7px 12px; border-bottom:1px solid var(--line); cursor:pointer; align-items:baseline; }
  .diag-row:hover { background:rgba(127,127,127,.08); }
  .diag-row .sev { font-size:10.5px; font-weight:700; text-transform:uppercase;
    letter-spacing:.4px; }
  .diag-row.sev-error .sev { color:var(--err); }
  .diag-row.sev-warning .sev { color:var(--warn); }
  .diag-row.sev-info .sev { color:var(--info); }
  .diag-row .code { font:11.5px ui-monospace,Menlo,Consolas,monospace; color:var(--muted); }
  .diag-row .loc { font:11px ui-monospace,Menlo,Consolas,monospace; color:var(--faint); }
  .diag-row .msg { color:var(--ink); }
  .diag-row .msg em { color:var(--faint); font-style:normal; }
  .diag-row .acc { display:inline-block; font-size:10px; font-weight:700;
    text-transform:uppercase; letter-spacing:.04em; color:var(--info);
    border:1px solid var(--info); border-radius:3px; padding:0 4px; }
  .diag-row.accepted .code { text-decoration:line-through; }
  .diag-row .hint { display:block; color:var(--faint); font-size:11.5px; margin-top:2px; }

  .tabs { display:flex; gap:2px; padding:6px 10px 0; background:var(--panel);
    border-bottom:1px solid var(--line); }
  .tab { font:inherit; font-size:12.5px; padding:7px 14px; border:0; cursor:pointer;
    background:transparent; color:var(--muted); border-radius:8px 8px 0 0;
    border-bottom:2px solid transparent; }
  .tab.active { color:var(--ink); font-weight:600; border-bottom-color:var(--accent); }
  .tabs .menu { align-self:center; margin-bottom:4px; }
  .viewport { flex:1; position:relative; overflow:hidden; background:var(--bg); }
  .viewport.stale .pane { opacity:.45; filter:saturate(.7); transition:opacity .15s; }
  .pane { position:absolute; inset:0; display:none; }
  .pane.active { display:block; }
  .svgbox { width:100%; height:100%; overflow:auto; display:flex;
    align-items:flex-start; justify-content:center; padding:14px; cursor:grab; }
  #pane-plan .svgbox svg { max-width:none; }
  .svgbox svg { height:auto; }
  #three-canvas { position:absolute; inset:0; width:100%; height:100%; display:block;
    touch-action:none; cursor:grab; }
  #three-canvas:active { cursor:grabbing; }
  #three-panel { position:absolute; top:12px; left:12px; background:var(--panel);
    border:1px solid var(--line); border-radius:10px; padding:9px 11px; font-size:12.5px;
    box-shadow:0 4px 16px rgba(20,30,50,.12); min-width:120px; }
  #three-panel .hd { font-size:10.5px; text-transform:uppercase; letter-spacing:.6px;
    color:var(--faint); margin-bottom:5px; }
  #three-toggles label { display:flex; align-items:center; gap:7px; padding:2px 0;
    cursor:pointer; user-select:none; }
  #three-toggles input { accent-color:var(--accent); }
  /* snapshot pill — bottom-left of the 3D pane, clear of the Walk pill (bottom-right)
     and the Layers panel (top-left). Hidden until a scene is mounted to capture. */
  #snap-btn { position:absolute; bottom:12px; left:14px; z-index:6; }
  #snap-btn[hidden] { display:none; }
  .views-grid { padding:12px; display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .views-grid figure { margin:0; background:var(--panel); border:1px solid var(--line);
    border-radius:10px; overflow:hidden; }
  .views-grid figcaption { font-size:11.5px; color:var(--faint); padding:7px 10px;
    border-bottom:1px solid var(--line); text-transform:uppercase; letter-spacing:.4px; }
  .views-grid .svgbox { height:220px; cursor:default; }

  /* --- Tier 5: edit mode --- */
  #pane-plan.active { display:flex; flex-direction:column; }
  .edit-bar { display:flex; align-items:center; gap:12px; padding:6px 12px; flex:none;
    background:var(--panel); border-bottom:1px solid var(--line); font-size:12.5px; }
  .edit-toggle { display:flex; align-items:center; gap:6px; cursor:pointer; user-select:none;
    font-weight:600; color:var(--muted); }
  .edit-toggle input { accent-color:var(--accent); }
  .edit-bar button { font:inherit; font-size:12px; padding:4px 11px; border-radius:7px;
    border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }
  .edit-bar button:disabled { opacity:.45; cursor:default; }
  .edit-note { font-size:11.5px; color:var(--faint); margin-left:auto; text-align:right; }
  .edit-note.err { color:var(--err); }
  /* multi-select: count indicator + align/distribute toolbar (2+ rooms) */
  .multi-count { font-size:11.5px; font-weight:600; color:var(--accent2); white-space:nowrap; }
  .multi-count:empty { display:none; }
  .align-tools { display:flex; align-items:center; gap:5px; }
  .align-tools[hidden] { display:none; }
  .align-tools button { padding:3px 8px; font-size:11.5px; }
  .ov-multi { fill:none; stroke:#8a5cc0; stroke-width:2.4; stroke-dasharray:4 2.4; pointer-events:none; }
  .plan-row { flex:1; display:flex; min-height:0; }
  .plan-body { position:relative; flex:1; min-height:0; min-width:0; }
  #panel-btn.on, #measure-btn.on, #elec-btn.on { border-color:var(--accent); color:var(--accent); }
  .edit-layer svg.measuring { cursor:crosshair; }
  .ov-measure { stroke:var(--accent); stroke-width:1.6; stroke-dasharray:5 3; }
  .ov-measure-t { fill:var(--accent); font-weight:700;
    paint-order:stroke; stroke:var(--panel); stroke-width:.22em; }

  /* --- design panel: outline + inspector, the no-code face of the DSL --- */
  .design-panel { flex:none; width:252px; overflow-y:auto; overflow-x:hidden;
    background:var(--panel); border-right:1px solid var(--line);
    font-size:12.5px; padding:10px 12px 20px; }
  .design-panel[hidden] { display:none; }
  .design-panel h5 { margin:14px 0 6px; font-size:10.5px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--faint); }
  .design-panel h5:first-child { margin-top:2px; }
  .dp-grid { display:grid; grid-template-columns:auto 1fr 1fr; gap:6px 8px; align-items:center; }
  .dp-grid label { color:var(--muted); font-size:11.5px; white-space:nowrap; }
  .dp-grid .wide { grid-column:2 / 4; }
  .design-panel input, .design-panel select { font:inherit; font-size:12px; width:100%;
    padding:3px 7px; border-radius:6px; border:1px solid var(--line);
    background:var(--editor); color:var(--ink); min-width:0; }
  .design-panel input:focus, .design-panel select:focus { border-color:var(--accent); outline:none; }
  .dp-level { margin:8px 0 3px; font-weight:600; font-size:11px; color:var(--muted);
    display:flex; align-items:center; }
  .dp-row { display:flex; align-items:center; gap:7px; padding:3px 7px; border-radius:6px;
    cursor:pointer; user-select:none; }
  .dp-row:hover { background:var(--bg); }
  .dp-row.sel { background:var(--accent); color:#fff; }
  .dp-row.sel .dp-dim, .dp-row.sel .dp-kind { color:rgba(255,255,255,.8); }
  .dp-row .swatch { width:10px; height:10px; border-radius:3px; flex:none;
    border:1px solid rgba(0,0,0,.25); }
  .dp-row .dp-id { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dp-row .dp-dim { margin-left:auto; color:var(--faint); font-size:11px; white-space:nowrap; }
  .dp-sub { margin-left:18px; }
  .dp-sub .dp-row { padding:2px 7px; font-size:12px; }
  .dp-kind { color:var(--faint); font-size:11px; }
  .dp-seed { opacity:.75; font-style:italic; }
  /* cross-file composition — instance group header + read-only stamped members */
  .dp-inst .dp-id { color:var(--accent); }
  .dp-stamped { opacity:.6; cursor:default; }
  .dp-stamped .dp-id::before { content:'▢ '; color:var(--faint); }
  .dp-btns { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; }
  .design-panel button { font:inherit; font-size:11.5px; padding:4px 9px; border-radius:6px;
    border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }
  .design-panel button:hover { border-color:var(--accent); color:var(--accent); }
  .design-panel button.danger:hover { border-color:var(--err); color:var(--err); }
  /* the variable-length room/opening list scrolls in its own capped region so
     the ＋ Room action row + Add-room form stay reachable on a short viewport */
  .dp-scroll { max-height:34vh; overflow-y:auto; overflow-x:hidden; margin:0 -4px;
    padding:0 4px; }
  /* the primary action row sticks to the panel bottom so it never scrolls away */
  .dp-actions { position:sticky; bottom:0; z-index:2; margin-top:6px;
    padding-top:8px; background:var(--panel);
    border-top:1px solid var(--line); }
  .dp-form { margin-top:8px; padding:8px; border:1px solid var(--line); border-radius:8px;
    background:var(--bg); }
  .dp-note { margin-top:6px; font-size:11px; color:var(--faint); }
  .dp-note.err { color:var(--err); }
  .edit-layer { position:absolute; inset:0; background:var(--bg); overflow:hidden; }
  /* the overlay's pan/pinch transform rides on this inner wrapper (not the <svg>),
     so svgEl.getScreenCTM() — which folds in ancestor CSS transforms — keeps the
     drag math exact under a two-finger zoom. transform-origin at 0,0 matches the
     midpoint math in pinchEdit(). */
  .edit-tf { position:absolute; inset:0; transform-origin:0 0; }
  .edit-layer svg { width:100%; height:100%; display:block; touch-action:none;
    -webkit-user-select:none; user-select:none; }
  .ov-room { cursor:move; }
  .ov-room.ov-stamped { cursor:move; }   /* a stamped member drags the whole instance */
  .ov-open { cursor:grab; }
  .ov-handle { fill:var(--accent2); stroke:#fff; }
  /* an invisible, finger-sized grab halo behind each resize handle on coarse
     pointers — the visible handle stays small, the tap target grows to ~40px
     (same trick the nudge chevrons use). */
  .ov-handle-hit { fill:transparent; }
  /* fixtures/furniture — draggable; a seed is dashed until a drag authors it */
  .ov-fixture { cursor:move; fill:rgba(90,90,90,.08); stroke:#5a5a5a; stroke-width:1; }
  .ov-fixture.seed { fill:rgba(90,90,90,.04); stroke:#9a9a9a; stroke-dasharray:2 2; }
  .ov-fixture:hover { stroke:var(--accent); }
  .ov-fix-t { fill:var(--muted); }
  /* other-level rooms: dimmed, non-interactive outlines you align the floor to */
  .ov-under { fill:none; stroke:var(--faint); stroke-dasharray:3 2; opacity:.5;
    pointer-events:none; }
  .ov-under-t { fill:var(--faint); opacity:.6; pointer-events:none; }
  /* stair footprint — shown on both the run's and the landing's level */
  .ov-stair { fill:rgba(150,130,90,.16); stroke:#9a8c66; stroke-dasharray:2 2;
    pointer-events:none; }
  .ov-stair-t { fill:#8a7f63; pointer-events:none; }
  /* positioned notes — leader callout; the dot is the drag handle */
  .ov-note { cursor:move; fill:#7A6A55; stroke:#fff; stroke-width:.5; }
  .ov-note:hover, .ov-note.sel { fill:var(--accent); }
  .ov-note-lead { stroke:#7A6A55; stroke-width:1; pointer-events:none; }
  .ov-note-t { fill:#7A6A55; font-style:italic; pointer-events:none; }
  .ov-note-t.sel { fill:var(--accent); }
  /* segmented floor switcher (multi-level plans, edit mode only) */
  .level-switch { display:flex; align-items:center; gap:4px; }
  .level-switch[hidden] { display:none; }
  .level-switch .lvl-label { font-size:11px; color:var(--faint); margin-right:1px; }
  .lvl-chip { font:inherit; font-size:11.5px; font-weight:600; padding:3px 10px;
    border-radius:20px; border:1px solid var(--line); background:var(--panel);
    color:var(--muted); cursor:pointer; white-space:nowrap; }
  .lvl-chip:hover:not(.on) { border-color:var(--accent); color:var(--accent); }
  .lvl-chip.on { background:var(--accent); border-color:var(--accent); color:#fff; }
  .h-e, .h-w { cursor:ew-resize; } .h-n, .h-s { cursor:ns-resize; }
  .h-ne, .h-sw { cursor:nesw-resize; } .h-nw, .h-se { cursor:nwse-resize; }
  @keyframes lineflash { from { background:rgba(209,135,63,.55); } to { background:transparent; } }
  .gln.flash { animation:lineflash 1s ease-out; }

  /* --- Report tab --- */
  .tabs #print-btn { align-self:center; margin:0 6px 4px 0; }
  .report-wrap { position:absolute; inset:0; overflow:auto; padding:16px 18px; }
  .rsec { margin:0 0 22px; max-width:920px; }
  .rsec h3 { font-size:11px; text-transform:uppercase; letter-spacing:.6px;
    color:var(--faint); margin:0 0 8px; border-bottom:1px solid var(--line);
    padding-bottom:5px; }
  .rchips { display:flex; gap:7px; flex-wrap:wrap; margin:0 0 10px; }
  .rtab { border-collapse:collapse; width:100%; font-size:12.5px; margin:2px 0 8px; }
  .rtab th, .rtab td { text-align:left; padding:4px 9px; border-bottom:1px solid var(--line); }
  .rtab th { color:var(--muted); font-weight:600; }
  .rtab td.num, .rtab th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .rtab tr.grp td { font-weight:600; color:var(--muted); padding-top:10px; border-bottom:0; }
  .rtab tr.total td { font-weight:700; border-top:2px solid var(--line); border-bottom:0; }
  .rnote { font-size:11.5px; color:var(--faint); font-style:italic; margin:6px 0 0;
    line-height:1.45; max-width:920px; }
  .rempty { color:var(--faint); padding:12px 0; }
  .rmeta { font-size:12.5px; color:var(--muted); margin:0 0 10px; }

  /* --- syntax-highlight overlay (a coloured <pre> behind the textarea) --- */
  .editor-stack { flex:1; position:relative; min-width:0; overflow:hidden; }
  #hl { position:absolute; inset:0; margin:0; overflow:hidden; pointer-events:none;
    padding:10px 12px; color:var(--ink); background:transparent; white-space:pre;
    tab-size:2; font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .editor-stack #editor { position:absolute; inset:0; flex:none; width:100%; height:100%;
    color:transparent; caret-color:var(--ink); background:transparent; }
  .editor-stack #editor::selection { background:rgba(47,111,176,.28); }
  /* muted, professional token palette (both schemes) */
  #hl .c { color:#8a94a4; font-style:italic; }
  #hl .s { color:#2e8b57; }
  #hl .n { color:#b06a2e; }
  #hl .k { color:#2f6fb0; font-weight:600; }
  #hl .t { color:#8a5cc0; }
  #hl .id { color:#1d2530; font-weight:600; }
  @media (prefers-color-scheme: dark) {
    #hl .c { color:#6b7686; } #hl .s { color:#7fc79a; } #hl .n { color:#d99a63; }
    #hl .k { color:#6fa8e0; } #hl .t { color:#c0a0e6; } #hl .id { color:#e6ebf2; }
  }
  :root[data-theme="dark"] #hl .c { color:#6b7686; }
  :root[data-theme="dark"] #hl .s { color:#7fc79a; }
  :root[data-theme="dark"] #hl .n { color:#d99a63; }
  :root[data-theme="dark"] #hl .k { color:#6fa8e0; }
  :root[data-theme="dark"] #hl .t { color:#c0a0e6; }
  :root[data-theme="dark"] #hl .id { color:#e6ebf2; }
  :root[data-theme="light"] #hl .c { color:#8a94a4; }
  :root[data-theme="light"] #hl .s { color:#2e8b57; }
  :root[data-theme="light"] #hl .n { color:#b06a2e; }
  :root[data-theme="light"] #hl .k { color:#2f6fb0; }
  :root[data-theme="light"] #hl .t { color:#8a5cc0; }
  :root[data-theme="light"] #hl .id { color:#1d2530; }
  /* find/replace match highlights, drawn in the mirror behind the caret */
  #hl mark.find { background:rgba(209,135,63,.30); color:inherit; border-radius:2px; }
  #hl mark.find.cur { background:rgba(209,135,63,.62); }

  /* --- editor find/replace bar (pinned top-right of the editor) --- */
  .find-bar { position:absolute; top:6px; right:12px; z-index:16; display:flex;
    flex-direction:column; gap:5px; padding:6px 7px; background:var(--panel);
    border:1px solid var(--line); border-radius:9px; box-shadow:0 4px 16px rgba(20,30,50,.18); }
  .find-bar[hidden] { display:none; }
  .find-row { display:flex; align-items:center; gap:5px; }
  .find-row[hidden] { display:none; }
  .find-bar input { font:inherit; font-size:12.5px; padding:4px 8px; border-radius:6px; width:168px;
    border:1px solid var(--line); background:var(--editor); color:var(--ink); outline:none; }
  .find-bar input:focus { border-color:var(--accent); }
  .find-count { font:11px ui-monospace,Menlo,Consolas,monospace; color:var(--faint);
    min-width:52px; text-align:center; white-space:nowrap; }
  .find-bar button { font:inherit; font-size:12px; font-weight:600; padding:4px 9px; border-radius:6px;
    border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }
  .find-bar button:hover { border-color:var(--accent); color:var(--accent); }
  .find-bar .find-nav { padding:3px 7px; font-size:11px; }
  .find-bar .find-x { border:0; font-size:16px; line-height:1; padding:0 5px; color:var(--muted); }

  /* --- editor autocomplete popup (fixed, positioned at the caret) --- */
  .ac-pop { position:fixed; z-index:60; min-width:132px; max-height:196px; overflow:auto;
    background:var(--panel); border:1px solid var(--line); border-radius:8px;
    box-shadow:0 6px 22px rgba(20,30,50,.22); padding:4px; font-size:12.5px; }
  .ac-pop[hidden] { display:none; }
  .ac-item { padding:4px 9px; border-radius:5px; cursor:pointer; white-space:nowrap;
    font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; color:var(--ink); }
  .ac-item.sel { background:var(--accent); color:#fff; }

  /* --- viewport zoom controls (2D plan + elevation lightbox) --- */
  .zoom-ctl { position:absolute; z-index:8; bottom:12px; right:14px; display:flex;
    align-items:center; gap:2px; padding:3px; background:var(--panel);
    border:1px solid var(--line); border-radius:9px; box-shadow:0 3px 12px rgba(20,30,50,.16);
    font-size:12.5px; user-select:none; }
  .zoom-ctl button { font:inherit; font-size:14px; line-height:1; width:26px; height:24px;
    padding:0; border:0; border-radius:6px; background:transparent; color:var(--ink);
    cursor:pointer; }
  .zoom-ctl button:hover { background:rgba(127,127,127,.14); }
  .zoom-ctl .zpct { min-width:44px; text-align:center; color:var(--muted);
    font-variant-numeric:tabular-nums; }
  .zoom-ctl .zfit { width:auto; padding:0 9px; font-size:12px; font-weight:600; }
  .plan-body .svgbox { overflow:hidden; align-items:flex-start; justify-content:flex-start;
    padding:0;
    /* the JS pan/pinch controller owns every gesture here (overflow is hidden, so
       there is nothing to native-scroll) — hand it all touches, page never fights it */
    touch-action:none; }
  .plan-body .svgbox svg { position:absolute; top:0; left:0; transform-origin:0 0; }
  .lb-body .svgbox { touch-action:none; }   /* elevation lightbox: same JS zoom/pan */

  /* --- live dimension readout chip (edit mode) --- */
  #dim-chip { position:absolute; z-index:12; display:none; pointer-events:none;
    padding:4px 9px; border-radius:7px; font:600 12px/1.2 ui-monospace,Menlo,Consolas,monospace;
    background:rgba(29,37,48,.92); color:#fff; box-shadow:0 3px 12px rgba(20,30,50,.28);
    white-space:nowrap; }
  #dim-chip .delta { color:#f0c088; font-weight:700; margin-left:6px; }

  /* --- score popover (per-category breakdown) --- */
  #score-chip { cursor:pointer; }
  .score-pop { position:absolute; z-index:40; top:44px; left:16px; width:270px;
    background:var(--panel); border:1px solid var(--line); border-radius:11px;
    box-shadow:0 8px 28px rgba(20,30,50,.22); padding:12px 13px; font-size:12px; }
  .score-pop[hidden] { display:none; }
  .score-pop h4 { margin:0 0 9px; font-size:12.5px; }
  .score-pop h4 span { color:var(--muted); font-weight:600; }
  .sp-row { display:grid; grid-template-columns:74px 1fr 34px; gap:8px; align-items:center;
    margin:0 0 6px; }
  .sp-row .lbl { color:var(--muted); text-transform:capitalize; }
  .sp-bar { height:7px; border-radius:4px; background:rgba(127,127,127,.16); overflow:hidden; }
  .sp-bar > span { display:block; height:100%; border-radius:4px; }
  .sp-row .val { text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); }
  .sp-details { margin:8px 0 0; padding-top:8px; border-top:1px solid var(--line);
    color:var(--faint); font-size:11.5px; line-height:1.45; }
  .sp-details div { margin:2px 0; }
  .sp-clean { color:var(--okc); font-weight:600; }

  /* --- help slide-over panel (DSL reference + shortcuts) --- */
  .help-backdrop { position:fixed; inset:0; z-index:50; background:rgba(15,20,30,.34); }
  .help-backdrop[hidden] { display:none; }
  .help-panel { position:fixed; top:0; right:0; z-index:51; width:min(440px,92vw); height:100%;
    background:var(--panel); border-left:1px solid var(--line); box-shadow:-8px 0 30px rgba(20,30,50,.24);
    display:flex; flex-direction:column; transform:translateX(0); }
  .help-panel[hidden] { display:none; }
  .help-head { display:flex; align-items:center; gap:10px; padding:12px 14px;
    border-bottom:1px solid var(--line); }
  .help-head .ht { font-weight:700; font-size:14px; }
  .help-head .hclose { margin-left:auto; font-size:19px; line-height:1; border:0; padding:0 6px;
    background:transparent; color:var(--muted); cursor:pointer; }
  #help-search { width:100%; font:inherit; font-size:12.5px; padding:7px 10px; border-radius:8px;
    border:1px solid var(--line); background:var(--editor); color:var(--ink); outline:none; }
  .help-search-wrap { padding:10px 14px; border-bottom:1px solid var(--line); }
  .help-body { flex:1; overflow:auto; padding:12px 14px; }
  .help-shortcuts { margin:0 0 14px; }
  .help-shortcuts h5, .help-ref h5 { font-size:10.5px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--faint); margin:0 0 7px; }
  .help-shortcuts .sc { display:flex; justify-content:space-between; gap:12px; padding:3px 0;
    font-size:12px; }
  .help-shortcuts kbd { font:11px ui-monospace,Menlo,Consolas,monospace; background:var(--gutter);
    border:1px solid var(--line); border-radius:5px; padding:1px 6px; color:var(--muted); }
  .help-shortcuts .sc-tip { font-size:12px; line-height:1.5; color:var(--muted); padding:3px 0;
    border-top:1px solid var(--line); }
  .help-shortcuts .sc-tip:first-of-type { border-top:none; }
  .help-ref .rline { font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    white-space:pre-wrap; color:var(--ink); padding:1px 0; }
  .help-ref .rline.head { color:var(--accent); font-weight:700; margin-top:12px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    font-size:12.5px; letter-spacing:.2px; }
  .help-ref .rline.hit mark { background:rgba(209,135,63,.35); color:inherit; border-radius:2px; }
  .help-ref .rempty { color:var(--faint); padding:12px 0; }

  /* --- compare modal: scheme A vs B side-by-side --- */
  .cmp-backdrop { position:fixed; inset:0; z-index:52; background:rgba(15,20,30,.42); }
  .cmp-backdrop[hidden] { display:none; }
  .cmp-modal { position:fixed; z-index:53; top:50%; left:50%; transform:translate(-50%,-50%);
    width:min(760px,94vw); max-height:88vh; display:flex; flex-direction:column;
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    box-shadow:0 18px 60px rgba(20,30,50,.34); }
  .cmp-modal[hidden] { display:none; }
  .cmp-head { display:flex; align-items:center; gap:10px; padding:12px 14px;
    border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .cmp-title { font-weight:700; font-size:14px; }
  .cmp-title span { color:var(--faint); font-weight:600; }
  .cmp-actions { display:flex; gap:6px; margin-left:6px; flex-wrap:wrap; }
  .cmp-close { margin-left:auto; font-size:19px; line-height:1; border:0; padding:0 6px;
    background:transparent; color:var(--muted); cursor:pointer; }
  .cmp-body { flex:1; overflow:auto; padding:14px 16px; font-size:12.5px; }
  .cmp-empty { color:var(--muted); line-height:1.6; padding:14px 4px; }
  .cmp-empty strong { color:var(--ink); }
  .cmp-err { color:var(--err); padding:12px 4px; line-height:1.5; }
  .cmp-cols { display:grid; grid-template-columns:1fr auto auto auto; gap:2px 12px;
    align-items:baseline; }
  .cmp-sec { margin:16px 0 6px; font-size:10.5px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--faint); }
  .cmp-sec:first-child { margin-top:2px; }
  .cmp-h { font-weight:700; font-size:12px; padding-bottom:4px; border-bottom:1px solid var(--line); }
  .cmp-h.a, .cmp-h.b { text-align:right; }
  .cmp-h .cap { display:block; font-weight:500; font-size:10.5px; color:var(--faint); }
  .cmp-row { display:contents; }
  .cmp-row > span { padding:3px 0; }
  .cmp-lbl { color:var(--muted); }
  .cmp-va, .cmp-vb, .cmp-vd { text-align:right; font-variant-numeric:tabular-nums; }
  .cmp-vd { color:var(--faint); }
  .cmp-win { color:var(--okc); font-weight:700; }
  .cmp-scorebig { font-size:16px; font-weight:800; }
  .cmp-tag { display:inline-block; padding:2px 8px; border-radius:6px; font-size:11.5px;
    margin:3px 6px 3px 0; }
  .cmp-tag.res { background:rgba(46,139,87,.16); color:var(--okc); }
  .cmp-tag.intro { background:rgba(200,69,47,.16); color:var(--err); }
  .cmp-diag-none { color:var(--faint); padding:4px 0; }

  /* --- elevation lightbox (a zoomable single-view overlay) --- */
  .views-grid figure { cursor:zoom-in; }
  .lightbox { position:fixed; inset:0; z-index:55; background:rgba(15,20,30,.62);
    display:flex; flex-direction:column; }
  .lightbox[hidden] { display:none; }
  .lb-head { display:flex; align-items:center; gap:10px; padding:10px 14px; color:#fff;
    font-size:13px; font-weight:600; }
  .lb-head .lbclose { margin-left:auto; font-size:20px; line-height:1; border:0; padding:0 8px;
    background:transparent; color:#fff; cursor:pointer; }
  .lb-body { flex:1; position:relative; overflow:hidden; margin:0 14px 14px; border-radius:10px;
    background:var(--panel); }
  .lb-body .svgbox { position:absolute; inset:0; overflow:hidden; padding:0;
    align-items:flex-start; justify-content:flex-start; cursor:grab; }
  .lb-body .svgbox svg { position:absolute; top:0; left:0; transform-origin:0 0; }

  /* =====================================================================
     TOUCH / COARSE-POINTER SUPPORT (Phase 21)
     ---------------------------------------------------------------------
     touch-action policy, by surface (why each is what it is):
       .split-h .............. none      pane-resize drag must never scroll
       #three-canvas ......... none      orbit/pan owns every gesture
       .edit-layer svg ....... none      room drag / pinch must never scroll
       .plan-body .svgbox .... none      JS pan+pinch controller owns gestures
       .lb-body .svgbox ...... none      lightbox JS zoom/pan owns gestures
       .diagnostics .......... pan-y     a finger must still scroll the list
       #editor / .report-wrap  (default) native scroll + page pinch stay live
     ---------------------------------------------------------------------
     Coarse pointers (finger): grow hit targets to a ~40px comfortable size
     with padding / min-height only — the desktop (fine-pointer) look never
     changes because these rules are gated behind @media (pointer: coarse).
     ===================================================================== */
  /* overlay nudge chevrons — a touch stand-in for the arrow-key nudge, shown
     only around the selected room on a coarse pointer (drawn in buildOverlay) */
  .ov-nudge { fill:var(--accent2); opacity:.9; cursor:pointer; }
  .ov-nudge-g .hit { fill:transparent; }   /* invisible finger-sized tap halo */
  .ov-nudge-t { fill:#fff; font-weight:700; pointer-events:none; }
  /* measure endpoints — small dots on fine pointers, fat grab circles on coarse */
  .ov-measure-end { fill:var(--accent); stroke:var(--panel); stroke-width:.14em; }

  @media (pointer: coarse){
    /* toolbar / tab / pill buttons: comfortable spacing + tall enough to tap */
    .tbtn, .tab, .edit-bar button, .align-tools button, .lvl-chip,
    .design-panel button, .find-bar button, .menu-list button, .na {
      min-height:40px; padding-top:9px; padding-bottom:9px; }
    .edit-bar { gap:14px; row-gap:8px; flex-wrap:wrap; }
    .toolbar { gap:9px; }
    .tabs { gap:6px; }
    /* the small zoom stepper: bigger keys */
    .zoom-ctl button { width:38px; height:38px; font-size:18px; }
    .zoom-ctl .zfit { width:auto; }
    /* diagnostics + panel rows: a full finger-height strike area */
    .diag-row { padding-top:12px; padding-bottom:12px; }
    .dp-row { padding-top:9px; padding-bottom:9px; }
    /* panel inputs / selects: 40px tall so a fingertip lands cleanly */
    .design-panel input, .design-panel select { min-height:40px; padding:8px 9px; }
    /* the ☰ Design / Edit toggle checkbox: a bigger box */
    .edit-toggle input, #three-toggles input { width:20px; height:20px; }
    /* selection cues can't rely on hover on touch — keep the fixture/note grab
       affordance visible by default (a soft outline), hover just intensifies it */
    .ov-fixture { stroke-width:1.4; }
  }

  /* --- print the current viewport, not the three-pane app chrome --- */
  @media print {
    header, #notice, .agent, .left, .split-h, .tabs, .edit-bar, #three-panel,
    .design-panel, #snap-btn, .drop-hint, .zoom-ctl, #dim-chip, .score-pop, .help-backdrop,
    .help-panel, .lightbox { display:none !important; }
    .plan-body .svgbox { overflow:visible !important; }
    .plan-body .svgbox svg { position:static !important; transform:none !important; }
    html, body { overflow:visible !important; height:auto !important; background:#fff !important; }
    main, .right, .viewport { display:block !important; position:static !important;
      overflow:visible !important; min-height:0 !important; }
    .viewport { background:#fff !important; }
    .pane { display:none !important; position:static !important; }
    .pane.active { display:block !important; position:static !important; }
    #pane-plan.active { display:block !important; }
    .plan-body { position:static !important; }
    .edit-layer { display:none !important; }
    .svgbox { height:auto !important; overflow:visible !important; display:block !important;
      cursor:auto !important; }
    .svgbox svg { transform:none !important; max-width:100% !important; }
    .report-wrap { position:static !important; overflow:visible !important; }
    .views-grid { grid-template-columns:1fr 1fr !important; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">barndsl <span>playground</span></div>
  <div id="plan-title"></div>
  <div id="score-chip" class="chip" title="">score</div>
  <div id="metrics"></div>
  <div class="spacer"></div>
  <div class="toolbar">
    <button class="tbtn" id="new-btn" title="Start a new plan from the scaffold">New</button>
    <button class="tbtn" id="open-btn" title="Open a .barn file (Ctrl/Cmd+O)">Open</button>
    <button class="tbtn" id="save-btn" title="Download the source as .barn (Ctrl/Cmd+S)">Save</button>
    <button class="tbtn" id="fmt-btn" title="Canonically reformat the source — comments &amp; pragmas preserved (Shift+Alt+F)">Format</button>
    <button class="tbtn" id="compare-btn" title="Compare the current plan against a saved baseline (A vs B)">Compare…</button>
    <input type="file" id="file-input" accept=".barn,.txt" hidden>
    <input type="file" id="compare-file-input" accept=".barn,.txt" hidden>
  </div>
  <label class="examples">example
    <select id="example-select"><option value="">loading…</option></select>
  </label>
  <button class="tbtn" id="theme-btn" aria-label="Cycle color theme"
    title="Theme: auto">◐</button>
  <button class="tbtn" id="help-btn" aria-haspopup="dialog"
    title="DSL reference &amp; keyboard shortcuts (?)">?</button>
</header>
<div id="score-pop" class="score-pop" hidden></div>
<div id="help-backdrop" class="help-backdrop" hidden></div>
<aside id="help-panel" class="help-panel" hidden role="dialog" aria-label="Help">
  <div class="help-head">
    <span class="ht">Reference &amp; shortcuts</span>
    <button class="hclose" id="help-close" title="Close (Esc)" aria-label="Close">×</button>
  </div>
  <div class="help-search-wrap">
    <input id="help-search" type="search" placeholder="Filter the DSL reference…"
      autocomplete="off" spellcheck="false">
  </div>
  <div class="help-body">
    <div class="help-shortcuts" id="help-shortcuts"></div>
    <div class="help-ref" id="help-ref"><h5>DSL reference</h5>
      <div id="help-ref-body" class="rempty">loading…</div></div>
  </div>
</aside>
<div id="compare-backdrop" class="cmp-backdrop" hidden></div>
<div id="compare-modal" class="cmp-modal" hidden role="dialog" aria-label="Compare schemes">
  <div class="cmp-head">
    <span class="cmp-title">Compare <span>A vs B</span></span>
    <div class="cmp-actions">
      <button class="tbtn" id="compare-set-a" title="Snapshot the current plan as baseline A">Set baseline A</button>
      <button class="tbtn" id="compare-load-a" title="Load a .barn file as baseline A">Load A…</button>
      <button class="tbtn" id="compare-swap" title="Swap which side is A and which is B" disabled>⇄ Swap</button>
    </div>
    <button class="cmp-close" id="compare-close" title="Close (Esc)" aria-label="Close">×</button>
  </div>
  <div class="cmp-body" id="compare-body"></div>
</div>
<div id="notice" hidden>
  <span class="notice-msg" id="notice-msg"></span>
  <span id="notice-actions"></span>
  <button class="nx" id="notice-dismiss" title="Dismiss" aria-label="Dismiss">×</button>
</div>
<main>
  <section class="agent" id="agent-pane">
    <div class="agent-head">
      <button id="agent-collapse" title="collapse the agent pane">‹</button>
      <div class="agent-title">agent <span>chat</span></div>
      <div class="agent-sub" id="agent-sub"></div>
    </div>
    <div class="thread" id="thread"></div>
    <div class="composer">
      <details class="offline-design" id="offline-design" open>
        <summary>Design (offline — rule-based)</summary>
        <div class="od-body">
          <div class="od-grid">
            <label>bedrooms</label>
            <input type="number" id="od-beds" min="0" max="8" step="1" value="3">
            <label>bathrooms</label>
            <input type="number" id="od-baths" min="0" max="6" step="1" value="2">
            <label>width (ft)</label>
            <input type="number" id="od-w" min="12" max="200" step="1" value="40">
            <label>length (ft)</label>
            <input type="number" id="od-l" min="12" max="200" step="1" value="30">
          </div>
          <label class="od-check"><input type="checkbox" id="od-open" checked> open kitchen / living</label>
          <div class="od-xt" id="od-extras"></div>
          <div class="composer-row">
            <button id="od-btn" title="Lay out a starting plan with no API key">Design offline</button>
          </div>
          <div class="agent-note" id="od-note">No API key needed — a deterministic space-filling layout you can then edit.</div>
        </div>
      </details>
      <div class="od-or">Design with Claude (needs API key)</div>
      <textarea id="brief" spellcheck="false"
        placeholder="Describe the barndo you want — e.g. &quot;3 bed 2 bath, open kitchen, 2-car shop bay, ~1800 sq ft&quot;. Then Design."></textarea>
      <div class="composer-row">
        <button id="send-btn">Design with Claude</button>
        <button id="stop-btn" hidden>Stop</button>
      </div>
      <div class="agent-note" id="agent-note"></div>
    </div>
  </section>
  <div class="split-h" id="split-agent" title="Drag to resize · double-click to reset"></div>
  <section class="left">
    <div class="editor-wrap" id="editor-wrap">
      <div class="gutter" id="gutter"></div>
      <div class="editor-stack">
        <pre id="hl" aria-hidden="true"></pre>
        <textarea id="editor" spellcheck="false" autocapitalize="off"
          autocomplete="off" wrap="off"></textarea>
      </div>
      <div class="find-bar" id="find-bar" hidden>
        <div class="find-row">
          <input id="find-input" type="text" placeholder="Find" autocomplete="off" spellcheck="false">
          <span class="find-count" id="find-count"></span>
          <button class="find-nav" id="find-prev" title="Previous match (Shift+Enter)" aria-label="Previous match">▲</button>
          <button class="find-nav" id="find-next" title="Next match (Enter)" aria-label="Next match">▼</button>
          <button class="find-x" id="find-close" title="Close (Esc)" aria-label="Close find">×</button>
        </div>
        <div class="find-row" id="find-replace-row" hidden>
          <input id="replace-input" type="text" placeholder="Replace" autocomplete="off" spellcheck="false">
          <button class="find-btn" id="replace-one" title="Replace this match">Replace</button>
          <button class="find-btn" id="replace-all" title="Replace all matches">All</button>
        </div>
      </div>
      <div class="drop-hint">Drop a .barn file to open</div>
    </div>
    <div class="diagnostics" id="diagnostics"></div>
  </section>
  <div class="split-h" id="split-editor" title="Drag to resize · double-click to reset"></div>
  <section class="right">
    <div class="tabs">
      <button class="tab active" data-tab="plan">2D plan</button>
      <button class="tab" data-tab="three">3D</button>
      <button class="tab" data-tab="views">Elevations</button>
      <button class="tab" data-tab="report">Report</button>
      <div class="spacer"></div>
      <button class="tbtn" id="print-btn"
        title="Open a print-ready packet — title block, plan, elevations, report">Print</button>
      <div class="menu">
        <button class="tbtn" id="export-btn" aria-haspopup="true" aria-expanded="false">Export ▾</button>
        <div class="menu-list" id="export-menu" hidden>
          <button class="menu-item" data-fmt="barn">Source <span class="fmt">.barn</span></button>
          <button class="menu-item" data-fmt="svg">2D plan <span class="fmt">.svg</span></button>
          <button class="menu-item" data-fmt="dxf">CAD drawing <span class="fmt">.dxf</span></button>
          <button class="menu-item" data-fmt="glb">3D model <span class="fmt">.glb</span></button>
          <button class="menu-item" data-fmt="ifc">BIM model <span class="fmt">.ifc</span></button>
          <button class="menu-item" data-fmt="viewer">3D viewer <span class="fmt">.html</span>
            <small>self-contained — share with a client</small></button>
          <button class="menu-item" data-fmt="packet">Permit packet <span class="fmt">.html</span>
            <small>print-ready — cover, plan, schedules, cost</small></button>
        </div>
      </div>
    </div>
    <div class="viewport" id="viewport">
      <div class="pane active" id="pane-plan">
        <div class="edit-bar">
          <button id="panel-btn" title="Design panel — outline &amp; properties, no code required">☰ Design</button>
          <label class="edit-toggle"><input type="checkbox" id="edit-mode"> Edit layout</label>
          <button id="undo-btn" disabled title="Nothing to undo">↶ Undo</button>
          <button id="redo-btn" disabled title="Nothing to redo">↷ Redo</button>
          <button id="measure-btn" disabled
            title="Measure — drag between two points on the plan (M, edit mode)">⟷ Measure</button>
          <button id="elec-btn"
            title="Electrical layer — show outlets, switches &amp; ceiling lights">⚡ Electrical</button>
          <button id="dims-btn"
            title="Dimension convention — nominal room lines vs face-of-stud">⟺ Dims: nominal</button>
          <span class="level-switch" id="level-switch" hidden></span>
          <span class="multi-count" id="multi-count"></span>
          <span class="align-tools" id="align-tools" hidden>
            <button data-btn="align-left" title="Align left edges (min x)">⇤ Left</button>
            <button data-btn="align-right" title="Align right edges (max x)">Right ⇥</button>
            <button data-btn="align-top" title="Align top edges (max y)">⤒ Top</button>
            <button data-btn="align-bottom" title="Align bottom edges (min y)">⤓ Bottom</button>
            <button data-btn="dist-h" title="Distribute horizontally — equalize gaps">⇹ Dist H</button>
            <button data-btn="dist-v" title="Distribute vertically — equalize gaps">⤨ Dist V</button>
          </span>
          <span class="edit-note" id="edit-note"></span>
        </div>
        <div class="plan-row">
        <aside class="design-panel" id="design-panel" hidden aria-label="Design panel"></aside>
        <div class="plan-body">
          <div class="svgbox" id="plan-svg" tabindex="0" style="outline:none"></div>
          <div class="edit-layer" id="edit-layer" hidden></div>
          <div id="dim-chip"></div>
          <div class="zoom-ctl" id="plan-zoom">
            <button data-z="out" title="Zoom out (−)" aria-label="Zoom out">−</button>
            <span class="zpct" id="plan-zpct">100%</span>
            <button data-z="in" title="Zoom in (+)" aria-label="Zoom in">+</button>
            <button class="zfit" data-z="fit" title="Fit to pane (0)">Fit</button>
          </div>
        </div>
        </div>
      </div>
      <div class="pane" id="pane-three">
        <canvas id="three-canvas"></canvas>
        <div id="three-panel"><div class="hd">Layers</div><div id="three-toggles"></div></div>
        <button class="tbtn" id="snap-btn" hidden
          title="Download this 3D view as a PNG">⤓ PNG</button>
      </div>
      <div class="pane" id="pane-views"></div>
      <div class="pane" id="pane-report"><div class="report-wrap" id="report-wrap"></div></div>
    </div>
  </section>
</main>
<div id="lightbox" class="lightbox" hidden>
  <div class="lb-head">
    <span id="lb-title">Elevation</span>
    <div class="zoom-ctl" id="lb-zoom" style="position:static;box-shadow:none;background:transparent;border:0;">
      <button data-z="out" title="Zoom out (−)" aria-label="Zoom out">−</button>
      <span class="zpct" id="lb-zpct" style="color:#fff;">100%</span>
      <button data-z="in" title="Zoom in (+)" aria-label="Zoom in">+</button>
      <button class="zfit" data-z="fit" title="Fit to view (0)">Fit</button>
    </div>
    <button class="lbclose" id="lb-close" title="Close (Esc)" aria-label="Close">×</button>
  </div>
  <div class="lb-body"><div class="svgbox" id="lb-svg" tabindex="0" style="outline:none"></div></div>
</div>
<div class="ac-pop" id="ac-pop" hidden></div>
<script>__RENDERER_JS__</script>
<script>
const LAYER_LABELS = __LAYER_LABELS__;
const INITIAL_SOURCE = __INITIAL_SOURCE__;
const INITIAL_FROM_FILE = __INITIAL_FROM_FILE__;   // server started with an explicit FILE arg
const SCAFFOLD_SOURCE = __SCAFFOLD_SOURCE__;        // "New plan" starter
const HIGHLIGHT = __HIGHLIGHT__;                    // {keywords, types} for the editor highlighter
const LS_SOURCE = 'barndsl.playground.source';
const LS_SAVED_AT = 'barndsl.playground.savedAt';
const LS_THEME = 'barndsl.playground.theme';        // auto | light | dark
const LS_AGENT_W = 'barndsl.playground.agentWidth';  // split: agent | editor
const LS_EDITOR_W = 'barndsl.playground.editorWidth';
const LS_COMPARE_A = 'barndsl.playground.compareA';  // the snapshotted baseline (scheme A)

const editor = document.getElementById('editor');
const gutter = document.getElementById('gutter');
const hl = document.getElementById('hl');
const diagEl = document.getElementById('diagnostics');
const planSvg = document.getElementById('plan-svg');
const elecBtn = document.getElementById('elec-btn');
let elecMode = false;  // ⚡ toggle: show the electrical layer on the plan SVG
const dimsBtn = document.getElementById('dims-btn');
let dimsMode = 'nominal';  // ⟺ toggle: 'nominal' room lines vs 'faces' (face-of-stud)
// Which baked plan-SVG variant to show: the electrical overlay wins (a distinct
// layer); otherwise the face-of-stud variant when the Dims toggle is on; else the
// default nominal render. All three ride in the payload — no client re-computation.
function planVariant(p){
  if (elecMode && p.electrical_svg) return p.electrical_svg;
  if (dimsMode === 'faces' && p.faces_svg) return p.faces_svg;
  return p.svg;
}
const viewsPane = document.getElementById('pane-views');
const reportWrap = document.getElementById('report-wrap');
const titleEl = document.getElementById('plan-title');
const scoreChip = document.getElementById('score-chip');
const metricsEl = document.getElementById('metrics');
const viewport = document.getElementById('viewport');

let diagnostics = [];
// Editor find/replace state (Feature: Ctrl/Cmd+F). Declared up here because
// renderHighlight reads it on the very first boot render, before the find bar wires.
let findOpen = false, findMatches = [], findIndex = -1;
// Autocomplete popup state (Ctrl/Cmd+Space / type-ahead).
let acOpen = false, acItems = [], acIndex = 0, acWord = null;
// Unified undo/redo history — one linear timeline of {v,s,e,label} snapshots with an
// index pointer (classic undo/redo), covering typing, smart edits and layout drags
// alike. It replaces the old undo-only gesture stack: every programmatic writer routes
// through applyEdit(); typing coalesces into bursts (COALESCE_MS) so a run of keystrokes
// undoes as one unit; undo/redo restore value *and* selection. histMirror shadows the
// current committed text so a burst can preserve its pre-input state. See histCommit /
// applyEdit / recordTyping / doUndo near the bottom of the script.
const HIST_CAP = 200;                 // linear history depth (oldest snapshot dropped past this)
const COALESCE_MS = 700;              // consecutive typing within this gap folds into one undo unit
let history = [], histIndex = -1, histMirror = '';
let lastEditKind = 'boot', lastTypeTime = 0, composing = false;
// Diagnostics triage: the active severity filter (null = show all), persisted
// across recompiles, and the last payload so a pill click can re-render in place.
let diagFilter = null, lastDiagPayload = { diagnostics: [], counts: { error:0, warning:0, info:0 } };
let lastGood = null;     // last payload that carried a full render
let scene3d = null;      // last good 3D scene json
let ctrl = null;         // 3D renderer controller
let threeInit = false;   // mountScene attempted (canvas may be replaced)
let sceneLoaded = false;  // scene3d currently uploaded to ctrl
let currentTab = 'plan';

function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmt(n){ return (Math.round(n*10)/10).toString(); }
function trimNum(n){ return (Math.round(n*10)/10).toString().replace(/\.0$/,''); }
// Feet-and-inches display, mirroring Python's fmt_ft_in: round to the nearest
// inch; whole feet drop the inch part (18′, not 18′-0″); fractional reads 18′-6″;
// sub-foot reads as inches alone (9″); 0 → 0′; negatives keep a leading '-'.
function fmtFtIn(v){
  v = Number(v); if (!isFinite(v)) return '';
  const neg = v < 0, t = Math.round(Math.abs(v) * 12);
  const ft = Math.floor(t / 12), inch = t % 12;
  let s;
  if (inch === 0) s = ft + '′';
  else if (ft === 0) s = inch + '″';
  else s = ft + '′-' + inch + '″';
  return neg ? '-' + s : s;
}
// Parse a length the user typed into a dimension field. Accepts 12'6", 12' 6",
// 12-6, 12.5 and plain 12 (and the ′/″ glyphs); returns feet as a number, or
// null when the text isn't a recognisable length (caller keeps prior value).
function parseFtIn(str){
  if (str == null) return null;
  let s = String(str).trim().replace(/[′’]/g, "'").replace(/[″”]/g, '"');
  if (s === '') return null;
  if (/^-?\d*\.?\d+$/.test(s)){ const f = parseFloat(s); return isFinite(f) ? f : null; }
  let m = s.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$/);   // 12-6
  if (m){ const ft = parseFloat(m[1]), sgn = ft < 0 ? -1 : 1; return ft + sgn * parseFloat(m[2]) / 12; }
  m = s.match(/^(-?\d+(?:\.\d+)?)\s*'\s*(?:(\d+(?:\.\d+)?)\s*"?)?$/);   // 12'6"  12'  12' 6
  if (m){ const ft = parseFloat(m[1]), sgn = ft < 0 ? -1 : 1; return ft + sgn * (m[2] ? parseFloat(m[2]) : 0) / 12; }
  m = s.match(/^(-?\d+(?:\.\d+)?)\s*"$/);   // 6"
  if (m) return parseFloat(m[1]) / 12;
  return null;
}

// --- editor: line numbers + severity gutter ---------------------------------
function renderGutter(){
  const n = editor.value.split('\n').length;
  const marks = {};
  const rank = { error:3, warning:2, info:1 };
  for (const d of diagnostics){ if (!d.line) continue;
    const prev = marks[d.line], r = rank[d.severity] || 0;
    if (!prev || r > prev.r) marks[d.line] = { sev:d.severity, r }; }
  let html = '';
  for (let i = 1; i <= n; i++){ const m = marks[i];
    html += '<div class="gln' + (m ? ' has-' + m.sev : '') + '">' +
      (m ? '<span class="dot"></span>' : '') + i + '</div>'; }
  gutter.innerHTML = html;
  gutter.scrollTop = editor.scrollTop;
  renderHighlight();          // keep the colour layer in step with every text change
}

// --- syntax highlighting: a coloured <pre> behind the transparent textarea ---
// Same font metrics, tab-size and padding as the textarea, aria-hidden, and
// scroll-synced with it — so the colours sit exactly under the caret and never
// desync. Purely visual: the textarea keeps all input behaviour (tab, IME, paste).
const HL_KW = new Set(HIGHLIGHT.keywords || []);
const HL_TYPE = new Set(HIGHLIGHT.types || []);
// Statement heads (line-leading keywords) and fixture kinds, split out from the
// merged highlight vocab: the autocomplete and the diagnostics quick-fix must tell
// a statement head from a mid-line modifier, which HL_KW alone can't.
const HL_STMT = new Set(HIGHLIGHT.statements || []);
const HL_FIX = HIGHLIGHT.fixtures || [];
function hlWord(w){
  if (w.length > 1 && w.endsWith(':'))
    return '<span class="id">' + esc(w.slice(0, -1)) + '</span>:';
  const lw = w.toLowerCase();
  if (HL_KW.has(lw)) return '<span class="k">' + esc(w) + '</span>';
  if (HL_TYPE.has(lw)) return '<span class="t">' + esc(w) + '</span>';
  if (/\d/.test(w))
    return esc(w).replace(/\d+(?:\.\d+)?/g, m => '<span class="n">' + m + '</span>');
  return esc(w);
}
function hlLine(line){
  let out = '', i = 0; const n = line.length;
  while (i < n){
    const ch = line[i];
    if (ch === '#'){ out += '<span class="c">' + esc(line.slice(i)) + '</span>'; break; }
    if (ch === '"'){
      let j = i + 1;
      while (j < n && line[j] !== '"'){ if (line[j] === '\\') j++; j++; }
      if (j < n) j++;                       // include the closing quote if present
      out += '<span class="s">' + esc(line.slice(i, j)) + '</span>'; i = j; continue;
    }
    if (ch === ' ' || ch === '\t'){ out += ch; i++; continue; }
    let j = i;
    while (j < n && line[j] !== ' ' && line[j] !== '\t' && line[j] !== '#' && line[j] !== '"') j++;
    out += hlWord(line.slice(i, j)); i = j;
  }
  return out;
}
// Find highlighting rides on the same mirror. A match can straddle token
// boundaries, so instead of injecting into hlLine's finished HTML we retokenise
// the line into {cls,text} pieces, split those pieces at the match edges, and wrap
// the covered runs in <mark> — well-formed even across a keyword/number seam.
function classifyWord(w){
  if (w.length > 1 && w.endsWith(':')) return [{ cls:'id', s:w.slice(0, -1) }, { cls:'', s:':' }];
  const lw = w.toLowerCase();
  if (HL_KW.has(lw)) return [{ cls:'k', s:w }];
  if (HL_TYPE.has(lw)) return [{ cls:'t', s:w }];
  if (/\d/.test(w)){                         // colour digit runs, leave the rest plain
    const parts = [], re = /(\d+(?:\.\d+)?)|([^\d]+)/g; let m;
    while ((m = re.exec(w))) parts.push({ cls: m[1] != null ? 'n' : '', s: m[0] });
    return parts;
  }
  return [{ cls:'', s:w }];
}
function hlPieces(line){
  const out = []; let i = 0; const n = line.length;
  while (i < n){
    const ch = line[i];
    if (ch === '#'){ out.push({ cls:'c', s:line.slice(i) }); break; }
    if (ch === '"'){
      let j = i + 1;
      while (j < n && line[j] !== '"'){ if (line[j] === '\\') j++; j++; }
      if (j < n) j++;
      out.push({ cls:'s', s:line.slice(i, j) }); i = j; continue;
    }
    if (ch === ' ' || ch === '\t'){ out.push({ cls:'', s:ch }); i++; continue; }
    let j = i;
    while (j < n && line[j] !== ' ' && line[j] !== '\t' && line[j] !== '#' && line[j] !== '"') j++;
    for (const p of classifyWord(line.slice(i, j))) out.push(p);
    i = j;
  }
  return out;
}
// Split pieces so each falls wholly inside or outside a match (marks sorted,
// non-overlapping, offsets relative to the line start).
function splitByMarks(pieces, marks){
  const out = []; let pos = 0;
  for (const p of pieces){
    const base = pos, end = pos + p.s.length; let cur = base;
    while (cur < end){
      let mk = null, next = end;
      for (const m of marks){
        if (m.s <= cur && cur < m.e){ mk = m; next = Math.min(end, m.e); break; }
        if (cur < m.s) next = Math.min(next, m.s);
      }
      out.push({ cls:p.cls, s:p.s.slice(cur - base, next - base), mk });
      cur = next;
    }
    pos = end;
  }
  return out;
}
function emitMarked(tagged){
  let html = '', open = undefined;
  for (const t of tagged){
    const id = t.mk ? (t.mk.i + (t.mk.cur ? ':cur' : '')) : null;
    if (id !== open){
      if (open) html += '</mark>';
      if (id !== null) html += '<mark class="find' + (t.mk.cur ? ' cur' : '') + '">';
      open = id;
    }
    html += t.cls ? '<span class="' + t.cls + '">' + esc(t.s) + '</span>' : esc(t.s);
  }
  if (open) html += '</mark>';
  return html;
}
function renderHighlight(){
  const active = findOpen && findMatches.length;
  let off = 0;
  const out = editor.value.split('\n').map(line => {
    let lm = null;
    if (active){
      lm = []; const lineEnd = off + line.length;
      for (let k = 0; k < findMatches.length; k++){
        const m = findMatches[k];
        if (m.end > off && m.start < lineEnd)
          lm.push({ s:Math.max(0, m.start - off), e:Math.min(line.length, m.end - off),
            i:k, cur:k === findIndex });
      }
    }
    off += line.length + 1;
    return (lm && lm.length) ? emitMarked(splitByMarks(hlPieces(line), lm)) : hlLine(line);
  });
  // A trailing newline keeps the <pre> the same height as the textarea's content.
  hl.innerHTML = out.join('\n') + '\n';
  hl.scrollTop = editor.scrollTop; hl.scrollLeft = editor.scrollLeft;
}
function syncScroll(){ gutter.scrollTop = editor.scrollTop;
  hl.scrollTop = editor.scrollTop; hl.scrollLeft = editor.scrollLeft; }

editor.addEventListener('scroll', syncScroll);
editor.addEventListener('scroll', () => { if (acOpen) hideAc(); });   // popup can't track a scroll
editor.addEventListener('input', () => {
  recordTyping();                       // fold this keystroke into the unified undo history
  renderGutter(); schedule();
  updateAutocomplete(false);            // refresh / dismiss the popup as the word changes
  if (findOpen) runFind(true);          // keep find matches live while the bar is open
});
// IME/composition must not fracture a burst: hold a flag so mid-composition input
// keeps folding into the current typing unit rather than starting a fresh checkpoint.
editor.addEventListener('compositionstart', () => { composing = true; });
editor.addEventListener('compositionend', () => { composing = false; });
editor.addEventListener('keydown', e => {
  // Unified undo/redo, intercepted here so the browser's native textarea undo never
  // fights the history (preventDefault). stopPropagation keeps the global handler from
  // running it a second time; the find/agent/help fields keep their own native undo.
  if (isRedoKey(e)){ e.preventDefault(); e.stopPropagation(); doRedo(); return; }
  if (isUndoKey(e)){ e.preventDefault(); e.stopPropagation(); doUndo(); return; }
  // Ctrl/Cmd+Space forces the completion popup regardless of word length.
  if ((e.ctrlKey || e.metaKey) && (e.key === ' ' || e.code === 'Space')){
    e.preventDefault(); updateAutocomplete(true); return;
  }
  // While the popup owns the keys, it captures navigation/accept/dismiss so the
  // textarea's own Tab-inserts-spaces stays untouched when nothing is open.
  if (acOpen){
    if (e.key === 'ArrowDown'){ e.preventDefault(); moveAc(1); return; }
    if (e.key === 'ArrowUp'){ e.preventDefault(); moveAc(-1); return; }
    if (e.key === 'Enter' || e.key === 'Tab'){ e.preventDefault(); acceptAc(); return; }
    if (e.key === 'Escape'){ e.preventDefault(); hideAc(); return; }
  }
  if ((e.ctrlKey || e.metaKey) && e.key === '/'){ e.preventDefault(); toggleComment(); return; }
  if (e.key === 'Tab'){ e.preventDefault(); insertText('  '); return; }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); compile(); return; }
});
function insertText(t){
  const s = editor.selectionStart, e = editor.selectionEnd;
  const caret = s + t.length;
  applyEdit(editor.value.slice(0, s) + t + editor.value.slice(e), caret, caret, 'typing');
  renderGutter(); schedule();
}

// --- comment toggle (Ctrl/Cmd+/) --------------------------------------------
// Toggles a leading `# ` on every non-blank line the selection (or caret) covers.
// If all of them are already commented it uncomments (dropping the `#` and one
// following space); otherwise it comments them. The selection is grown to cover
// the same lines afterwards so a repeat keypress flips them straight back.
function toggleComment(){
  const val = editor.value, selS = editor.selectionStart, selE = editor.selectionEnd;
  const lines = val.split('\n'), starts = [];
  let off = 0;
  for (const ln of lines){ starts.push(off); off += ln.length + 1; }
  const lineOf = pos => { let li = 0;
    for (let i = 0; i < lines.length; i++){ if (starts[i] <= pos) li = i; else break; } return li; };
  let first = lineOf(selS), last = lineOf(selE);
  // A selection ending exactly at a line's start shouldn't drag in that next line.
  if (selE > selS && selE === starts[last] && last > first) last--;
  const idxs = [];
  for (let i = first; i <= last; i++) if (lines[i].trim() !== '') idxs.push(i);
  if (!idxs.length) return;                       // nothing but blank lines
  const allCommented = idxs.every(i => /^\s*#/.test(lines[i]));
  for (const i of idxs)
    lines[i] = allCommented ? lines[i].replace(/^(\s*)#\s?/, '$1')
                            : lines[i].replace(/^(\s*)/, '$1# ');
  // Reselect the same span of lines (start of first → end of last).
  let noff = 0; const nstarts = [];
  for (const ln of lines){ nstarts.push(noff); noff += ln.length + 1; }
  autosaveOff = false;
  applyEdit(lines.join('\n'), nstarts[first], nstarts[last] + lines[last].length, 'toggle comment');
  renderGutter(); schedule();
}

// --- debounced compile ------------------------------------------------------
let timer = null;
function schedule(){ clearTimeout(timer); timer = setTimeout(compile, 400); }
function compile(){
  const source = editor.value;
  fetch('/api/compile', { method:'POST', headers:{ 'Content-Type':'application/json' },
    body: JSON.stringify({ source }) })
    .then(r => r.json()).then(applyResult)
    .catch(err => { diagEl.innerHTML =
      '<div class="diag-empty">server unreachable: ' + esc(err) + '</div>'; });
}

function applyResult(p){
  diagnostics = p.diagnostics || [];
  renderDiagnostics(p);
  renderGutter();
  updateHeader(p);
  const good = p.svg && !p.recovered;
  if (good){
    lastGood = p; scene3d = p.scene; sceneLoaded = false;
    viewport.classList.remove('stale');
    planSvg.innerHTML = planVariant(p);
    if (currentTab === 'plan') planZoom.refit(); else planNeedsFit = true;
    renderViews(p);
    renderReport(p);
    if (currentTab === 'three') showThree();
  } else if (lastGood){
    viewport.classList.add('stale');  // keep the last good render, dimmed
  } else {
    planSvg.innerHTML = '';
  }
  refreshEditData(good ? p : null);
  autosave();                          // persist whatever is now in the editor
  updateExportState(!!p.ok);           // export needs a clean compile (ok, not just rendered)
}

// --- header (title / score / metrics) ---------------------------------------
function scoreClass(t){ return t >= 85 ? 'good' : (t >= 65 ? 'mid' : 'low'); }
function breakdown(s){
  const parts = [];
  for (const k in s.components){ const v = s.components[k]; if (v > 0) parts.push(k + ' -' + fmt(v)); }
  let txt = 'Design score ' + fmt(s.total) + ' / 100' +
    (parts.length ? '  (deductions: ' + parts.join(', ') + ')' : '  (clean)');
  for (const k in (s.details || {})) txt += '\n  ' + k + ': ' + s.details[k];
  return txt;
}
function updateHeader(p){
  if (p.title) titleEl.textContent = p.title;
  if (p.score){
    scoreChip.textContent = 'Score ' + fmt(p.score.total) + '/100';
    scoreChip.className = 'chip ' + scoreClass(p.score.total);
    scoreChip.title = breakdown(p.score);
  }
  if (p.metrics){ const m = p.metrics;
    metricsEl.textContent = Math.round(m.footprint_sqft) + ' sq ft · ' +
      (m.bedroom_count | 0) + ' bed / ' + trimNum(m.bathroom_count) + ' bath'; }
  if (p.score){ lastScore = p.score; if (!scorePop.hidden) renderScorePop(); }
}

// --- score popover (per-category breakdown, click / touch) ------------------
// The chip's `title` stays as a fallback; the popover draws the same components
// as colour-coded bars plus the detail lines, and works on touch (click, not
// hover). Bars are sized to each category's deduction relative to the largest.
const scorePop = document.getElementById('score-pop');
let lastScore = null;
const SCORE_COLORS = { errors:'#c8452f', warnings:'#c98a1e', infos:'#2f6fb0',
  space:'#8a5cc0', circulation:'#2e8b57', proportion:'#d1873f', daylight:'#2F6FB0' };
function renderScorePop(){
  const s = lastScore; if (!s){ scorePop.innerHTML = ''; return; }
  const comps = s.components || {};
  const entries = Object.keys(comps).map(k => [k, comps[k]]);
  const max = Math.max(1, ...entries.map(e => e[1]));
  const active = entries.filter(e => e[1] > 0);
  let rows = '';
  for (const [k, v] of (active.length ? active : entries)){
    const pct = Math.max(v > 0 ? 6 : 0, Math.round((v / max) * 100));
    const col = SCORE_COLORS[k] || 'var(--accent)';
    rows += '<div class="sp-row"><span class="lbl">' + esc(k) + '</span>' +
      '<span class="sp-bar"><span style="width:' + pct + '%;background:' + col + '"></span></span>' +
      '<span class="val">-' + fmt(v) + '</span></div>';
  }
  if (!active.length) rows += '<div class="sp-clean">No deductions — a clean plan.</div>';
  let details = '';
  for (const k in (s.details || {}))
    details += '<div><strong>' + esc(k) + ':</strong> ' + esc(s.details[k]) + '</div>';
  scorePop.innerHTML = '<h4>Design score ' + fmt(s.total) + ' <span>/ 100</span></h4>' + rows +
    (details ? '<div class="sp-details">' + details + '</div>' : '');
}
function toggleScorePop(show){
  const open = show == null ? scorePop.hidden : show;
  if (open && lastScore){ renderScorePop(); scorePop.hidden = false; }
  else scorePop.hidden = true;
}
scoreChip.addEventListener('click', e => { e.stopPropagation(); toggleScorePop(); });
document.addEventListener('click', e => {
  if (!scorePop.hidden && !e.target.closest('#score-pop') && !e.target.closest('#score-chip'))
    toggleScorePop(false);
});

// --- help slide-over: DSL reference + keyboard shortcuts --------------------
// The reference is fetched once (finally wiring GET /api/reference) and rendered
// with a light touch: heading lines get an accent heading, everything else a
// monospace block. A filter input narrows it; Esc / click-outside closes.
const helpBtn = document.getElementById('help-btn');
const helpPanel = document.getElementById('help-panel');
const helpBackdrop = document.getElementById('help-backdrop');
const helpSearch = document.getElementById('help-search');
const helpRefBody = document.getElementById('help-ref-body');
const helpShortcuts = document.getElementById('help-shortcuts');
const MOD = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl';
const SHORTCUTS = [
  ['Save .barn', MOD + '+S'], ['Open a .barn file', MOD + '+O'],
  ['Format (comments & pragmas kept)', 'Shift+Alt+F'],
  ['Find in the editor', MOD + '+F'], ['Find & replace', MOD + '+H'],
  ['Autocomplete', MOD + '+Space'], ['Toggle comment', MOD + '+/'],
  ['Send to the agent', MOD + '+Enter'], ['Undo / Redo', MOD + '+Z  ·  ' + MOD + '+Shift+Z'],
  ['Zoom in / out / fit', '+  −  0'], ['Compile now', MOD + '+Enter'],
  ['Cancel a drag', 'Esc'], ['Switch floor (edit mode)', '[  ]'],
  ['Measure on the plan (edit mode)', 'm'],
  ['Nudge the selected room 1 ft / 3 ft', '←↑↓→  ·  Shift'],
  ['Rotate the selected fixture', 'r'], ['Delete the selection', 'Del'],
  ['Switch viewport tab', '1  2  3  4'], ['Cycle theme', 't'],
  ['Open this help', '?'],
];
let helpRefLines = null;   // cached parsed reference lines (fetched once)

//: 3D-view tips, shown in the help panel (the first-person walkthrough in particular).
const THREE_TIPS = [
  'Walk mode: on the 3D tab, click Walk (or press Enter) to step inside at eye height.',
  'WASD or the arrow keys move relative to where you look; the mouse looks around; ' +
    'Shift runs. You slide along walls and pass through doorways.',
  'On a touch device (iPad), a thumbstick appears bottom-left to move while a ' +
    'second finger drags to look; push the stick to its rim to run. Tap the Exit ' +
    'pill to leave (there is no Esc).',
  'Eye-height pill (or the C key) cycles Standing / Seated (ADA sightlines) / ' +
    'Child so you can check what each sees. The Furniture pill toggles whether you ' +
    'bump into fixtures or ghost through them.',
  'Walk up the stairs to reach the upper floor; Esc (or leaving the tab) exits back to orbit.',
];

//: Edit-mode direct-manipulation tips (drag behaviours), shown in the help panel.
const EDIT_TIPS = [
  'The ☰ Design panel edits the plan through forms — outline, properties, add and ' +
    'delete — no code required. Every change is still one DSL text edit, so the ' +
    'code pane follows along and Undo works as usual.',
  'Drag a room to move it; drag its handles to resize. Edges snap to neighbours. ' +
    'Arrow keys nudge the selected room 1 ft (Shift: 3 ft).',
  'Drag a door or window along its wall to re-position it.',
  'Measure (⟷ or `m`): drag between any two points for a live distance readout — ' +
    'clearances, walkways, furniture gaps. Esc puts the tape away.',
  'Drag a fixture to move it. An authored fixture rewrites its `at x,y`; a dashed ' +
    'auto-seed (bath/kitchen/laundry) becomes an authored `fixture` line where you drop it. ' +
    'Dragging a counter `along` run slides it along its wall (updating `from`/`to`).',
  'Add fixtures in the DSL: `fixture <kind> in <room> [at <x>,<y>] [wall N|S|E|W] [rotate <deg>]`. ' +
    'A countertop run: `fixture counter in <room> along N|S|E|W [from <a> to <b>] [depth <d>]`.',
  'Every drag is one surgical text edit. Undo and redo (' + MOD + '+Z, ' + MOD +
    '+Shift+Z) span one timeline across typing, smart edits and drags alike.',
];
function renderShortcuts(){
  let h = '<h5>Keyboard shortcuts</h5>';
  for (const [label, keys] of SHORTCUTS)
    h += '<div class="sc"><span>' + esc(label) + '</span><kbd>' + esc(keys) + '</kbd></div>';
  h += '<h5>3D view</h5>';
  for (const tip of THREE_TIPS) h += '<div class="sc-tip">' + esc(tip) + '</div>';
  h += '<h5>Edit mode</h5>';
  for (const tip of EDIT_TIPS) h += '<div class="sc-tip">' + esc(tip) + '</div>';
  helpShortcuts.innerHTML = h;
}
function isHelpHeading(line){
  // A non-indented line that reads as a section title (ends with ':' or is a
  // banner). Indented grammar lines stay monospace.
  if (!line || /^\s/.test(line)) return false;
  return /:\s*$/.test(line) || /^[A-Z][A-Z0-9 ]+[A-Z0-9]$/.test(line);
}
function loadReference(){
  if (helpRefLines) return Promise.resolve();
  return fetch('/api/reference').then(r => r.json()).then(j => {
    helpRefLines = String(j.reference || '').split('\n');
    renderReference('');
  }).catch(() => { helpRefBody.className = 'rempty';
    helpRefBody.textContent = 'Reference unavailable.'; });
}
function renderReference(q){
  if (!helpRefLines) return;
  q = (q || '').trim().toLowerCase();
  let out = '', shown = 0;
  for (const raw of helpRefLines){
    if (q && raw.toLowerCase().indexOf(q) < 0) continue;
    const head = isHelpHeading(raw);
    let body = esc(raw);
    if (q){ const i = raw.toLowerCase().indexOf(q);
      body = esc(raw.slice(0, i)) + '<mark>' + esc(raw.slice(i, i + q.length)) +
        '</mark>' + esc(raw.slice(i + q.length)); }
    out += '<div class="rline' + (head ? ' head' : '') + (q ? ' hit' : '') + '">' +
      (body || '&nbsp;') + '</div>';
    shown++;
  }
  helpRefBody.className = '';
  helpRefBody.innerHTML = shown ? out : '<div class="rempty">No matches for “' + esc(q) + '”.</div>';
}
function openHelp(){
  renderShortcuts();
  helpBackdrop.hidden = false; helpPanel.hidden = false;
  loadReference();
  setTimeout(() => helpSearch.focus(), 30);
}
function closeHelp(){ helpPanel.hidden = true; helpBackdrop.hidden = true; }
helpBtn.addEventListener('click', openHelp);
document.getElementById('help-close').addEventListener('click', closeHelp);
helpBackdrop.addEventListener('click', closeHelp);
helpSearch.addEventListener('input', () => renderReference(helpSearch.value));

// --- compare: scheme A (a saved baseline) vs B (the current editor) ----------
// The workflow the empty state teaches: snapshot the current plan as baseline A,
// keep editing, then reopen Compare to see A vs B. A survives reload via
// localStorage; B is always whatever is in the editor now. Swap flips which side
// is A and which is B (a pure display toggle — it never mutates the editor).
const compareBtn = document.getElementById('compare-btn');
const compareModal = document.getElementById('compare-modal');
const compareBackdrop = document.getElementById('compare-backdrop');
const compareBody = document.getElementById('compare-body');
const compareFileInput = document.getElementById('compare-file-input');
let compareBaselineA = null;   // the snapshotted baseline source (scheme A), or null
let compareSwapped = false;    // show B-as-A / A-as-B without touching the editor
try { compareBaselineA = localStorage.getItem(LS_COMPARE_A); } catch (e){}

function setBaselineA(src){
  compareBaselineA = src;
  try { localStorage.setItem(LS_COMPARE_A, src); } catch (e){}
  document.getElementById('compare-swap').disabled = false;
  if (!compareModal.hidden) runCompare();
}
function openCompare(){
  compareBackdrop.hidden = false; compareModal.hidden = false;
  document.getElementById('compare-swap').disabled = compareBaselineA == null;
  runCompare();
}
function closeCompare(){ compareModal.hidden = true; compareBackdrop.hidden = true; }

// Which metrics read as "higher is better" vs "lower is better" — used only to
// tint the winning side (ties stay neutral). Lengths render with fmtFtIn; areas
// and counts render as plain sq ft / integers.
const CMP_LOWER_BETTER = { footprint_sqft:1, unassigned_sqft:1, exterior_wall_area_sqft:1,
  roof_area_sqft:1, beam_linear_ft:1, post_count:1 };
const CMP_LEN = { beam_linear_ft:1 };
const CMP_METRIC_LABEL = { footprint_sqft:'footprint', interior_sqft:'interior',
  habitable_sqft:'habitable', unassigned_sqft:'unassigned', bedroom_count:'bedrooms',
  bathroom_count:'bathrooms', exterior_wall_area_sqft:'exterior wall', roof_area_sqft:'roof area',
  beam_linear_ft:'beam length', post_count:'posts' };
function cmpVal(k, v){
  if (v == null) return '—';
  if (CMP_LEN[k]) return fmtFtIn(v);
  if (/_sqft$/.test(k)) return trimNum(v) + ' sq ft';
  if (/_count$/.test(k)) return String(Math.round(v));
  return trimNum(v);
}
// class for the two value cells, tinting whichever side wins the row.
function cmpWin(av, bv, lowerBetter){
  if (av == null || bv == null || av === bv) return ['cmp-va', 'cmp-vb'];
  const aWins = lowerBetter ? av < bv : av > bv;
  return aWins ? ['cmp-va cmp-win', 'cmp-vb'] : ['cmp-va', 'cmp-vb cmp-win'];
}

function renderCompare(p, aCap, bCap){
  if (p.error){
    compareBody.innerHTML = '<div class="cmp-err">' +
      esc((p.error && (p.error.message || p.error)) || 'comparison failed') + '</div>';
    return;
  }
  const A = p.a, B = p.b, cm = p.compile || { a:{}, b:{} };
  const badge = c => !c ? '' : (c.ok ? '' :
    ' <span class="cmp-tag intro">' + ((c.counts && c.counts.error) || 0) + ' err</span>');
  let h = '<div class="cmp-cols">';
  h += '<span class="cmp-h"></span>' +
    '<span class="cmp-h a">A' + badge(cm.a) + '<span class="cap">' + esc(aCap) + '</span></span>' +
    '<span class="cmp-h b">B' + badge(cm.b) + '<span class="cap">' + esc(bCap) + '</span></span>' +
    '<span class="cmp-h b">Δ</span>';
  // score
  const sw = cmpWin(A.score, B.score, false);
  h += '<span class="cmp-lbl">Design score</span>' +
    '<span class="' + sw[0] + ' cmp-scorebig">' + fmt(A.score) + '</span>' +
    '<span class="' + sw[1] + ' cmp-scorebig">' + fmt(B.score) + '</span>' +
    '<span class="cmp-vd">' + (p.deltas.score >= 0 ? '+' : '') + fmt(p.deltas.score) + '</span>';
  h += '</div>';
  // per-component deductions (only components either side deducts on)
  const comps = Object.keys(Object.assign({}, A.components, B.components)).sort();
  let crows = '';
  for (const k of comps){
    const av = A.components[k] || 0, bv = B.components[k] || 0;
    if (!av && !bv) continue;
    const w = cmpWin(av, bv, true);   // fewer deductions wins
    crows += '<span class="cmp-lbl">' + esc(k) + '</span>' +
      '<span class="' + w[0] + '">-' + fmt(av) + '</span>' +
      '<span class="' + w[1] + '">-' + fmt(bv) + '</span>' +
      '<span class="cmp-vd">' + ((bv - av) >= 0 ? '+' : '') + fmt(bv - av) + '</span>';
  }
  if (crows) h += '<div class="cmp-sec">Score deductions (lower is better)</div>' +
    '<div class="cmp-cols">' + crows + '</div>';
  // takeoff metrics
  let mrows = '';
  for (const k in A.metrics){
    if (!(k in B.metrics)) continue;
    const av = A.metrics[k], bv = B.metrics[k];
    const w = cmpWin(av, bv, !!CMP_LOWER_BETTER[k]);
    const d = p.deltas[k];
    mrows += '<span class="cmp-lbl">' + esc(CMP_METRIC_LABEL[k] || k) + '</span>' +
      '<span class="' + w[0] + '">' + cmpVal(k, av) + '</span>' +
      '<span class="' + w[1] + '">' + cmpVal(k, bv) + '</span>' +
      '<span class="cmp-vd">' + (d ? (d > 0 ? '+' : '') + trimNum(d) : '·') + '</span>';
  }
  if (mrows) h += '<div class="cmp-sec">Takeoff</div><div class="cmp-cols">' + mrows + '</div>';
  // diagnostics multiset diff
  const res = p.resolved || {}, intro = p.introduced || {};
  const resK = Object.keys(res), introK = Object.keys(intro);
  h += '<div class="cmp-sec">Diagnostics (B relative to A)</div><div>';
  if (!resK.length && !introK.length)
    h += '<div class="cmp-diag-none">Identical diagnostic code sets.</div>';
  for (const c of resK)
    h += '<span class="cmp-tag res">B resolves ' + esc(c) + (res[c] > 1 ? ' ×' + res[c] : '') + '</span>';
  for (const c of introK)
    h += '<span class="cmp-tag intro">B introduces ' + esc(c) + (intro[c] > 1 ? ' ×' + intro[c] : '') + '</span>';
  h += '</div>';
  compareBody.innerHTML = h;
}

async function runCompare(){
  if (compareBaselineA == null){
    document.getElementById('compare-swap').disabled = true;
    compareBody.innerHTML = '<div class="cmp-empty"><strong>Snapshot this plan as baseline A</strong>, ' +
      'keep editing, then reopen Compare to see your changes (scheme B) measured against it — ' +
      'score, takeoff and diagnostics side by side.</div>';
    return;
  }
  // Not swapped: A = the baseline snapshot, B = the current editor. Swapped flips them.
  const editorSrc = editor.value;
  const source_a = compareSwapped ? editorSrc : compareBaselineA;
  const source_b = compareSwapped ? compareBaselineA : editorSrc;
  const aCap = compareSwapped ? 'current editor' : 'baseline';
  const bCap = compareSwapped ? 'baseline' : 'current editor';
  compareBody.innerHTML = '<div class="cmp-empty">Comparing…</div>';
  try {
    const resp = await fetch('/api/compare', { method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ source_a, source_b }) });
    const p = await resp.json();
    renderCompare(p, aCap, bCap);
  } catch (err){
    compareBody.innerHTML = '<div class="cmp-err">Compare failed: ' + esc(String(err)) + '</div>';
  }
}
compareBtn.addEventListener('click', openCompare);
document.getElementById('compare-close').addEventListener('click', closeCompare);
compareBackdrop.addEventListener('click', closeCompare);
document.getElementById('compare-set-a').addEventListener('click', () => { compareSwapped = false; setBaselineA(editor.value); });
document.getElementById('compare-swap').addEventListener('click', () => { compareSwapped = !compareSwapped; runCompare(); });
document.getElementById('compare-load-a').addEventListener('click', () => compareFileInput.click());
compareFileInput.addEventListener('change', () => {
  const f = compareFileInput.files && compareFileInput.files[0];
  if (f){ const rd = new FileReader();
    rd.onload = () => { compareSwapped = false; setBaselineA(String(rd.result || '')); };
    rd.readAsText(f); }
  compareFileInput.value = '';
});

// --- theme toggle (header): auto → light → dark -----------------------------
// Auto defers to the OS via the media query (today's behaviour, unchanged);
// light/dark stamp `data-theme` on <html>, which the pinned palette blocks read.
// Persisted, keyboard-accessible (it's a <button>, plus the `t` global key).
const themeBtn = document.getElementById('theme-btn');
const THEME_CYCLE = ['auto', 'light', 'dark'];
const THEME_GLYPH = { auto:'◐', light:'☀', dark:'☾' };
let themeMode = 'auto';
function applyTheme(mode){
  themeMode = THEME_CYCLE.indexOf(mode) >= 0 ? mode : 'auto';
  if (themeMode === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', themeMode);
  themeBtn.textContent = THEME_GLYPH[themeMode];
  themeBtn.title = 'Theme: ' + themeMode + ' (t)';
  try { localStorage.setItem(LS_THEME, themeMode); } catch (e){}
  // A 3D redraw picks up palette-driven clear/background changes immediately.
  if (currentTab === 'three' && ctrl) ctrl.draw();
}
function cycleTheme(){ applyTheme(THEME_CYCLE[(THEME_CYCLE.indexOf(themeMode) + 1) % THEME_CYCLE.length]); }
themeBtn.addEventListener('click', cycleTheme);
(function initTheme(){ let saved = null; try { saved = localStorage.getItem(LS_THEME); } catch (e){}
  applyTheme(saved || 'auto'); })();

// Global keys: `?` opens help, `t` cycles the theme, `1`–`4` switch the viewport
// tab — all only when focus is not in an editable element. Esc closes overlays.
document.addEventListener('keydown', e => {
  const el = document.activeElement, tag = el && el.tagName;
  const typing = tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT';
  if (e.key === '?' && !typing){ e.preventDefault(); openHelp(); return; }
  if (!typing && !e.metaKey && !e.ctrlKey && !e.altKey){
    const TAB_KEY = { '1':'plan', '2':'three', '3':'views', '4':'report' };
    if (TAB_KEY[e.key]){ e.preventDefault(); selectTab(TAB_KEY[e.key]); return; }
    if (e.key === 't'){ e.preventDefault(); cycleTheme(); return; }
  }
  if (e.key === 'Escape'){
    if (lb && !lb.hidden){ closeLightbox(); return; }
    if (!compareModal.hidden){ closeCompare(); return; }
    if (!helpPanel.hidden){ closeHelp(); return; }
    if (!scorePop.hidden){ toggleScorePop(false); return; }
  }
});

// --- diagnostics list -------------------------------------------------------
function escAttr(s){ return esc(s).replace(/"/g, '&quot;'); }

// A hint often embeds a paste-able DSL line in backticks, e.g.
// "…e.g. `entry a south width 3 offset 4`." We surface an Apply button only when
// that snippet is a *complete, literal* statement the author can drop in as-is:
// its first word is a statement head (not a modifier), it carries no placeholder
// (…/</>) they would still have to fill in, and it is NOT introduced as an
// example (e.g. / for example / like right before the backtick). That last guard
// separates a droppable fix ("Declare the footprint: `envelope 60 x 40`") from
// an illustration of the syntax a malformed line should have had ("Add the
// closing quote, e.g. `plan \"Name\"`") — inserting the illustration would leave
// the real error untouched. `site <W> x <L>` and `size 12 x 10` also get no
// button. Mirrors quickfix_snippet in lsp.py (shared-fixture parity test).
const QUICKFIX_PLACEHOLDER = /\.\.\.|…|[<>]/;
const QUICKFIX_EXAMPLE_LEAD = /(?:e\.g\.|for example|like)[\s,]*$/i;
function quickFixSnippet(hint){
  if (!hint) return null;
  const re = /`([^`]+)`/g; let m;
  while ((m = re.exec(hint))){
    const snip = m[1].trim();
    const head = (snip.split(/\s+/)[0] || '').toLowerCase();
    if (!HL_STMT.has(head) || QUICKFIX_PLACEHOLDER.test(snip)) continue;
    if (QUICKFIX_EXAMPLE_LEAD.test(hint.slice(0, m.index))) continue;
    return snip;
  }
  return null;
}
function qfixTitle(snip){
  const t = snip.length > 30 ? snip.slice(0, 26).replace(/\s+\S*$/, '') + ' …' : snip;
  return 'Insert this line: ' + t;
}
// Insert a quick-fix snippet as a fresh line — after the diagnostic's line if it
// has one, else past the last non-blank line — then rerun the compile flow and
// jump/flash the inserted line.
function applyQuickFix(snippet, line){
  const lines = editor.value.split('\n');
  let at;
  if (line && line >= 1 && line <= lines.length){ at = line; }
  else { let i = lines.length - 1; while (i >= 0 && lines[i].trim() === '') i--; at = i + 1; }
  lines.splice(at, 0, snippet);
  autosaveOff = false;
  applyEdit(lines.join('\n'), null, null, 'quick-fix');
  renderGutter(); compile();
  const ln = at + 1;
  jumpToLine(ln); flashLine(ln);        // jumpToLine sets the on-screen selection
}

// Triage: order a *copy* errors → warnings → infos, line ascending within each
// (no-line rows last). The payload the viewport reads stays in emit order.
const SEV_RANK = { error:0, warning:1, info:2 };
function sortedDiagnostics(ds){
  return ds.slice().sort((a, b) => {
    const s = (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3);
    return s || (a.line || Infinity) - (b.line || Infinity);
  });
}
function countChip(kind, n){
  const active = diagFilter === kind, zero = !n;
  const title = zero ? '' : (active ? 'Showing only ' + kind + 's — click to clear'
                                    : 'Show only ' + kind + 's');
  return '<span class="count ' + kind + (zero ? ' zero' : '') + (active ? ' active' : '') +
    '" data-filter="' + kind + '" title="' + title + '">' + n + ' ' + kind +
    (n === 1 ? '' : 's') + '</span>';
}
function renderDiagnostics(p){
  lastDiagPayload = p;
  const ds = p.diagnostics || [], c = p.counts || { error:0, warning:0, info:0 };
  // A filter whose bucket emptied on recompile self-clears — a filter matching
  // nothing would just hide the work that remains, so we reveal it instead.
  if (diagFilter && !c[diagFilter]) diagFilter = null;
  let head = '<div class="diag-head">' + countChip('error', c.error) +
    countChip('warning', c.warning) + countChip('info', c.info) +
    (p.ok ? '<span class="ok">✓ compiles clean</span>' : '') + '</div>';
  if (!ds.length){ diagEl.innerHTML = head + '<div class="diag-empty">No diagnostics.</div>'; return; }
  let shown = sortedDiagnostics(ds);
  if (diagFilter) shown = shown.filter(d => d.severity === diagFilter);
  if (!shown.length){ diagEl.innerHTML = head +
    '<div class="diag-empty">No ' + esc(diagFilter) + 's — clear the filter to see the rest.</div>';
    return; }
  let rows = '';
  for (const d of shown){
    const snip = quickFixSnippet(d.hint);
    const apply = snip ? '<button class="qfix" data-qfix="' + escAttr(snip) + '" data-qline="' +
      (d.line || '') + '" title="' + escAttr(qfixTitle(snip)) + '">Apply</button>' : '';
    // An accepted diagnostic (downgraded by a `# barndsl: accept CODE` pragma) is
    // shown as an INFO with the reason visible and a distinct "accepted" badge —
    // the audit trail an author (and a reviewer) reads.
    const accBadge = d.accepted ? '<span class="acc" title="downgraded by an accept pragma">accepted</span> ' : '';
    rows += '<div class="diag-row sev-' + d.severity + (d.accepted ? ' accepted' : '') +
      '" data-line="' + (d.line || '') + '">' +
      '<span class="sev">' + d.severity + '</span>' +
      '<span class="code">' + esc(d.code) + '</span>' +
      '<span class="msg">' + accBadge +
        (d.line ? '<span class="loc">L' + d.line + (d.col ? ':' + d.col : '') + '</span> ' : '') +
        esc(d.message) + (d.room ? ' <em>(' + esc(d.room) + ')</em>' : '') +
        (d.hint ? '<span class="hint">' + esc(d.hint) + '</span>' : '') +
      '</span>' + apply + '</div>';
  }
  diagEl.innerHTML = head + rows;
}
diagEl.addEventListener('click', e => {
  // A count pill toggles a severity filter (zero-count pills stay inert).
  const pill = e.target.closest('.count[data-filter]');
  if (pill){
    if (!pill.classList.contains('zero')){
      const kind = pill.getAttribute('data-filter');
      diagFilter = diagFilter === kind ? null : kind;
      renderDiagnostics(lastDiagPayload);
    }
    return;
  }
  // Apply a quick-fix without also triggering the row's jump-to-line.
  const qbtn = e.target.closest('.qfix');
  if (qbtn){ e.stopPropagation();
    applyQuickFix(qbtn.getAttribute('data-qfix'),
      parseInt(qbtn.getAttribute('data-qline') || '0', 10)); return; }
  const row = e.target.closest('.diag-row'); if (!row) return;
  const ln = parseInt(row.getAttribute('data-line') || '0', 10);
  if (ln) jumpToLine(ln);
});
function jumpToLine(ln){
  const lines = editor.value.split('\n');
  let pos = 0;
  for (let i = 0; i < ln - 1 && i < lines.length; i++) pos += lines[i].length + 1;
  editor.focus();
  editor.selectionStart = pos;
  editor.selectionEnd = pos + (lines[ln - 1] ? lines[ln - 1].length : 0);
  const lh = parseFloat(getComputedStyle(editor).lineHeight) || 20;
  editor.scrollTop = Math.max(0, (ln - 3) * lh);
  gutter.scrollTop = editor.scrollTop;
}

// --- find & replace ---------------------------------------------------------
// A compact bar over the editor. Search is case-insensitive plain text; matches
// are highlighted in the mirror (renderHighlight), cycled with Enter/Shift+Enter
// or the ▲▼ buttons (wrapping), and replaced one-at-a-time or all at once. The bar
// closes on Esc, dropping every mark and returning focus to the editor.
const findBar = document.getElementById('find-bar');
const findInput = document.getElementById('find-input');
const findCount = document.getElementById('find-count');
const findReplaceRow = document.getElementById('find-replace-row');
const replaceInput = document.getElementById('replace-input');
function computeFindMatches(){
  findMatches = [];
  const q = findInput.value; if (!q) return;
  const hay = editor.value.toLowerCase(), needle = q.toLowerCase();
  let i = 0, idx;
  while ((idx = hay.indexOf(needle, i)) >= 0){
    findMatches.push({ start: idx, end: idx + needle.length });
    i = idx + needle.length;                 // non-overlapping
  }
}
function updateFindCount(){
  findCount.textContent = findMatches.length ? (findIndex + 1) + ' / ' + findMatches.length
    : (findInput.value ? '0 results' : '');
}
function scrollToMatch(m){
  const ln = editor.value.slice(0, m.start).split('\n').length;   // 1-based
  const lh = parseFloat(getComputedStyle(editor).lineHeight) || 20;
  editor.scrollTop = Math.max(0, (ln - 3) * lh);
  syncScroll();
}
function selectMatch(m){ editor.selectionStart = m.start; editor.selectionEnd = m.end; scrollToMatch(m); }
function runFind(keep){
  const prev = findMatches[findIndex];
  computeFindMatches();
  if (!findMatches.length){ findIndex = -1; }
  else if (keep && prev){
    let ni = findMatches.findIndex(m => m.start >= prev.start);
    findIndex = ni < 0 ? 0 : ni;
  } else {
    const caret = editor.selectionStart || 0;
    let ni = findMatches.findIndex(m => m.start >= caret);
    findIndex = ni < 0 ? 0 : ni;
  }
  updateFindCount(); renderHighlight();
  if (findIndex >= 0) scrollToMatch(findMatches[findIndex]);
}
function cycleFind(dir){
  if (!findMatches.length) return;
  findIndex = (findIndex + dir + findMatches.length) % findMatches.length;
  updateFindCount(); renderHighlight(); selectMatch(findMatches[findIndex]);
}
function openFind(withReplace){
  findOpen = true; findBar.hidden = false; findReplaceRow.hidden = !withReplace;
  const sel = editor.value.slice(editor.selectionStart, editor.selectionEnd);
  if (sel && sel.indexOf('\n') < 0) findInput.value = sel;   // prefill from a single-line selection
  runFind(false);
  findInput.focus(); findInput.select();
}
function closeFind(){
  findOpen = false; findBar.hidden = true; findMatches = []; findIndex = -1;
  renderHighlight(); editor.focus();
}
function replaceOne(){
  if (findIndex < 0 || !findMatches.length) return;
  const m = findMatches[findIndex], rep = replaceInput.value;
  const caret = m.start + rep.length;
  autosaveOff = false;
  applyEdit(editor.value.slice(0, m.start) + rep + editor.value.slice(m.end), caret, caret, 'replace');
  renderGutter(); schedule();
  runFind(false);
}
function replaceAll(){
  computeFindMatches();
  if (!findInput.value || !findMatches.length) return;
  const rep = replaceInput.value;
  let out = '', last = 0;
  for (const m of findMatches){ out += editor.value.slice(last, m.start) + rep; last = m.end; }
  out += editor.value.slice(last);
  const n = findMatches.length;
  autosaveOff = false;
  applyEdit(out, null, null, 'replace all');   // null selection leaves the caret put
  renderGutter(); schedule();
  runFind(false);
  findCount.textContent = 'replaced ' + n;
}
findInput.addEventListener('input', () => runFind(false));
findInput.addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); cycleFind(e.shiftKey ? -1 : 1); }
});
replaceInput.addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); replaceOne(); }
});
// Esc closes the bar from anywhere inside it — the ▲▼/Replace buttons hold focus
// after a click, so an input-only handler would strand the bar open.
findBar.addEventListener('keydown', e => {
  if (e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); closeFind(); }
});
document.getElementById('find-next').addEventListener('click', () => cycleFind(1));
document.getElementById('find-prev').addEventListener('click', () => cycleFind(-1));
document.getElementById('find-close').addEventListener('click', closeFind);
document.getElementById('replace-one').addEventListener('click', replaceOne);
document.getElementById('replace-all').addEventListener('click', replaceAll);

// --- editor autocomplete ----------------------------------------------------
// A context popup: statement heads at line start, room types after `room <id>:`,
// fixture kinds after `fixture`, and live room ids after door/open/window/entry
// heads, placement anchors and `in`. Never inside a comment. Accepting replaces
// the current word through the normal input path (renderGutter/renderHighlight).
const acPop = document.getElementById('ac-pop');
const AC_ANCHORS = new Set(['east-of','west-of','north-of','south-of',
  'right-of','left-of','above-of','below-of']);
const AC_ROOM_HEADS = new Set(['door','open','window','entry']);
let _acCharW = 0;
function acCharWidth(){
  if (_acCharW) return _acCharW;
  const cs = getComputedStyle(editor), span = document.createElement('span');
  span.style.cssText = 'position:absolute;visibility:hidden;white-space:pre';
  span.style.fontFamily = cs.fontFamily; span.style.fontSize = cs.fontSize;
  span.textContent = 'MMMMMMMMMM'; document.body.appendChild(span);
  _acCharW = span.getBoundingClientRect().width / 10; span.remove();
  return _acCharW || 8;
}
function roomIds(){
  const ids = [], re = /^\s*room\s+([A-Za-z_][\w-]*)\s*:/gm; let m;
  while ((m = re.exec(editor.value))) ids.push(m[1]);
  return ids;
}
function currentWord(){
  const pos = editor.selectionStart, v = editor.value;
  let s = pos, e = pos;
  while (s > 0 && /[\w-]/.test(v[s - 1])) s--;
  while (e < v.length && /[\w-]/.test(v[e])) e++;
  return { start: s, end: e, text: v.slice(s, pos) };   // prefix = text typed up to the caret
}
function completionContext(){
  const pos = editor.selectionStart, v = editor.value;
  const lineStart = v.lastIndexOf('\n', pos - 1) + 1;
  if (v.slice(lineStart, pos).indexOf('#') >= 0) return null;   // never inside a comment
  const w = currentWord();
  const before = v.slice(lineStart, w.start);
  const toks = before.split(/\s+/).filter(Boolean);
  const head = (toks[0] || '').toLowerCase();
  const last = toks.length ? toks[toks.length - 1].toLowerCase() : '';
  let items = null;
  if (toks.length === 0){ items = HIGHLIGHT.statements || []; }        // first word of the line
  else if (AC_ROOM_HEADS.has(last) || AC_ANCHORS.has(last) || last === 'in' || last === '-'){
    items = roomIds();                                                // a room-id slot
  } else if (head === 'room' && /^room\s+[A-Za-z_][\w-]*:\s*$/.test(before)){
    items = HIGHLIGHT.types || [];                                    // room type after `room <id>:`
  } else if (head === 'fixture' && toks.length === 1){
    items = HL_FIX;                                                   // `fixture <kind>`
  } else if (head === 'fixture' && (toks[1] || '').toLowerCase() === 'counter'){
    // counter run grammar: `in <room> along N|S|E|W [from <a> to <b>] [depth <d>]`.
    // (The `in <room>` id slot is already served by the room-id branch above.)
    const t = toks.map(s => s.toLowerCase());
    const alongIdx = t.indexOf('along');
    if (last === 'along'){
      items = ['N', 'S', 'E', 'W'];                                  // wall after `along`
    } else if (alongIdx === -1 && t.length >= 4 && t[t.length - 2] === 'in'){
      items = ['along'];                                             // after `in <room>`
    } else if (alongIdx >= 0 && t.length === alongIdx + 2){
      items = ['from'];                                              // after the wall
    } else { return null; }                                          // from/to/depth: no numeric popup
  } else { return null; }
  if (!items || !items.length) return null;
  return { word: w, items };
}
function paintAc(){
  let h = '';
  for (let i = 0; i < acItems.length; i++)
    h += '<div class="ac-item' + (i === acIndex ? ' sel' : '') + '" data-i="' + i + '">' +
      esc(acItems[i]) + '</div>';
  acPop.innerHTML = h;
}
function positionAc(){
  const pos = editor.selectionStart, v = editor.value;
  const nl = v.lastIndexOf('\n', pos - 1), col = pos - (nl + 1);
  const line = v.slice(0, pos).split('\n').length - 1;
  const cs = getComputedStyle(editor), lh = parseFloat(cs.lineHeight) || 20;
  const padL = parseFloat(cs.paddingLeft) || 0, padT = parseFloat(cs.paddingTop) || 0;
  const r = editor.getBoundingClientRect();
  const x = r.left + padL + col * acCharWidth() - editor.scrollLeft;
  const y = r.top + padT + (line + 1) * lh - editor.scrollTop;      // just below the caret's line
  acPop.style.left = x + 'px'; acPop.style.top = y + 'px';
  const box = acPop.getBoundingClientRect();
  if (box.right > window.innerWidth - 6) acPop.style.left = (window.innerWidth - box.width - 6) + 'px';
  if (box.bottom > window.innerHeight - 6) acPop.style.top = (y - box.height - lh - 4) + 'px';
}
function showAc(items, word){
  acItems = items; acWord = word; acIndex = 0; acOpen = true;
  paintAc(); acPop.hidden = false; positionAc();
}
function hideAc(){ acOpen = false; acPop.hidden = true; acItems = []; }
function moveAc(dir){
  if (!acItems.length) return;
  acIndex = (acIndex + dir + acItems.length) % acItems.length;
  paintAc();
  const sel = acPop.querySelector('.ac-item.sel'); if (sel) sel.scrollIntoView({ block:'nearest' });
}
function acceptAc(i){
  if (i == null) i = acIndex;
  const pick = acItems[i]; if (pick == null){ hideAc(); return; }
  const w = acWord, v = editor.value, caret = w.start + pick.length;
  applyEdit(v.slice(0, w.start) + pick + v.slice(w.end), caret, caret, 'autocomplete');
  hideAc(); editor.focus();
  renderGutter(); schedule();                    // accepted text goes through the normal path
}
function updateAutocomplete(force){
  const ctx = completionContext();
  if (!ctx){ hideAc(); return; }
  const prefix = ctx.word.text.toLowerCase();
  if (!force && prefix.length < 2){ hideAc(); return; }
  const matches = ctx.items.filter(it => it.toLowerCase().startsWith(prefix));
  // Nothing to choose (no match, or the single match is already fully typed).
  if (!matches.length || (matches.length === 1 && matches[0].toLowerCase() === prefix)){ hideAc(); return; }
  showAc(matches, ctx.word);
}
acPop.addEventListener('mousedown', e => e.preventDefault());   // keep the caret in the editor
acPop.addEventListener('click', e => {
  const it = e.target.closest('.ac-item'); if (!it) return;
  acceptAc(parseInt(it.getAttribute('data-i'), 10));
});
editor.addEventListener('blur', () => { if (acOpen) hideAc(); });

// --- tabs / viewport --------------------------------------------------------
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => selectTab(btn.getAttribute('data-tab')));
});
function selectTab(tab){
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(b =>
    b.classList.toggle('active', b.getAttribute('data-tab') === tab));
  document.querySelectorAll('.pane').forEach(p =>
    p.classList.toggle('active', p.id === 'pane-' + tab));
  if (tab === 'three') showThree();
  // Leaving the 3D tab must drop out of walk mode cleanly (release pointer lock,
  // unhook its key/mouse listeners) — the renderer restores the orbit camera.
  else if (ctrl && ctrl.exitWalk) ctrl.exitWalk();
  // A pane has no measurable size while hidden, so Fit is deferred until it shows.
  if (tab === 'plan'){ planNeedsFit = false; planZoom.refit(); }
  updateSnapState();               // the snapshot pill only lives on the 3D tab
}
let planNeedsFit = false;
function showThree(){
  if (!threeInit){ threeInit = true;
    ctrl = mountScene(document.getElementById('three-canvas'), LAYER_LABELS,
      document.getElementById('three-toggles')); }
  if (ctrl && scene3d){
    if (!sceneLoaded){ ctrl.setScene(scene3d); sceneLoaded = true; }
    else { ctrl.resize(); ctrl.draw(); }
  }
  updateSnapState();
}

// --- 3D snapshot (download the current WebGL view as a PNG) ------------------
// The canvas has no preserveDrawingBuffer, so its pixels are only valid until the
// browser composites: draw and read the buffer in the SAME synchronous task, with
// no await in between. toDataURL is synchronous, so it captures what draw() just
// rendered (orbit or walk — it's the same canvas). Reuses downloadBlob.
const snapBtn = document.getElementById('snap-btn');
function updateSnapState(){ snapBtn.hidden = !(currentTab === 'three' && ctrl && scene3d); }
function dataUrlToBlob(url){
  const comma = url.indexOf(','), bin = atob(url.slice(comma + 1));
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type:'image/png' });
}
function snapshot3d(){
  if (!ctrl || !scene3d) return;
  const canvas = document.getElementById('three-canvas');
  ctrl.draw();                                   // draw first…
  let url;
  try { url = canvas.toDataURL('image/png'); }   // …then read, same task, no yield
  catch (e){ showNotice('Could not capture the 3D view.'); return; }
  downloadBlob(dataUrlToBlob(url), planSlug() + '-3d.png');
}
snapBtn.addEventListener('click', snapshot3d);
function renderViews(p){
  if (!p.elevations){ viewsPane.innerHTML = '<div class="diag-empty">No views.</div>'; return; }
  const order = [['south','South'],['north','North'],['east','East'],['west','West']];
  let html = '<div class="views-grid">';
  for (const pair of order){ const svg = p.elevations[pair[0]];
    if (svg) html += '<figure data-view="' + pair[0] + '" title="Click to zoom"><figcaption>' +
      pair[1] + ' elevation</figcaption><div class="svgbox">' + svg + '</div></figure>'; }
  if (p.section) html += '<figure data-view="section" title="Click to zoom">' +
    '<figcaption>Section</figcaption><div class="svgbox">' + p.section + '</div></figure>';
  if (p.site_svg) html += '<figure data-view="site" title="Click to zoom">' +
    '<figcaption>Site plan</figcaption><div class="svgbox">' + p.site_svg + '</div></figure>';
  viewsPane.innerHTML = html + '</div>';
}

// --- elevation lightbox (a zoomable single-view overlay) --------------------
const lb = document.getElementById('lightbox');
const lbSvg = document.getElementById('lb-svg');
const lbTitle = document.getElementById('lb-title');
const lbZoom = makeZoom(lbSvg, {
  onChange: pct => { document.getElementById('lb-zpct').textContent = pct + '%'; },
});
const VIEW_LABEL = { south:'South elevation', north:'North elevation', east:'East elevation',
  west:'West elevation', section:'Section', site:'Site plan' };
function openLightbox(view){
  const p = lastGood; if (!p) return;
  const svg = view === 'section' ? p.section
    : (view === 'site' ? p.site_svg : (p.elevations && p.elevations[view]));
  if (!svg) return;
  lbTitle.textContent = VIEW_LABEL[view] || 'View';
  lbSvg.innerHTML = svg;
  lb.hidden = false;
  lbZoom.refit();
  try { lbSvg.focus({ preventScroll:true }); } catch(_){}
}
function closeLightbox(){ lb.hidden = true; lbSvg.innerHTML = ''; }
viewsPane.addEventListener('click', e => {
  const fig = e.target.closest('figure[data-view]'); if (!fig) return;
  openLightbox(fig.getAttribute('data-view'));
});
document.getElementById('lb-close').addEventListener('click', closeLightbox);
document.getElementById('lb-zoom').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  const z = b.getAttribute('data-z');
  if (z === 'in') lbZoom.zoomIn(); else if (z === 'out') lbZoom.zoomOut(); else lbZoom.fit();
});
lb.addEventListener('click', e => { if (e.target === lb) closeLightbox(); });

// --- Report tab (cost / schedules / energy / areas) -------------------------
// Rendered from the server-computed `report` block on the compile payload (cost,
// schedules, energy and areas all come from the pure engine functions). The same
// reportHTML() string feeds the Print packet, so the two never drift.
function money(v){ return '$' + Math.round(v).toLocaleString('en-US'); }
function sqft(v){ return Math.round(v).toLocaleString('en-US') + ' sq ft'; }

function reportHTML(rep){
  if (!rep || rep.error || !rep.cost){
    const why = rep && rep.error ? ('Report unavailable: ' + esc(rep.error))
      : 'No report yet — compile a plan to see cost, schedules and areas.';
    return '<div class="rempty">' + why + '</div>';
  }
  return costSection(rep) + scheduleSections(rep) + energySection(rep) + areaSection(rep);
}
function costSection(rep){
  const est = rep.cost, t = est.total;
  let rows = '', lastGroup = null;
  for (const ln of est.assemblies){
    if (ln.group !== lastGroup){ lastGroup = ln.group;
      rows += '<tr class="grp"><td colspan="4">' + esc(ln.group) + '</td></tr>'; }
    rows += '<tr><td>' + esc(ln.item) + '</td>' +
      '<td class="num">' + trimNum(ln.quantity) + ' ' + esc(ln.unit) + '</td>' +
      '<td class="num">' + money(ln.unit_cost) + '</td>' +
      '<td class="num">' + money(ln.cost) + '</td></tr>';
  }
  let subs = '';
  for (const g in est.subtotals)
    subs += '<tr><td>' + esc(g) + '</td><td class="num">' + money(est.subtotals[g]) + '</td></tr>';
  const cps = rep.cost_per_sqft != null ? (money(rep.cost_per_sqft) + '/sq ft') : '—';
  const mult = est.multiplier !== 1 ? (' · regional ×' + est.multiplier) : '';
  return '<div class="rsec"><h3>Cost estimate' + esc(mult) + '</h3>' +
    '<div class="rchips"><span class="chip good">' + money(t.expected) + ' expected</span>' +
    '<span class="chip">' + money(t.low) + ' – ' + money(t.high) + ' (±' + est.band_pct + '%)</span>' +
    '<span class="chip">' + cps + '</span></div>' +
    '<table class="rtab"><tr><th>Item</th><th class="num">Qty</th>' +
    '<th class="num">Unit cost</th><th class="num">Cost</th></tr>' + rows + '</table>' +
    '<table class="rtab" style="max-width:460px"><tr><th>Assembly subtotal</th>' +
    '<th class="num">Cost</th></tr>' + subs +
    '<tr class="total"><td>Estimated total (expected)</td><td class="num">' + money(t.expected) +
    '</td></tr></table>' +
    '<p class="rnote">' + esc(est.disclaimer) + '</p></div>';
}
function scheduleSections(rep){
  let out = '';
  for (const s of (rep.schedules || [])){
    const head = s.columns.map(c => '<th>' + esc(c) + '</th>').join('');
    const body = s.rows.length
      ? s.rows.map(r => '<tr>' + r.map(c => '<td>' + esc(c) + '</td>').join('') + '</tr>').join('')
      : '<tr><td colspan="' + s.columns.length + '">None.</td></tr>';
    out += '<div class="rsec"><h3>' + esc(s.title) + ' (' + s.count + ')</h3>' +
      '<table class="rtab"><tr>' + head + '</tr>' + body + '</table></div>';
  }
  return out;
}
function energySection(rep){
  const e = rep.energy; if (!e) return '';       // skipped entirely with no climate zone
  const t = e.targets;
  const rows = [['Ceiling', t.ceiling], ['Walls', t.wall], ['Floor', t.floor],
    ['Slab edge', t.slab || '—'], ['Windows', 'U-' + t.window_u]]
    .map(p => '<tr><td>' + esc(p[0]) + '</td><td>' + esc(p[1]) + '</td></tr>').join('');
  return '<div class="rsec"><h3>Energy &amp; climate</h3>' +
    '<p class="rmeta">IECC climate zone ' + e.zone + ' — prescriptive envelope targets (approx.).</p>' +
    '<table class="rtab" style="max-width:420px"><tr><th>Assembly</th><th>Target</th></tr>' +
    rows + '</table>' +
    '<p class="rnote">On a steel frame, run the wall insulation as continuous exterior ' +
    'insulation — steel studs are a severe thermal bridge that guts the cavity R-value. ' +
    'Confirm against the adopted energy code (ideally with a rater).</p></div>';
}
function areaSection(rep){
  const a = rep.areas;
  const rows = a.rooms.length
    ? a.rooms.map(r => '<tr><td>' + esc(r.name) + '</td><td>' + esc(r.type) + '</td>' +
        '<td class="num">' + r.level + '</td>' +
        '<td class="num">' + fmtFtIn(r.width) + ' × ' + fmtFtIn(r.length) + '</td>' +
        '<td class="num">' + sqft(r.area) + '</td></tr>').join('')
    : '<tr><td colspan="5">No rooms.</td></tr>';
  return '<div class="rsec"><h3>Areas</h3>' +
    '<table class="rtab"><tr><th>Room</th><th>Type</th><th class="num">Level</th>' +
    '<th class="num">Dimensions</th><th class="num">Area</th></tr>' + rows +
    '<tr class="total"><td colspan="4">Total room area</td><td class="num">' +
    sqft(a.total_area) + '</td></tr></table>' +
    '<p class="rnote">Footprint ' + sqft(a.footprint_sqft) + ' · interior (conditioned) ' +
    sqft(a.interior_sqft) + ' · habitable ' + sqft(a.habitable_sqft) + '.</p></div>';
}
function renderReport(p){ reportWrap.innerHTML = reportHTML(p && p.report); }
renderReport(null);

// --- Print packet (a self-contained, print-ready window) --------------------
// Composed client-side from the payload the SPA already holds — so it carries the
// elevations + section the server packet omits, needs no round-trip, and never
// touches the editor text or autosave state.
const printBtn = document.getElementById('print-btn');
const PRINT_CSS =
  '*{box-sizing:border-box;}' +
  'body{font-family:Helvetica,Arial,sans-serif;color:#222;margin:0;line-height:1.4;}' +
  '.sheet{padding:34px 42px;page-break-after:always;}' +
  '.sheet:last-child{page-break-after:auto;}' +
  '.cover{padding-top:120px;}' +
  'h1{font-size:30px;margin:0 0 6px;}' +
  'h2{font-size:19px;margin:0 0 14px;border-bottom:2px solid #8A4B12;padding-bottom:6px;}' +
  '.tb{color:#555;font-size:15px;}' +
  '.pnote{color:#777;font-size:12px;font-style:italic;margin-top:10px;}' +
  '.svgwrap{border:1px solid #ddd;padding:10px;overflow-x:auto;}' +
  '.svgwrap svg,figure svg{max-width:100%;height:auto;}' +
  // The true-scale wrapper is sized in physical inches; the SVG must fill it
  // exactly — max-width alone would never grow a drawing up to the stated
  // scale, and border-box would fold the wrapper's padding/border into the
  // inch width, shaving ~2% off the printed scale.
  '.svgwrap.scaled{box-sizing:content-box;}' +
  '.svgwrap.scaled svg{width:100%;height:auto;display:block;}' +
  '.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}' +
  'figure{margin:0;border:1px solid #ddd;border-radius:6px;overflow:hidden;padding:8px;}' +
  'figcaption{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#777;margin-bottom:6px;}' +
  '.rsec{margin:0 0 22px;}' +
  '.rsec h3{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:#777;margin:0 0 8px;border-bottom:1px solid #ddd;padding-bottom:5px;}' +
  '.rtab{border-collapse:collapse;width:100%;font-size:12.5px;margin:2px 0 8px;}' +
  '.rtab th,.rtab td{text-align:left;padding:4px 9px;border-bottom:1px solid #ddd;}' +
  '.rtab th{background:#f6f6f6;}' +
  '.rtab td.num,.rtab th.num{text-align:right;font-variant-numeric:tabular-nums;}' +
  '.rtab tr.grp td{font-weight:700;padding-top:10px;border-bottom:0;}' +
  '.rtab tr.total td{font-weight:700;border-top:2px solid #222;border-bottom:0;}' +
  '.rnote{font-size:11.5px;color:#777;font-style:italic;margin:6px 0 0;}' +
  '.rmeta{font-size:12.5px;color:#555;margin:0 0 10px;}' +
  '.rchips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px;}' +
  '.chip{font-size:12px;font-weight:600;padding:3px 9px;border-radius:20px;border:1px solid #ddd;}' +
  '.rempty{color:#777;padding:12px 0;}' +
  '@media print{.sheet{padding:0;}@page{margin:14mm;}}';

function buildPrintDoc(p){
  const title = p.title || 'Barndominium plan';
  const score = p.score ? (fmt(p.score.total) + ' / 100') : '—';
  const m = p.metrics || {};
  const foot = m.footprint_sqft != null ? (sqft(m.footprint_sqft) + ' footprint') : '';
  const date = new Date().toLocaleDateString();
  const elevs = p.elevations || {};
  const order = [['south','South'],['north','North'],['east','East'],['west','West']];
  let elevHtml = '';
  for (const pair of order){ if (elevs[pair[0]]) elevHtml +=
    '<figure><figcaption>' + pair[1] + ' elevation</figcaption>' + elevs[pair[0]] + '</figure>'; }
  if (p.section) elevHtml += '<figure><figcaption>Section</figcaption>' + p.section + '</figure>';
  // Print the plan to a true architectural scale: use the scale-bar render and
  // size it in physical inches (the server picked the largest scale that fits
  // Letter). The stated scale + graphic bar survive any browser print margin.
  const pr = p.print || {};
  const planSvgSrc = p.print_svg || p.svg;
  const wStyle = pr.css_width_in ? ' style="width:' + pr.css_width_in + 'in;max-width:100%;"' : '';
  const scaleTb = pr.note ? ' · ' + esc(pr.note) : '';
  const body =
    '<section class="sheet cover"><h1>' + esc(title) + '</h1>' +
      '<p class="tb">Drawing packet · ' + esc(date) + ' · score ' + esc(score) +
      (foot ? ' · ' + esc(foot) : '') + scaleTb + '</p></section>' +
    '<section class="sheet"><h2>Floor plan</h2>' +
      (pr.note ? '<p class="tb">' + esc(pr.note) + '</p>' : '') +
      '<div class="svgwrap' + (pr.css_width_in ? ' scaled' : '') + '"' + wStyle + '>' + planSvgSrc + '</div>' +
      '<p class="pnote">Drawn to architectural scale — verify against the graphic scale bar.</p></section>' +
    (elevHtml ? '<section class="sheet"><h2>Elevations &amp; section</h2><div class="grid">' +
      elevHtml + '</div></section>' : '') +
    '<section class="sheet"><h2>Report</h2>' + reportHTML(p.report) + '</section>';
  return '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>' + esc(title) + ' — packet</title><style>' + PRINT_CSS + '</style></head><body>' +
    body + '<script>window.onload=function(){setTimeout(function(){try{window.print();}' +
    'catch(e){}},150);};<\/script></body></html>';
}
function openPrint(){
  const p = lastGood;
  if (!p || !p.svg){ showNotice('Compile a clean plan before printing the packet.'); return; }
  const w = window.open('', '_blank');
  if (!w){ showNotice('Allow pop-ups to open the printable packet.'); return; }
  const doc = buildPrintDoc(p);
  w.document.open(); w.document.write(doc); w.document.close();
}
printBtn.addEventListener('click', openPrint);

// --- viewport zoom / pan (CSS transform) ------------------------------------
// A reusable controller over a `.svgbox` holding one <svg>: transform-driven
// zoom + drag-pan, a Fit that fills the pane from the SVG's intrinsic size, and
// keyboard +/-/0. Shared by the 2D plan and the elevation lightbox.
function makeZoom(box, opts){
  opts = opts || {};
  const MIN = 0.05, MAX = 12, PAD = 12;
  let scale = 1, tx = 0, ty = 0, fitScale = 1;
  let dragging = false, sx = 0, sy = 0, moved = false;
  function svg(){ return box.querySelector('svg'); }
  function intrinsic(el){
    let w = parseFloat(el.getAttribute('width')), h = parseFloat(el.getAttribute('height'));
    if (!(w > 0) || !(h > 0)){
      const vb = (el.getAttribute('viewBox') || '').split(/[ ,]+/).map(Number);
      if (vb.length === 4){ w = vb[2]; h = vb[3]; }
    }
    return { w: w || el.clientWidth || 1, h: h || el.clientHeight || 1 };
  }
  function apply(){ const el = svg(); if (!el) return;
    el.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
    if (opts.onChange) opts.onChange(Math.round((scale / (fitScale || 1)) * 100)); }
  function fit(){ const el = svg(); if (!el) return;
    const bw = box.clientWidth, bh = box.clientHeight; if (!bw || !bh) return;
    const it = intrinsic(el);
    fitScale = Math.min((bw - 2 * PAD) / it.w, (bh - 2 * PAD) / it.h);
    if (!(fitScale > 0) || !isFinite(fitScale)) fitScale = 1;
    scale = fitScale;
    tx = (bw - it.w * scale) / 2; ty = (bh - it.h * scale) / 2; apply();
  }
  function zoomAt(factor, cx, cy){ const el = svg(); if (!el) return;
    const r = box.getBoundingClientRect();
    if (cx == null){ cx = r.width / 2; cy = r.height / 2; } else { cx -= r.left; cy -= r.top; }
    const ns = Math.min(MAX, Math.max(MIN, scale * factor));
    tx = cx - (cx - tx) * (ns / scale); ty = cy - (cy - ty) * (ns / scale);
    scale = ns; apply();
  }
  box.addEventListener('wheel', e => { e.preventDefault();
    zoomAt(Math.exp(-e.deltaY * 0.0012), e.clientX, e.clientY); }, { passive:false });
  // --- unified pointer input: one finger / mouse pans, two fingers pinch-zoom ---
  // Pointer Events cover mouse, touch and pen with one code path. Each active
  // pointer is captured (so a drag that leaves the box still tracks) and remembered
  // in `ptrs`; a second pointer promotes the gesture to a pinch around the two-finger
  // midpoint (reusing zoomAt, exactly like the +/− buttons), driving the same scale.
  // pointercancel (iOS fires it when it steals the gesture) tears down cleanly — no
  // stuck pan. Mouse behaviour is unchanged: a single mouse pointer only ever pans.
  const ptrs = new Map();          // pointerId -> {x,y}
  let pinchD = 0;                  // last two-finger distance (0 = not pinching)
  let lastTapT = 0, lastTapX = 0, lastTapY = 0;   // touch double-tap → fit
  function twoPts(){ return Array.from(ptrs.values()); }
  box.addEventListener('pointerdown', e => { if (!svg()) return;
    ptrs.set(e.pointerId, { x:e.clientX, y:e.clientY });
    try { box.setPointerCapture(e.pointerId); } catch(_){}
    if (ptrs.size >= 2){                     // promote to pinch: stop the pan
      dragging = false; const p = twoPts();
      pinchD = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      box.style.cursor = ''; return;
    }
    dragging = true; moved = false;
    sx = e.clientX - tx; sy = e.clientY - ty;
    if (box.hasAttribute('tabindex')) try { box.focus({ preventScroll:true }); } catch(_){}
    box.style.cursor = 'grabbing'; });
  box.addEventListener('keydown', zoomKeys);
  box.addEventListener('pointermove', e => {
    if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, { x:e.clientX, y:e.clientY });
    if (pinchD && ptrs.size >= 2){           // pinch: zoom about the moving midpoint
      const p = twoPts(), d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      const mx = (p[0].x + p[1].x) / 2, my = (p[0].y + p[1].y) / 2;
      if (d > 0 && pinchD > 0) zoomAt(d / pinchD, mx, my);
      pinchD = d; moved = true; return;
    }
    if (!dragging) return;
    tx = e.clientX - sx; ty = e.clientY - sy; moved = true; apply(); });
  function endPtr(e){
    ptrs.delete(e.pointerId);
    try { box.releasePointerCapture(e.pointerId); } catch(_){}
    if (ptrs.size < 2) pinchD = 0;
    if (ptrs.size === 1){                     // one finger lifted from a pinch → resume pan
      const p = twoPts()[0]; dragging = true; moved = true; sx = p.x - tx; sy = p.y - ty; return;
    }
    if (ptrs.size === 0){ dragging = false; box.style.cursor = '';
      if (!moved){
        // Touch double-tap on empty space = Fit (mirrors the Fit button / dblclick).
        if (e.pointerType && e.pointerType !== 'mouse'){
          const now = Date.now();
          const onRoom = e.target && e.target.closest && e.target.closest('[data-room]');
          if (!onRoom && now - lastTapT < 320 &&
              Math.abs(e.clientX - lastTapX) < 32 && Math.abs(e.clientY - lastTapY) < 32){
            lastTapT = 0; fit();
          } else { lastTapT = now; lastTapX = e.clientX; lastTapY = e.clientY; }
        }
        if (opts.onClick) opts.onClick(e);
      }
    }
  }
  box.addEventListener('pointerup', endPtr);
  box.addEventListener('pointercancel', endPtr);
  box.addEventListener('dblclick', () => fit());   // mouse double-click (unchanged)
  return {
    fit,
    zoomIn(){ zoomAt(1.25); }, zoomOut(){ zoomAt(0.8); },
    // fit lazily on the first render (once the pane has a measurable size)
    refit(){ requestAnimationFrame(fit); },
  };
}

const planZoom = makeZoom(planSvg, {
  onChange: pct => { document.getElementById('plan-zpct').textContent = pct + '%'; },
  onClick: planClickToSource,
});
document.getElementById('plan-zoom').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  const z = b.getAttribute('data-z');
  if (z === 'in') planZoom.zoomIn(); else if (z === 'out') planZoom.zoomOut(); else planZoom.fit();
});

// Click a room on the (non-edit) plan → jump the editor to its source line + flash.
function planClickToSource(e){
  if (editMode) return;
  const rect = e.target && e.target.closest ? e.target.closest('[data-room]') : null;
  if (!rect) return;
  const id = rect.getAttribute('data-room');
  const room = (lastGood && lastGood.rooms || []).find(r => r.id === id);
  const ln = room && room.line;
  if (ln){ jumpToLine(ln); flashLine(ln); }
}

// Keyboard zoom when the plan viewport has focus (+/-/0). The plan pane is made
// focusable so these don't fight the editor's own key handling.
function zoomKeys(e){
  if (e.key === '+' || e.key === '=' ){ e.preventDefault(); activeZoom().zoomIn(); }
  else if (e.key === '-' || e.key === '_'){ e.preventDefault(); activeZoom().zoomOut(); }
  else if (e.key === '0'){ e.preventDefault(); activeZoom().fit(); }
}
function activeZoom(){ return (lb && !lb.hidden) ? lbZoom : planZoom; }

// --- examples menu ----------------------------------------------------------
fetch('/api/examples').then(r => r.json()).then(list => {
  const sel = document.getElementById('example-select');
  sel.innerHTML = '<option value="">load example…</option>';
  for (const ex of list){
    const o = document.createElement('option');
    o.value = ex.name; o.textContent = ex.name; o.dataset.src = ex.source;
    sel.appendChild(o);
  }
  sel.addEventListener('change', () => {
    const opt = sel.selectedOptions[0];
    if (opt && opt.dataset.src != null){ hideNotice(); setSource(opt.dataset.src, 'load example'); }
  });
}).catch(() => {});

// --- agent chat pane --------------------------------------------------------
const agentPane = document.getElementById('agent-pane');
const thread = document.getElementById('thread');
const briefEl = document.getElementById('brief');
const sendBtn = document.getElementById('send-btn');
const stopBtn = document.getElementById('stop-btn');
const agentNote = document.getElementById('agent-note');
const agentSub = document.getElementById('agent-sub');
const collapseBtn = document.getElementById('agent-collapse');

let agentAvailable = false;
let running = false;
let jobId = null;
let hasResult = false;   // has the agent landed a plan in this conversation yet?
let statusEl = null;     // the live status bubble shown while a job streams

const PHASE_LABEL = { starting:'starting', writing:'writing DSL', compiling:'compiling',
  critiquing:'critiquing', revising:'revising' };

// A collapse clears the pane's explicit width so the `.collapsed` 38px rule wins
// (inline styles beat the class); expanding restores the dragged width.
let agentSavedW = '';
collapseBtn.addEventListener('click', () => {
  const collapsed = agentPane.classList.toggle('collapsed');
  collapseBtn.textContent = collapsed ? '›' : '‹';
  collapseBtn.title = (collapsed ? 'expand' : 'collapse') + ' the agent pane';
  if (collapsed){ agentSavedW = agentPane.style.width; agentPane.style.width = ''; }
  else if (agentSavedW){ agentPane.style.width = agentSavedW; }
  afterSplitResize();
});

// --- resizable split panes (agent | editor | viewport) ----------------------
// Two 6px handles drive the flex row by writing an explicit width onto the pane on
// each handle's left (the agent section, then the editor column); the viewport
// (flex:1) takes the remainder. Widths are clamped to sensible minimums, persisted
// per-split in localStorage, and reset by double-clicking a handle. After every
// tick the self-measuring panes are nudged (the window didn't resize, only the
// split did): the live 3D canvas re-sizes, the 2D plan re-fits.
const mainEl = document.querySelector('main');
const leftCol = document.querySelector('.left');
const splitAgent = document.getElementById('split-agent');
const splitEditor = document.getElementById('split-editor');
const AGENT_MIN = 200, EDITOR_MIN = 320, VIEWPORT_MIN = 360, HANDLES = 12;
function afterSplitResize(){
  if (currentTab === 'three' && ctrl){ ctrl.resize(); ctrl.draw(); }
  else if (currentTab === 'plan') planZoom.refit();
  else planNeedsFit = true;
}
function agentWidthNow(){
  return agentPane.classList.contains('collapsed') ? 38 : agentPane.getBoundingClientRect().width;
}
function clampAgent(px){
  const room = mainEl.clientWidth - EDITOR_MIN - VIEWPORT_MIN - HANDLES;
  return Math.max(AGENT_MIN, Math.min(px, Math.max(AGENT_MIN, room)));
}
function clampEditor(px){
  const room = mainEl.clientWidth - agentWidthNow() - VIEWPORT_MIN - HANDLES;
  return Math.max(EDITOR_MIN, Math.min(px, Math.max(EDITOR_MIN, room)));
}
function persistSplits(){
  try {
    if (agentPane.style.width) localStorage.setItem(LS_AGENT_W, parseInt(agentPane.style.width, 10));
    if (leftCol.style.width) localStorage.setItem(LS_EDITOR_W, parseInt(leftCol.style.width, 10));
  } catch (e){}
}
function startSplit(which, ev){
  if (which === 'agent' && agentPane.classList.contains('collapsed')) return;   // handle no-ops
  const handle = which === 'agent' ? splitAgent : splitEditor;
  handle.classList.add('dragging');
  const prevCursor = document.body.style.cursor, prevSel = document.body.style.userSelect;
  document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
  try { handle.setPointerCapture(ev.pointerId); } catch (_){}
  function move(e){
    const left = mainEl.getBoundingClientRect().left;
    if (which === 'agent') agentPane.style.width = clampAgent(e.clientX - left) + 'px';
    else leftCol.style.width = clampEditor(e.clientX - left - agentWidthNow() - 6) + 'px';
    afterSplitResize();
  }
  function up(e){
    handle.removeEventListener('pointermove', move);
    handle.removeEventListener('pointerup', up);
    handle.classList.remove('dragging');
    document.body.style.cursor = prevCursor; document.body.style.userSelect = prevSel;
    try { handle.releasePointerCapture(e.pointerId); } catch (_){}
    persistSplits(); afterSplitResize();
  }
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up);
  ev.preventDefault();
}
splitAgent.addEventListener('pointerdown', e => startSplit('agent', e));
splitEditor.addEventListener('pointerdown', e => startSplit('editor', e));
// Double-click a handle → drop the explicit width and the saved key, back to the
// CSS default (agent 308px, editor 36%).
splitAgent.addEventListener('dblclick', () => {
  agentPane.style.width = ''; agentSavedW = '';
  try { localStorage.removeItem(LS_AGENT_W); } catch (e){}
  afterSplitResize();
});
splitEditor.addEventListener('dblclick', () => {
  leftCol.style.width = '';
  try { localStorage.removeItem(LS_EDITOR_W); } catch (e){}
  afterSplitResize();
});
(function restoreSplits(){
  try {
    const aw = localStorage.getItem(LS_AGENT_W);
    if (aw) agentPane.style.width = clampAgent(parseFloat(aw)) + 'px';
    const ew = localStorage.getItem(LS_EDITOR_W);
    if (ew) leftCol.style.width = clampEditor(parseFloat(ew)) + 'px';
  } catch (e){}
})();

function addMsg(cls, text){
  const el = document.createElement('div');
  el.className = cls; el.textContent = text;
  thread.appendChild(el); thread.scrollTop = thread.scrollHeight;
  return el;
}
function setStatus(text){
  if (!statusEl){
    statusEl = document.createElement('div');
    statusEl.className = 'msg-status';
    statusEl.innerHTML = '<span class="spin"></span><span class="txt"></span>';
    thread.appendChild(statusEl);
  }
  statusEl.querySelector('.txt').textContent = text;
  thread.scrollTop = thread.scrollHeight;
}
function clearStatus(){ if (statusEl){ statusEl.remove(); statusEl = null; } }

function iterRow(ev){
  const row = document.createElement('div');
  row.className = 'iter-row'; row.dataset.round = ev.round;
  const t = ev.score ? ev.score.total : null;
  const c = ev.counts || { error:0, warning:0, info:0 };
  row.innerHTML = '<span class="rn">round ' + ev.round + '</span>' +
    (t == null ? '<span class="sc">—</span>'
               : '<span class="sc ' + scoreClass(t) + '">' + fmt(t) + '</span>') +
    '<span class="ct">' + (c.error | 0) + 'e ' + (c.warning | 0) + 'w ' +
    (c.info | 0) + 'i</span>';
  thread.appendChild(row); thread.scrollTop = thread.scrollHeight;
}
function applyIteration(ev){
  if (ev.source != null){ editor.value = ev.source; renderGutter(); }
  if (ev.payload) applyResult(ev.payload);   // viewport evolves live per round
}

function setRunning(on){
  running = on;
  sendBtn.disabled = on || !agentAvailable;
  briefEl.disabled = on || !agentAvailable;
  stopBtn.hidden = !on; stopBtn.disabled = false;
}

async function sendDesign(){
  if (running || !agentAvailable) return;
  const brief = briefEl.value.trim();
  if (!brief) return;
  autosaveOff = false;   // a design run is a deliberate action — resume autosave
  hideNotice();
  addMsg('msg-user', brief);
  briefEl.value = '';
  const body = { brief, iterations: 3 };
  // Follow-ups refine the current editor plan; the first brief starts fresh.
  if (hasResult) body.source = editor.value;
  setRunning(true); clearStatus(); setStatus('starting'); jobId = null;
  try {
    const resp = await fetch('/api/design', { method:'POST',
      headers:{ 'Content-Type':'application/json' }, body: JSON.stringify(body) });
    if (resp.status === 409){ clearStatus();
      addMsg('msg-agent err', 'A design job is already running — please wait.'); return; }
    if (!resp.ok || !resp.body){ clearStatus();
      addMsg('msg-agent err', 'Design request failed (' + resp.status + ').'); return; }
    await readSSE(resp.body.getReader());
  } catch (err){
    clearStatus(); addMsg('msg-agent err', 'Connection error: ' + String(err));
  } finally {
    setRunning(false); jobId = null;
  }
}

async function readSSE(reader){
  const dec = new TextDecoder();
  let buf = '';
  for (;;){
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream:true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0){
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
      handleFrame(frame);
    }
  }
}
function handleFrame(frame){
  let event = 'message', data = '';
  for (const line of frame.split('\n')){
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  if (!data) return;
  let ev; try { ev = JSON.parse(data); } catch (e){ return; }
  onEvent(event, ev);
}
function onEvent(kind, ev){
  if (kind === 'status'){
    if (ev.job) jobId = ev.job;
    const label = PHASE_LABEL[ev.phase] || ev.phase;
    setStatus(ev.round ? (label + ' — round ' + ev.round + ' of ' + ev.rounds) : label);
  } else if (kind === 'iteration'){
    iterRow(ev); applyIteration(ev);
  } else if (kind === 'done'){
    clearStatus();
    // Compare against the last COMMITTED state, not the screen: the per-round
    // previews write editor.value directly (deliberately not undo steps), so by
    // now the winner usually matches the screen — but it still has to join the
    // history, or the first undo would skip both it and the pre-run source.
    if (ev.source != null && ev.source !== histMirror){
      applyEdit(ev.source, null, null, 'agent design'); renderGutter();
    } else if (ev.source != null){
      editor.value = ev.source; renderGutter();     // identical to committed: no entry
    }
    if (ev.payload) applyResult(ev.payload);
    hasResult = true;
    const win = thread.querySelector('.iter-row[data-round="' + ev.round + '"]');
    if (win) win.classList.add('win');
    const t = ev.score ? fmt(ev.score.total) : '—';
    addMsg('msg-agent', 'Landed the best plan (round ' + ev.round + ' of ' +
      ev.iterations + ', score ' + t + '/100). Edit it, or send a follow-up to refine.');
  } else if (kind === 'error'){
    clearStatus();
    const msg = ev.kind === 'cancelled' ? 'Stopped.'
      : (ev.message || 'The agent hit an error.') + (ev.kind ? '  (' + ev.kind + ')' : '');
    addMsg('msg-agent err', msg);
  }
}

sendBtn.addEventListener('click', sendDesign);
briefEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); sendDesign(); }
});
stopBtn.addEventListener('click', () => {
  if (!jobId) return;
  stopBtn.disabled = true;
  fetch('/api/design/cancel', { method:'POST',
    headers:{ 'Content-Type':'application/json' }, body: JSON.stringify({ id: jobId }) })
    .catch(() => {});
});

function initAgent(){
  fetch('/api/agent').then(r => r.json()).then(a => {
    agentAvailable = !!a.available;
    if (agentAvailable){
      agentSub.textContent = 'ready';
      agentNote.textContent = 'Describe a plan, or send a follow-up to refine the current one. ⌘/Ctrl+Enter to send.';
      setRunning(false);
    } else {
      agentSub.textContent = 'off';
      briefEl.disabled = true; sendBtn.disabled = true;
      agentNote.className = 'agent-note bad';
      agentNote.textContent = a.reason || "pip install 'barndsl[agent]' and set ANTHROPIC_API_KEY";
    }
  }).catch(() => {
    briefEl.disabled = true; sendBtn.disabled = true;
    agentNote.className = 'agent-note bad';
    agentNote.textContent = 'agent status unavailable';
  });
}
initAgent();

// --- offline (rule-based) Design form ---------------------------------------
// Turns the small form into a POST /api/layout call (no API key), then loads the
// returned DSL through setSource — the same funnel New/Open/example use, so it is
// exactly ONE undoable checkpoint. Works whether or not the Claude agent is on.
const OD_EXTRAS = [
  ['garage', '2-car garage'], ['shop', 'shop bay'], ['office', 'office'],
  ['dining', 'dining'], ['mudroom', 'mudroom'], ['laundry', 'laundry'],
];
const odBtn = document.getElementById('od-btn');
const odNote = document.getElementById('od-note');
(function initOfflineExtras(){
  const wrap = document.getElementById('od-extras');
  if (!wrap) return;
  wrap.innerHTML = OD_EXTRAS.map(([v, lbl]) =>
    '<label><input type="checkbox" class="od-x" value="' + v + '"' +
    (v === 'garage' ? ' checked' : '') + '> ' + esc(lbl) + '</label>').join('');
})();
function odNum(id, def){ const v = parseFloat(document.getElementById(id).value);
  return isFinite(v) ? v : def; }
async function designOffline(){
  const extras = Array.prototype.map.call(
    document.querySelectorAll('.od-x:checked'), c => c.value);
  const body = {
    bedrooms: odNum('od-beds', 3), bathrooms: odNum('od-baths', 2),
    width: odNum('od-w', 40), length: odNum('od-l', 30),
    open_kitchen: document.getElementById('od-open').checked,
    extras: extras, name: (lastGood && lastGood.settings && lastGood.settings.name) || 'My Barndo',
  };
  odBtn.disabled = true;
  if (odNote){ odNote.className = 'agent-note'; odNote.textContent = 'Laying out…'; }
  try {
    const resp = await fetch('/api/layout', { method:'POST',
      headers:{ 'Content-Type':'application/json' }, body: JSON.stringify(body) });
    const j = await resp.json();
    if (!resp.ok || !j || j.error){
      const msg = (j && j.error && (j.error.message || j.error)) || ('layout failed (' + resp.status + ')');
      if (odNote){ odNote.className = 'agent-note bad'; odNote.textContent = String(msg); }
      return;
    }
    setSource(j.source, 'design offline');   // one undoable checkpoint
    if (odNote){ odNote.className = 'agent-note';
      odNote.textContent = 'Laid out a starting plan — edit it, or tweak the form and design again.'; }
  } catch (err){
    if (odNote){ odNote.className = 'agent-note bad'; odNote.textContent = 'Connection error: ' + String(err); }
  } finally {
    odBtn.disabled = false;
  }
}
if (odBtn) odBtn.addEventListener('click', designOffline);

// --- Tier 5: direct-manipulation edit mode ----------------------------------
// An interactive SVG overlay drawn from the payload's `rooms`/`openings`. Drags
// become surgical DSL text edits (POST /api/edit) so the source stays the source
// of truth; the editor text and viewport swap to the server's rewritten source.
const editChk = document.getElementById('edit-mode');
const editLayer = document.getElementById('edit-layer');
const undoBtn = document.getElementById('undo-btn');
const redoBtn = document.getElementById('redo-btn');
const editNoteEl = document.getElementById('edit-note');
const measureBtn = document.getElementById('measure-btn');
const dimChip = document.getElementById('dim-chip');
const planBody = document.querySelector('.plan-body');
const levelSwitch = document.getElementById('level-switch');
const multiCountEl = document.getElementById('multi-count');
const alignTools = document.getElementById('align-tools');
let editLevel = 0;                   // the floor the overlay currently edits
let editMode = false, editReady = false;
let editRooms = [], editOpens = [], editLevels = [0], editFixtures = [], editNotes = [];
let allRooms = [], allOpens = [], allStairs = [], allFixtures = [], allNotes = [], allInstances = [];   // every level — the dimmed underlay
let selectedRoomId = null, svgEl = null, ghostEl = null, drag = null, ov = null;
let editTfEl = null;   // the `.edit-tf` wrapper carrying the touch pan/pinch transform
// Overlay-level multi-selection (rooms only) — distinct from dpSel/selectedRoomId
// (the inspector's single notion). Shift-click toggles; a plain click clears it.
const multiSel = new Set();
// Coarse pointer (finger) — bigger overlay handles + on-screen nudge chevrons.
const COARSE = (function(){ try { return matchMedia('(pointer: coarse)').matches; } catch(_){ return false; } })();

// -- edit-overlay pan/pinch transform (touch only) --
// The overlay <svg> auto-fits the pane via viewBox+preserveAspectRatio, so identity
// IS "fit". Touch pan/pinch layers a CSS transform on the .edit-tf wrapper on top of
// that; a rebuild or a leave resets to identity (back to fit). Desktop never sets it.
let editTf = { s: 1, tx: 0, ty: 0 };
const ETF_MIN = 0.3, ETF_MAX = 8;
function applyEditTf(){ if (editTfEl)
  editTfEl.style.transform = 'translate(' + editTf.tx + 'px,' + editTf.ty + 'px) scale(' + editTf.s + ')'; }
function resetEditTf(){ editTf = { s: 1, tx: 0, ty: 0 }; applyEditTf(); }
// Zoom the overlay about a screen point (pinch midpoint), same math as makeZoom.zoomAt.
function zoomEditAt(factor, cx, cy){
  const r = editLayer.getBoundingClientRect();
  const mx = cx - r.left, my = cy - r.top;
  const ns = Math.min(ETF_MAX, Math.max(ETF_MIN, editTf.s * factor));
  editTf.tx = mx - (mx - editTf.tx) * (ns / editTf.s);
  editTf.ty = my - (my - editTf.ty) * (ns / editTf.s);
  editTf.s = ns; applyEditTf();
}

function snap(v){ return Math.round(v * 2) / 2; }          // 0.5 ft grid
function Y(py){ return ov.MID - py; }                       // plan y (north up) → svg y
function roomById(id){ return editRooms.find(r => r.id === id); }
function openByKey(k){ return editOpens.find(o => o.key === k); }
function roomLine(id){ const r = roomById(id); return r ? r.line : null; }
function editNote(msg, isErr){ editNoteEl.textContent = msg || '';
  editNoteEl.className = 'edit-note' + (isErr ? ' err' : ''); }

// -- overlay multi-selection (rooms) + align / distribute --
// The edit-bar shows a live count and, at 2+ rooms, the align/distribute toolbar.
function renderMultiCount(){
  const n = multiSel.size;
  multiCountEl.textContent = n >= 1 ? n + ' selected' : '';
  alignTools.hidden = n < 2;
}
function toggleMultiSel(id){
  if (!roomById(id)) return;
  if (multiSel.has(id)) multiSel.delete(id); else multiSel.add(id);
  renderMultiCount();
  if (editMode) buildOverlay();
}
function clearMultiSel(rebuild){
  if (!multiSel.size){ renderMultiCount(); return; }
  multiSel.clear(); renderMultiCount();
  if (rebuild && editMode) buildOverlay();
}
function pruneMultiSel(){   // drop ids that vanished (a delete, or a floor switch)
  let changed = false;
  for (const id of Array.from(multiSel)) if (!roomById(id)){ multiSel.delete(id); changed = true; }
  if (changed) renderMultiCount();
}
function selectedRooms(){ return Array.from(multiSel).map(roomById).filter(Boolean); }
function roomsBBox(rooms){
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const r of rooms){ minX = Math.min(minX, r.x); minY = Math.min(minY, r.y);
    maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.l); }
  return { x: minX, y: minY, w: maxX - minX, l: maxY - minY };
}
// Align the selected rooms' edges to the extreme edge (all in plan feet, snap 0.5).
// Overlap / out-of-envelope stays the compiler's job — we never pre-block a move.
function alignRooms(edge){
  const rooms = selectedRooms(); if (rooms.length < 2) return;
  const edits = [];
  if (edge === 'left'){ const t = snap(Math.min.apply(null, rooms.map(r => r.x)));
    for (const r of rooms) if (t !== r.x) edits.push({ kind:'move_room', room:r.id, x:t, y:r.y }); }
  else if (edge === 'right'){ const t = Math.max.apply(null, rooms.map(r => r.x + r.w));
    for (const r of rooms){ const nx = snap(t - r.w); if (nx !== r.x) edits.push({ kind:'move_room', room:r.id, x:nx, y:r.y }); } }
  else if (edge === 'top'){ const t = Math.max.apply(null, rooms.map(r => r.y + r.l));
    for (const r of rooms){ const ny = snap(t - r.l); if (ny !== r.y) edits.push({ kind:'move_room', room:r.id, x:r.x, y:ny }); } }
  else if (edge === 'bottom'){ const t = snap(Math.min.apply(null, rooms.map(r => r.y)));
    for (const r of rooms) if (t !== r.y) edits.push({ kind:'move_room', room:r.id, x:r.x, y:t }); }
  applyEdits(edits, 'align rooms');
}
// Distribute: keep the first and last (by position), equalise the gaps between the
// sorted members. Needs 3+ to have a middle to move; 2 is a clean no-op.
function distributeRooms(axis){
  const rooms = selectedRooms();
  if (rooms.length < 3){ if (rooms.length) editNote('Select 3+ rooms to distribute.'); return; }
  const horiz = axis === 'h';
  const sorted = rooms.slice().sort((a, b) => horiz ? (a.x - b.x) : (a.y - b.y));
  const first = sorted[0], last = sorted[sorted.length - 1];
  const startEdge = horiz ? (first.x + first.w) : (first.y + first.l);
  const endEdge = horiz ? last.x : last.y;
  const middle = sorted.slice(1, -1);
  const totalW = middle.reduce((s, m) => s + (horiz ? m.w : m.l), 0);
  const gap = (endEdge - startEdge - totalW) / (sorted.length - 1);
  let cursor = startEdge;
  const edits = [];
  for (const m of middle){
    const np = snap(cursor + gap);
    if (horiz){ if (np !== m.x) edits.push({ kind:'move_room', room:m.id, x:np, y:m.y }); cursor = np + m.w; }
    else { if (np !== m.y) edits.push({ kind:'move_room', room:m.id, x:m.x, y:np }); cursor = np + m.l; }
  }
  applyEdits(edits, 'distribute rooms');
}

function initEdit(){
  // Parse an inline <svg> so the SVG namespace comes from the DOM (no namespace
  // URL literal in the page — the app stays free of external-looking references).
  // The <svg> nests in a `.edit-tf` wrapper that carries the touch pan/pinch
  // transform (see the CSS note): scaling the wrapper, not the <svg>, keeps
  // svgEl.getScreenCTM() — which toPlan() relies on — exact under a two-finger zoom.
  editLayer.innerHTML = '<div class="edit-tf"><svg preserveAspectRatio="xMidYMid meet"></svg></div>';
  editTfEl = editLayer.firstChild;
  svgEl = editTfEl.firstChild;
  svgEl.addEventListener('pointerdown', onDown);
  svgEl.addEventListener('pointermove', onMove);
  svgEl.addEventListener('pointerup', onUp);
  svgEl.addEventListener('pointercancel', onCancel);
  editChk.addEventListener('change', () => {
    editMode = editChk.checked; editLayer.hidden = !editMode;
    planSvg.style.display = editMode ? 'none' : '';
    document.getElementById('plan-zoom').style.display = editMode ? 'none' : '';
    measureBtn.disabled = !editMode;
    if (!editMode) setMeasure(false);
    resetEditTf();
    renderLevelSwitcher();
    if (editMode) buildOverlay();
    else { selectedRoomId = null; clearMultiSel(false); editNote(''); planZoom.refit(); }
  });
  measureBtn.addEventListener('click', () => setMeasure(!measureMode));
  elecBtn.addEventListener('click', () => {
    elecMode = !elecMode;
    elecBtn.classList.toggle('on', elecMode);
    // Swap the plan SVG in place — the electrical variant rides in the payload,
    // so no re-compile and nothing leaves the page (offline).
    if (lastGood) planSvg.innerHTML = planVariant(lastGood);
  });
  dimsBtn.addEventListener('click', () => {
    // Toggle the dimension convention. The face-of-stud variant is baked into
    // the payload (like the electrical layer), so this is a pure in-page swap —
    // one server-rendered knob, no client-side dimension math.
    dimsMode = (dimsMode === 'faces') ? 'nominal' : 'faces';
    dimsBtn.classList.toggle('on', dimsMode === 'faces');
    dimsBtn.textContent = (dimsMode === 'faces') ? '⟺ Dims: faces' : '⟺ Dims: nominal';
    if (lastGood) planSvg.innerHTML = planVariant(lastGood);
  });
  undoBtn.addEventListener('click', doUndo);
  redoBtn.addEventListener('click', doRedo);
  levelSwitch.addEventListener('click', e => {
    const b = e.target.closest('[data-level]'); if (!b) return;
    setEditLevel(parseInt(b.getAttribute('data-level'), 10));
  });
  alignTools.addEventListener('click', e => {
    const b = e.target.closest('[data-btn]'); if (!b) return;
    const a = b.getAttribute('data-btn');
    if (a === 'align-left' || a === 'align-right' || a === 'align-top' || a === 'align-bottom')
      alignRooms(a.slice(6));
    else if (a === 'dist-h') distributeRooms('h');
    else if (a === 'dist-v') distributeRooms('v');
  });
}

// -- the floor switcher (only on plans with >1 level, only in edit mode) --
function levelLabel(lvl){ return lvl === 0 ? 'Ground' : ('Level ' + lvl); }
function renderLevelSwitcher(){
  const multi = editMode && editLevels.length > 1;
  levelSwitch.hidden = !multi;
  if (!multi){ levelSwitch.innerHTML = ''; return; }
  let h = '<span class="lvl-label">Floor</span>';
  for (const lvl of editLevels)
    h += '<button class="lvl-chip' + (lvl === editLevel ? ' on' : '') +
      '" data-level="' + lvl + '">' + esc(levelLabel(lvl)) + '</button>';
  levelSwitch.innerHTML = h;
}
function applyLevelFilter(){
  editRooms = allRooms.filter(r => r.level === editLevel);
  editOpens = allOpens.filter(o => o.level === editLevel);
  editFixtures = allFixtures.filter(f => f.level === editLevel);
  editNotes = allNotes.filter(n => (n.level || 0) === editLevel);
}
function fixtureByKey(k){ return editFixtures.find(f => f.id === k); }
function noteByIndex(i){ return editNotes.find(n => n.index === i); }
function setEditLevel(lvl){
  if (editLevels.indexOf(lvl) < 0 || lvl === editLevel) return;
  editLevel = lvl; selectedRoomId = null;
  clearMultiSel(false);            // selection is per-floor
  applyLevelFilter(); renderLevelSwitcher();
  if (editMode) buildOverlay();
}

function refreshEditData(p){
  if (p && p.rooms){
    allRooms = p.rooms; allOpens = p.openings || []; allStairs = p.stairs || [];
    allFixtures = p.fixtures || [];
    allNotes = p.notes || [];
    allInstances = p.instances || [];
    editLevels = p.levels || [0];
    if (editLevels.indexOf(editLevel) < 0) editLevel = editLevels[0] || 0;  // clamp
    applyLevelFilter(); editReady = true;
    pruneMultiSel();               // a recompile may have removed selected rooms
  }
  renderLevelSwitcher();
  if (editMode) buildOverlay();
  renderPanel();                 // the design panel mirrors the same payload
}

// Fit-or-hide guard for overlay labels. Text and rects share the plan-unit space
// (font-size is authored in feet, like the rect width), so the overlay scales to
// the pane without changing their ratio — a label that overruns its rect (the
// side-by-side washer/dryer, a long fixture kind, a narrow stair) would spill into
// its neighbour at small sizes. Estimate the advance width (~0.6em/char for this
// sans-serif) and, when it won't fit, drop the <text> and keep a <title> tooltip so
// hover still names the element. Deterministic and recomputed on every buildOverlay.
function labelFits(text, fontSize, boxW){
  return (String(text).length * fontSize * 0.6) <= (boxW - 0.4);
}
// One on-screen nudge chevron: a visible dot + glyph over a fat, invisible finger
// halo. `pyPlan` is in plan feet (north up); the halo carries the data-nudge hit.
function nudgeChevron(dir, px, pyPlan, rr, glyph){
  const cy = Y(pyPlan);
  return '<g class="ov-nudge-g" data-nudge="' + dir + '">' +
    '<circle class="ov-nudge" cx="' + px + '" cy="' + cy + '" r="' + rr +
      '" vector-effect="non-scaling-stroke"/>' +
    '<text class="ov-nudge-t" x="' + px + '" y="' + (cy + rr * 0.34) +
      '" text-anchor="middle" font-size="' + (rr * 1.1) + '">' + glyph + '</text>' +
    '<circle class="hit" cx="' + px + '" cy="' + cy + '" r="' + (rr * 1.7) + '"/></g>';
}

function buildOverlay(){
  if (!editMode || !svgEl) return;
  if (!editReady){ svgEl.innerHTML = '';
    editNote('Fix the errors to edit the layout.', true); return; }
  // The viewBox spans every level's rooms (+ stair footprints), so the active
  // floor sits in its true position over the floor below — the underlay you align
  // the loft to. It also stays fixed as you switch floors.
  const boxes = allRooms.map(r => [r.x, r.y, r.x + r.w, r.y + r.l])
    .concat(allStairs.map(t => [t.x, t.y, t.x + t.w, t.y + t.l]));
  if (!boxes.length){ svgEl.innerHTML = ''; editNote('No rooms to edit.'); return; }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const b of boxes){ minX = Math.min(minX, b[0]); minY = Math.min(minY, b[1]);
    maxX = Math.max(maxX, b[2]); maxY = Math.max(maxY, b[3]); }
  const pad = 3, W = (maxX - minX) + 2 * pad, H = (maxY - minY) + 2 * pad;
  ov = { minX, minY, maxX, maxY, MID: minY + maxY };
  svgEl.setAttribute('viewBox', (minX - pad) + ' ' + (minY - pad) + ' ' + W + ' ' + H);
  const fs = Math.max(1.1, Math.min(2.4, Math.min(W, H) * 0.05));
  // Coarse pointers (finger) get fatter resize handles — a bigger grab area without
  // touching the desktop (fine-pointer) look.
  const hs = COARSE ? Math.max(1.4, Math.min(3.4, Math.min(W, H) * 0.05))
                    : Math.max(0.8, Math.min(2.2, Math.min(W, H) * 0.032));
  let s = '';
  // Dimmed context: rooms on the other floors, as non-interactive outlines. Their
  // id is fit-or-hidden (no title — they're inert, pointer-events:none).
  for (const r of allRooms){
    if (r.level === editLevel) continue;
    const utext = labelFits(r.id, fs * 0.72, r.w)
      ? '<text class="ov-under-t" x="' + (r.x + r.w / 2) + '" y="' + (Y(r.y + r.l / 2) + fs * 0.3) +
        '" text-anchor="middle" font-size="' + (fs * 0.72) + '">' + esc(r.id) + '</text>' : '';
    s += '<rect class="ov-under" x="' + r.x + '" y="' + Y(r.y + r.l) +
      '" width="' + r.w + '" height="' + r.l + '" vector-effect="non-scaling-stroke"/>' + utext;
  }
  for (const r of editRooms){
    const stamped = !!r.instance;   // a `use` member — the whole instance drags as one
    const sel = r.id === selectedRoomId && !stamped;
    const dims = fmtFtIn(r.w) + '×' + fmtFtIn(r.l);
    const idFits = labelFits(r.id, fs, r.w);
    const idText = idFits ? '<text x="' + (r.x + r.w / 2) + '" y="' + (Y(r.y + r.l / 2) - fs * 0.1) +
      '" text-anchor="middle" font-size="' + fs + '" fill="#333" style="pointer-events:none">' +
      esc(r.id) + '</text>' : '';
    const dimText = labelFits(dims, fs * 0.72, r.w) ? '<text x="' + (r.x + r.w / 2) + '" y="' +
      (Y(r.y + r.l / 2) + fs * 1.05) + '" text-anchor="middle" font-size="' + (fs * 0.72) +
      '" fill="#777" style="pointer-events:none">' + dims + '</text>' : '';
    // A hidden id leaves a <title> so hover on the (interactive) room still names it.
    const title = idFits ? '' : '<title>' + esc(r.id) + (stamped ? ' — part ' + esc(r.instance) : '') + '</title>';
    const instAttr = stamped ? ' data-instance="' + esc(r.instance) + '"' : '';
    s += '<rect class="ov-room' + (stamped ? ' ov-stamped' : '') + '" data-room="' + esc(r.id) + '"' +
      instAttr + ' x="' + r.x + '" y="' + Y(r.y + r.l) +
      '" width="' + r.w + '" height="' + r.l + '" fill="' + r.color + '" stroke="' +
      (sel ? '#2F6FB0' : (stamped ? '#8a6d3b' : '#2b2b2b')) + '" stroke-width="' + (sel ? 2.4 : 1) +
      '"' + (stamped ? ' stroke-dasharray="1.4 1"' : '') +
      ' vector-effect="non-scaling-stroke">' + title + '</rect>' + idText + dimText;
    // Multi-selection ring — a distinct dashed violet outline over the room rect.
    if (multiSel.has(r.id))
      s += '<rect class="ov-multi" x="' + r.x + '" y="' + Y(r.y + r.l) +
        '" width="' + r.w + '" height="' + r.l + '"/>';
  }
  // Fixtures on this floor — draggable. A seed is dashed (a drag materialises it
  // into an authored `fixture` line); an authored fixture is solid. The kind label
  // is fit-or-hidden (washer/dryer are the classic overlap) with a <title> fallback.
  for (const f of editFixtures){
    const fk = f.kind.replace(/_/g, ' ');
    const fFits = labelFits(fk, fs * 0.6, f.w);
    const ftext = fFits ? '<text class="ov-fix-t" x="' + (f.x + f.w / 2) + '" y="' +
      (Y(f.y + f.l / 2) + fs * 0.28) + '" text-anchor="middle" font-size="' + (fs * 0.6) +
      '" style="pointer-events:none">' + esc(fk) + '</text>' : '';
    const ftitle = fFits ? '' : '<title>' + esc(fk) + '</title>';
    s += '<rect class="ov-fixture' + (f.seed ? ' seed' : '') + '" data-fixkey="' + esc(f.id) +
      '" x="' + f.x + '" y="' + Y(f.y + f.l) + '" width="' + f.w + '" height="' + f.l +
      '" vector-effect="non-scaling-stroke">' + ftitle + '</rect>' + ftext;
  }
  for (const o of editOpens){
    const seg = openSeg(o, o.offset);
    const col = o.kind === 'window' ? '#2F6FB0' : '#c0392b';
    s += '<line class="ov-open" data-okey="' + esc(o.key) + '" x1="' + seg[0].x + '" y1="' + Y(seg[0].y) +
      '" x2="' + seg[1].x + '" y2="' + Y(seg[1].y) + '" stroke="' + col +
      '" stroke-width="4.5" vector-effect="non-scaling-stroke" stroke-linecap="round"/>';
  }
  // Positioned notes on this floor — a leader (dot → text, NE) drawn like the
  // print callout; the dot is the drag handle (one move_note per drag).
  for (const n of editNotes){
    const sel = dpSel && dpSel.t === 'note' && dpSel.k === n.index;
    const lead = Math.max(1.2, fs * 0.9);
    const tx = n.x + lead, ty = n.y + lead;      // NE in plan feet (+x east, +y north)
    s += '<line class="ov-note-lead" x1="' + n.x + '" y1="' + Y(n.y) + '" x2="' + tx +
      '" y2="' + Y(ty) + '" vector-effect="non-scaling-stroke"/>';
    if (labelFits(n.text, fs * 0.72, 24))
      s += '<text class="ov-note-t' + (sel ? ' sel' : '') + '" x="' + (tx + 0.4) + '" y="' +
        (Y(ty) + fs * 0.25) + '" font-size="' + (fs * 0.72) +
        '" text-anchor="start" style="pointer-events:none">' + esc(n.text) + '</text>';
    s += '<circle class="ov-note' + (sel ? ' sel' : '') + '" data-notekey="' + n.index +
      '" cx="' + n.x + '" cy="' + Y(n.y) + '" r="' + Math.max(0.8, fs * 0.38) +
      '" vector-effect="non-scaling-stroke"><title>' + esc(n.text) + '</title></circle>';
  }
  // Stair footprints touching this floor (run or landing) — drawn over the rooms
  // so the cross-level anchor stays visible even where a room sits on it; inert
  // (pointer-events:none) so the room beneath stays draggable.
  for (const t of allStairs){
    if (t.from !== editLevel && t.to !== editLevel) continue;
    const up = t.from === editLevel;
    const slabel = t.id + (up ? ' ↑' + t.to : ' ↓' + t.from);
    // Fit-or-hide on narrow stairs (the "flight ↑1" overrun). The rect is inert
    // (pointer-events:none), so a hidden label simply drops — no title to surface.
    const stext = labelFits(slabel, fs * 0.72, t.w)
      ? '<text class="ov-stair-t" x="' + (t.x + t.w / 2) + '" y="' + (Y(t.y + t.l / 2) + fs * 0.3) +
        '" text-anchor="middle" font-size="' + (fs * 0.72) + '">' + esc(slabel) + '</text>' : '';
    s += '<rect class="ov-stair" x="' + t.x + '" y="' + Y(t.y + t.l) +
      '" width="' + t.w + '" height="' + t.l + '" vector-effect="non-scaling-stroke"/>' + stext;
  }
  const r = roomById(selectedRoomId);
  if (r){
    const pts = [['sw', r.x, r.y], ['s', r.x + r.w / 2, r.y], ['se', r.x + r.w, r.y],
      ['e', r.x + r.w, r.y + r.l / 2], ['ne', r.x + r.w, r.y + r.l], ['n', r.x + r.w / 2, r.y + r.l],
      ['nw', r.x, r.y + r.l], ['w', r.x, r.y + r.l / 2]];
    // On coarse pointers the visible handle stays small (hs) but an invisible
    // halo sits behind it sized to ~44 SCREEN px (via the live CTM, so it holds
    // at any zoom), giving a comfortable >=40px grab without changing the desktop
    // look. Same data-handle → the drag logic (e.target.closest('[data-handle]'))
    // treats a halo tap as a handle grab.
    let pxPerUnit = 1;
    try { const m = svgEl.getScreenCTM(); if (m && m.a) pxPerUnit = m.a; } catch (_){}
    const hhit = COARSE ? Math.max(hs, 44 / pxPerUnit) : hs;
    for (const p of pts){
      if (COARSE){
        s += '<rect class="ov-handle-hit" data-handle="' + p[0] + '" data-room="' + esc(r.id) +
          '" x="' + (p[1] - hhit / 2) + '" y="' + (Y(p[2]) - hhit / 2) + '" width="' + hhit +
          '" height="' + hhit + '"/>';
      }
      s += '<rect class="ov-handle h-' + p[0] + '" data-handle="' + p[0] + '" data-room="' + esc(r.id) +
        '" x="' + (p[1] - hs / 2) + '" y="' + (Y(p[2]) - hs / 2) + '" width="' + hs + '" height="' + hs +
        '" vector-effect="non-scaling-stroke"/>';
    }
    // On-screen nudge chevrons (coarse pointer only) — a touch stand-in for the
    // arrow-key nudge, one 1 ft step per tap, arranged N/E/S/W around the room.
    if (COARSE){
      const rr = Math.max(1.6, hs * 0.9), gap = rr + 0.8;
      s += nudgeChevron('up',    r.x + r.w / 2, r.y + r.l + gap, rr, '↑');
      s += nudgeChevron('down',  r.x + r.w / 2, r.y - gap,       rr, '↓');
      s += nudgeChevron('left',  r.x - gap,     r.y + r.l / 2,   rr, '←');
      s += nudgeChevron('right', r.x + r.w + gap, r.y + r.l / 2, rr, '→');
    }
  }
  svgEl.innerHTML = s;
  // The floor switcher replaces the old fixed per-level note; only surface a note
  // when the chosen floor has nothing editable on it.
  editNote(editRooms.length ? '' : 'No rooms on this floor to edit.');
}

function openSeg(o, off){
  const dx = o.bx - o.ax, dy = o.by - o.ay, len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len;
  return [{ x: o.ax + ux * off, y: o.ay + uy * off },
          { x: o.ax + ux * (off + o.width), y: o.ay + uy * (off + o.width) }];
}
function projOffset(o, P){
  const dx = o.bx - o.ax, dy = o.by - o.ay, len = Math.hypot(dx, dy) || 1;
  return ((P.x - o.ax) * dx + (P.y - o.ay) * dy) / len;
}
function toPlan(e){
  const pt = svgEl.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const loc = pt.matrixTransform(svgEl.getScreenCTM().inverse());
  return { x: loc.x, y: ov.MID - loc.y };
}
function resizeCalc(d, P){
  let x = d.cur.x, y = d.cur.y, x2 = d.cur.x + d.cur.w, y2 = d.cur.y + d.cur.l;
  const px = snap(P.x), py = snap(P.y);
  if (d.h.indexOf('e') >= 0) x2 = Math.max(x + 3, px);   // 3 ft minimum dimension
  if (d.h.indexOf('w') >= 0) x = Math.min(x2 - 3, px);
  if (d.h.indexOf('n') >= 0) y2 = Math.max(y + 3, py);
  if (d.h.indexOf('s') >= 0) y = Math.min(y2 - 3, py);
  return { x, y, w: x2 - x, l: y2 - y };
}

// -- ghost (the live drag preview) --
function addGhost(d){
  removeGhost();
  const NS = svgEl.namespaceURI;
  if (d.kind === 'open'){
    ghostEl = document.createElementNS(NS, 'line');
    ghostEl.setAttribute('stroke', '#d1873f'); ghostEl.setAttribute('stroke-width', '5.5');
    ghostEl.setAttribute('stroke-linecap', 'round'); ghostEl.setAttribute('stroke-dasharray', '3 2');
    placeGhostLine(d.o, d.offset);
  } else if (d.kind === 'note'){
    ghostEl = document.createElementNS(NS, 'circle');
    ghostEl.setAttribute('fill', 'rgba(122,106,85,.4)'); ghostEl.setAttribute('stroke', '#7A6A55');
    ghostEl.setAttribute('stroke-width', '1.5');
    placeGhostNote(d.cur.x, d.cur.y);
  } else {
    ghostEl = document.createElementNS(NS, 'rect');
    ghostEl.setAttribute('fill', 'rgba(209,135,63,.18)'); ghostEl.setAttribute('stroke', '#d1873f');
    ghostEl.setAttribute('stroke-width', '2'); ghostEl.setAttribute('stroke-dasharray', '4 3');
    placeGhostRect(d.cur.x, d.cur.y, d.cur.w, d.cur.l);
  }
  ghostEl.setAttribute('vector-effect', 'non-scaling-stroke');
  ghostEl.setAttribute('pointer-events', 'none');
  svgEl.appendChild(ghostEl);
}
function removeGhost(){ if (ghostEl && ghostEl.parentNode) ghostEl.parentNode.removeChild(ghostEl); ghostEl = null; }
function placeGhostRect(x, y, w, l){ if (!ghostEl) return;
  ghostEl.setAttribute('x', x); ghostEl.setAttribute('y', Y(y + l));
  ghostEl.setAttribute('width', w); ghostEl.setAttribute('height', l); }
function placeGhostLine(o, off){ if (!ghostEl) return; const seg = openSeg(o, off);
  ghostEl.setAttribute('x1', seg[0].x); ghostEl.setAttribute('y1', Y(seg[0].y));
  ghostEl.setAttribute('x2', seg[1].x); ghostEl.setAttribute('y2', Y(seg[1].y)); }
function ovFontSize(){ const W = (ov.maxX - ov.minX) + 6, H = (ov.maxY - ov.minY) + 6;
  return Math.max(1.1, Math.min(2.4, Math.min(W, H) * 0.05)); }
function placeGhostNote(x, y){ if (!ghostEl) return;
  ghostEl.setAttribute('cx', x); ghostEl.setAttribute('cy', Y(y));
  ghostEl.setAttribute('r', Math.max(0.8, ovFontSize() * 0.38)); }

// -- live dimension readout (a small chip near the cursor) --
function showDim(html, e){
  const r = planBody.getBoundingClientRect();
  dimChip.innerHTML = html;
  dimChip.style.display = 'block';
  let x = e.clientX - r.left + 14, y = e.clientY - r.top + 16;
  // keep it inside the pane
  const cw = dimChip.offsetWidth, ch = dimChip.offsetHeight;
  if (x + cw > r.width - 6) x = e.clientX - r.left - cw - 14;
  if (y + ch > r.height - 6) y = e.clientY - r.top - ch - 16;
  dimChip.style.left = Math.max(4, x) + 'px'; dimChip.style.top = Math.max(4, y) + 'px';
}
function hideDim(){ dimChip.style.display = 'none'; }

// -- neighbour snap guides (align a dragged edge to another room's edge) --
// Cheap axis-aligned comparisons against the rooms array. When a moving edge is
// within the grid step of a neighbour's edge, snap to it and remember a guide.
let guideEls = [];
function clearGuides(){ for (const g of guideEls) if (g.parentNode) g.remove(); guideEls = []; }
function drawGuide(vertical, coord){
  if (!ov) return;
  const NS = svgEl.namespaceURI;
  const ln = document.createElementNS(NS, 'line');
  if (vertical){ ln.setAttribute('x1', coord); ln.setAttribute('x2', coord);
    ln.setAttribute('y1', Y(ov.maxY + 3)); ln.setAttribute('y2', Y(ov.minY - 3)); }
  else { ln.setAttribute('y1', Y(coord)); ln.setAttribute('y2', Y(coord));
    ln.setAttribute('x1', ov.minX - 3); ln.setAttribute('x2', ov.maxX + 3); }
  ln.setAttribute('stroke', '#2F6FB0'); ln.setAttribute('stroke-width', '1');
  ln.setAttribute('stroke-dasharray', '4 3'); ln.setAttribute('vector-effect', 'non-scaling-stroke');
  ln.setAttribute('pointer-events', 'none');
  svgEl.appendChild(ln); guideEls.push(ln);
}
// Snap the given edges of a rect to a neighbour's edges. `edges` limits which
// sides may move: {l,r,b,t} booleans (move drags all four together; resize only
// the handled sides). Returns the adjusted {x,y,w,l} plus the guide coords hit.
function neighborSnap(id, rect, edges){
  const T = 0.5;
  let dx = null, gx = null, dy = null, gy = null;
  const vs = []; if (edges.l) vs.push(['l', rect.x]); if (edges.r) vs.push(['r', rect.x + rect.w]);
  const hs = []; if (edges.b) hs.push(['b', rect.y]); if (edges.t) hs.push(['t', rect.y + rect.l]);
  for (const r of editRooms){
    if (r.id === id) continue;
    const rv = [r.x, r.x + r.w], rh = [r.y, r.y + r.l];
    for (const [, mv] of vs) for (const v of rv){ const d = v - mv;
      if (Math.abs(d) <= T && (dx === null || Math.abs(d) < Math.abs(dx))){ dx = d; gx = v; } }
    for (const [, mh] of hs) for (const h of rh){ const d = h - mh;
      if (Math.abs(d) <= T && (dy === null || Math.abs(d) < Math.abs(dy))){ dy = d; gy = h; } }
  }
  const out = { x: rect.x, y: rect.y, w: rect.w, l: rect.l, gx, gy };
  if (dx !== null){ if (edges.l && !edges.r){ out.x += dx; } else if (edges.r && !edges.l){ out.w += dx; }
    else { out.x += dx; } }
  if (dy !== null){ if (edges.b && !edges.t){ out.y += dy; } else if (edges.t && !edges.b){ out.l += dy; }
    else { out.y += dy; } }
  return out;
}

// -- measure tape (edit mode): drag between two points for a distance readout --
// Ends snap to the 0.5 ft grid; the finished tape stays on screen (with its label)
// until the next measurement, Esc, or an overlay rebuild. Pure client-side — it
// never touches the source, so it doesn't join the undo history.
let measureMode = false, measureEls = [];
function clearMeasure(){ for (const el of measureEls) if (el.parentNode) el.remove(); measureEls = []; }
function setMeasure(on){
  measureMode = !!on && editMode;
  measureBtn.classList.toggle('on', measureMode);
  if (svgEl) svgEl.classList.toggle('measuring', measureMode);
  if (!measureMode) clearMeasure();
  editNote(measureMode ? 'Measure: drag between two points — Esc when done.' : '');
}
function measureLabel(a, b){
  const dx = Math.abs(b.x - a.x), dy = Math.abs(b.y - a.y);
  const d = Math.round(Math.hypot(dx, dy) * 100) / 100;
  return fmtFtIn(d) + (dx && dy ? '  (' + fmtFtIn(dx) + ' × ' + fmtFtIn(dy) + ')' : '');
}
function drawMeasure(a, b){
  clearMeasure();
  const NS = svgEl.namespaceURI;
  const ln = document.createElementNS(NS, 'line');
  ln.setAttribute('class', 'ov-measure');
  ln.setAttribute('x1', a.x); ln.setAttribute('y1', Y(a.y));
  ln.setAttribute('x2', b.x); ln.setAttribute('y2', Y(b.y));
  ln.setAttribute('vector-effect', 'non-scaling-stroke');
  ln.setAttribute('pointer-events', 'none');
  const t = document.createElementNS(NS, 'text');
  t.setAttribute('class', 'ov-measure-t');
  const fs = Math.max(1.1, Math.min(2.4, Math.min(ov.maxX - ov.minX, ov.maxY - ov.minY) * 0.05));
  t.setAttribute('font-size', fs * 0.85);
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('x', (a.x + b.x) / 2);
  t.setAttribute('y', Y((a.y + b.y) / 2) - fs * 0.5);
  t.setAttribute('pointer-events', 'none');
  t.textContent = measureLabel(a, b);
  svgEl.appendChild(ln); svgEl.appendChild(t);
  measureEls = [ln, t];
  // Endpoint dots — small on a mouse, fat grab circles on a coarse (finger) pointer.
  const er = fs * (COARSE ? 0.5 : 0.22);
  for (const end of [a, b]){
    const c = document.createElementNS(NS, 'circle');
    c.setAttribute('class', 'ov-measure-end');
    c.setAttribute('cx', end.x); c.setAttribute('cy', Y(end.y)); c.setAttribute('r', er);
    c.setAttribute('vector-effect', 'non-scaling-stroke');
    c.setAttribute('pointer-events', 'none');
    svgEl.appendChild(c); measureEls.push(c);
  }
}

// -- keyboard nudge: arrows step the selected room(s) 1 ft (Shift: one 3 ft module) --
// Key repeats accumulate into ONE pending move (bounding ghost + note preview); a
// 350 ms pause flushes it as a single batched edit — one undo entry per gesture,
// exactly like releasing a drag. The pending set is a list of rooms, so a single
// room and a whole multi-selection share the same accumulate-then-flush path.
let nudge = null;   // { ids, rooms:[{id,x0,y0,w,l}], dx, dy, timer, label }
function nudgeBBox(n){
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const r of n.rooms){ const x = r.x0 + n.dx, y = r.y0 + n.dy;
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + r.w); maxY = Math.max(maxY, y + r.l); }
  return { x: minX, y: minY, w: maxX - minX, l: maxY - minY };
}
function nudgeMembers(rooms, ddx, ddy, label){
  const ids = rooms.map(r => r.id).join('|');
  if (nudge && nudge.ids !== ids) flushNudge();
  if (!nudge) nudge = { ids, label, dx: 0, dy: 0, timer: null,
    rooms: rooms.map(r => ({ id: r.id, x0: r.x, y0: r.y, w: r.w, l: r.l })) };
  nudge.dx += ddx; nudge.dy += ddy;
  const b = nudgeBBox(nudge);
  if (!ghostEl) addGhost({ kind: 'move', cur: b });
  placeGhostRect(b.x, b.y, b.w, b.l);
  editNote(nudge.rooms.length > 1
    ? nudge.rooms.length + ' rooms → Δ ' + fmtFtIn(nudge.dx) + ', ' + fmtFtIn(nudge.dy)
    : nudge.rooms[0].id + ' → ' + fmtFtIn(nudge.rooms[0].x0 + nudge.dx) + ', ' +
      fmtFtIn(nudge.rooms[0].y0 + nudge.dy));
  clearTimeout(nudge.timer);
  nudge.timer = setTimeout(flushNudge, 350);
}
function flushNudge(){
  if (!nudge) return;
  const n = nudge; nudge = null;
  clearTimeout(n.timer);
  removeGhost();
  if (n.dx || n.dy)
    applyEdits(n.rooms.map(r => ({ kind:'move_room', room:r.id, x:r.x0 + n.dx, y:r.y0 + n.dy })), n.label);
  else editNote('');
}
function cancelNudge(){
  if (!nudge) return;
  clearTimeout(nudge.timer); nudge = null;
  removeGhost(); editNote('');
}

// -- pointer interactions --
// One code path for mouse, touch and pen (Pointer Events). Every active pointer is
// captured and remembered in `editPtrs`; a second pointer promotes to a pinch-zoom
// of the overlay (abandoning any one-finger drag cleanly). On touch, one finger on a
// room/handle drags it while one finger on empty space pans the overlay — the tablet-
// CAD convention. Mouse behaviour is untouched: a single mouse pointer never pans/pinches.
const editPtrs = new Map();          // pointerId -> {x,y}
let editPinchD = 0;                  // last pinch distance (0 = not pinching)
let eLastTapT = 0, eLastTapX = 0, eLastTapY = 0;   // empty-space touch double-tap → fit
function abortDrag(){ if (!drag) return; drag = null; removeGhost(); clearGuides(); hideDim(); }
function onDown(e){
  if (!editMode || !ov) return;
  editPtrs.set(e.pointerId, { x:e.clientX, y:e.clientY });
  // Second finger → pinch. Drop any one-finger drag with no edit written (clean abort).
  if (editPtrs.size >= 2){
    abortDrag(); cancelNudge();
    const p = Array.from(editPtrs.values());
    editPinchD = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
    try { svgEl.setPointerCapture(e.pointerId); } catch(_){}
    e.preventDefault(); return;
  }
  // Tap an on-screen nudge chevron → step the selection 1 ft (touch arrow-keys).
  const nEl = e.target.closest('[data-nudge]');
  if (nEl){
    const members = multiSel.size >= 2 ? selectedRooms()
      : (roomById(selectedRoomId) ? [roomById(selectedRoomId)] : []);
    if (members.length){
      const d = nEl.getAttribute('data-nudge');
      nudgeMembers(members, d === 'left' ? -1 : d === 'right' ? 1 : 0,
        d === 'down' ? -1 : d === 'up' ? 1 : 0,
        members.length > 1 ? 'nudge rooms' : 'nudge room');
    }
    e.preventDefault(); return;
  }
  flushNudge();                      // commit any pending keyboard move first
  const P = toPlan(e);
  if (measureMode){
    const a = { x: snap(P.x), y: snap(P.y) };
    drag = { kind:'measure', a, b:a, P };
    drawMeasure(a, a);
    try { svgEl.setPointerCapture(e.pointerId); } catch(_){}
    e.preventDefault();
    return;
  }
  const handleEl = e.target.closest('[data-handle]');
  const openEl = e.target.closest('[data-okey]');
  const fixEl = e.target.closest('[data-fixkey]');
  const noteEl = e.target.closest('[data-notekey]');
  const roomEl = e.target.closest('[data-room]');
  const roomId = roomEl && roomEl.getAttribute('data-room');
  const instAlias = roomEl && roomEl.getAttribute('data-instance');
  // A stamped member is read-only individually — dragging it drags the WHOLE
  // instance (one move_use edit; the existing bounding-ghost machinery fits).
  if (instAlias && !handleEl){
    const inst = allInstances.find(i => i.alias === instAlias); if (!inst) return;
    const bb = { x: inst.bbox[0], y: inst.bbox[1], w: inst.bbox[2] - inst.bbox[0], l: inst.bbox[3] - inst.bbox[1] };
    if (multiSel.size) clearMultiSel(true);
    drag = { kind:'moveuse', inst, P, ddx:0, ddy:0, moved:false, bbox: bb, cur: bb };
    addGhost(drag);
    try { svgEl.setPointerCapture(e.pointerId); } catch(_){}
    e.preventDefault();
    return;
  }
  // Shift-click a room rect toggles it into the multi-selection (no drag starts).
  if (e.shiftKey && roomEl && !handleEl){ toggleMultiSel(roomId); e.preventDefault(); return; }
  // Dragging a member of a 2+ room selection moves the whole set as one.
  const groupDrag = roomEl && !handleEl && roomId && multiSel.has(roomId) && multiSel.size >= 2;
  // A plain click that isn't a group drag clears the multi-selection first.
  if (!groupDrag && multiSel.size) clearMultiSel(true);
  if (noteEl){
    const n = noteByIndex(parseInt(noteEl.getAttribute('data-notekey'), 10)); if (!n) return;
    drag = { kind:'note', n, P, cur:{ x:n.x, y:n.y }, calc:{ x:n.x, y:n.y }, moved:false };
  } else if (fixEl){
    const f = fixtureByKey(fixEl.getAttribute('data-fixkey')); if (!f) return;
    drag = { kind:'fixture', f, P, cur:{ x:f.x, y:f.y, w:f.w, l:f.l }, calc:{ x:f.x, y:f.y }, moved:false };
  } else if (handleEl){
    const id = handleEl.getAttribute('data-room'), r = roomById(id); if (!r) return;
    drag = { kind:'resize', id, h:handleEl.getAttribute('data-handle'), P,
      cur:{ x:r.x, y:r.y, w:r.w, l:r.l }, calc:{ x:r.x, y:r.y, w:r.w, l:r.l }, moved:false };
  } else if (openEl){
    const o = openByKey(openEl.getAttribute('data-okey')); if (!o) return;
    drag = { kind:'open', o, P, offset:o.offset, moved:false };
  } else if (groupDrag){
    const members = selectedRooms();
    drag = { kind:'movegroup', pressed:roomId, P, ddx:0, ddy:0, moved:false,
      members: members.map(m => ({ id:m.id, x0:m.x, y0:m.y, w:m.w, l:m.l })),
      bbox: roomsBBox(members) };
    drag.cur = drag.bbox;                 // addGhost draws the bounding rect
  } else if (roomEl){
    const id = roomId, r = roomById(id); if (!r) return;
    if (id !== selectedRoomId){ selectedRoomId = id; buildOverlay(); }
    drag = { kind:'move', id, P, cur:{ x:r.x, y:r.y, w:r.w, l:r.l },
      calc:{ x:r.x, y:r.y, w:r.w, l:r.l }, moved:false };
  } else if (e.pointerType && e.pointerType !== 'mouse'){
    // One finger on empty space (touch) → pan the overlay; a double-tap here = Fit.
    const now = Date.now();
    if (now - eLastTapT < 320 && Math.abs(e.clientX - eLastTapX) < 32 &&
        Math.abs(e.clientY - eLastTapY) < 32){ eLastTapT = 0; resetEditTf(); e.preventDefault(); return; }
    eLastTapT = now; eLastTapX = e.clientX; eLastTapY = e.clientY;
    drag = { kind:'panedit', sx: e.clientX - editTf.tx, sy: e.clientY - editTf.ty };
    try { svgEl.setPointerCapture(e.pointerId); } catch(_){}
    e.preventDefault(); return;
  } else { return; }   // empty-space mouse click: no-op, exactly as before
  addGhost(drag);
  try { svgEl.setPointerCapture(e.pointerId); } catch(_){}
  e.preventDefault();
}
function onMove(e){
  if (editPtrs.has(e.pointerId)) editPtrs.set(e.pointerId, { x:e.clientX, y:e.clientY });
  if (editPinchD && editPtrs.size >= 2){          // pinch: zoom the overlay about the midpoint
    const p = Array.from(editPtrs.values());
    const d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
    const mx = (p[0].x + p[1].x) / 2, my = (p[0].y + p[1].y) / 2;
    if (d > 0 && editPinchD > 0) zoomEditAt(d / editPinchD, mx, my);
    editPinchD = d; return;
  }
  if (drag && drag.kind === 'panedit'){
    editTf.tx = e.clientX - drag.sx; editTf.ty = e.clientY - drag.sy; applyEditTf(); return;
  }
  if (!drag) return;
  const P = toPlan(e);
  if (drag.kind === 'measure'){
    drag.b = { x: snap(P.x), y: snap(P.y) };
    drawMeasure(drag.a, drag.b);
    showDim(measureLabel(drag.a, drag.b), e);
    return;
  }
  clearGuides();
  if (drag.kind === 'movegroup'){
    const ddx = snap(P.x - drag.P.x), ddy = snap(P.y - drag.P.y);
    drag.ddx = ddx; drag.ddy = ddy;
    if (ddx || ddy) drag.moved = true;
    placeGhostRect(drag.bbox.x + ddx, drag.bbox.y + ddy, drag.bbox.w, drag.bbox.l);
    showDim(drag.members.length + ' rooms — Δ ' + fmtFtIn(ddx) + ', ' + fmtFtIn(ddy), e);
    return;
  }
  if (drag.kind === 'moveuse'){
    const ddx = snap(P.x - drag.P.x), ddy = snap(P.y - drag.P.y);
    drag.ddx = ddx; drag.ddy = ddy;
    if (ddx || ddy) drag.moved = true;
    placeGhostRect(drag.bbox.x + ddx, drag.bbox.y + ddy, drag.bbox.w, drag.bbox.l);
    showDim('part ' + esc(drag.inst.alias) + ' — Δ ' + fmtFtIn(ddx) + ', ' + fmtFtIn(ddy), e);
    return;
  }
  if (drag.kind === 'move'){
    let nx = snap(drag.cur.x + (P.x - drag.P.x)), ny = snap(drag.cur.y + (P.y - drag.P.y));
    const sn = neighborSnap(drag.id, { x:nx, y:ny, w:drag.cur.w, l:drag.cur.l },
      { l:true, r:true, b:true, t:true });
    nx = sn.x; ny = sn.y;
    if (sn.gx !== null) drawGuide(true, sn.gx);
    if (sn.gy !== null) drawGuide(false, sn.gy);
    drag.calc = { x:nx, y:ny, w:drag.cur.w, l:drag.cur.l };
    if (nx !== drag.cur.x || ny !== drag.cur.y) drag.moved = true;
    placeGhostRect(nx, ny, drag.cur.w, drag.cur.l);
    showDim(esc(drag.id) + ' — ' + fmtFtIn(drag.cur.w) + ' × ' + fmtFtIn(drag.cur.l) +
      ' at ' + fmtFtIn(nx) + ', ' + fmtFtIn(ny), e);
  } else if (drag.kind === 'resize'){
    let c = resizeCalc(drag, P);
    const h = drag.h;
    const sn = neighborSnap(drag.id, c, { l: h.indexOf('w') >= 0, r: h.indexOf('e') >= 0,
      b: h.indexOf('s') >= 0, t: h.indexOf('n') >= 0 });
    if (sn.w >= 3 && sn.l >= 3) c = { x:sn.x, y:sn.y, w:sn.w, l:sn.l };
    if (sn.gx !== null) drawGuide(true, sn.gx);
    if (sn.gy !== null) drawGuide(false, sn.gy);
    drag.calc = c;
    if (c.x !== drag.cur.x || c.y !== drag.cur.y || c.w !== drag.cur.w || c.l !== drag.cur.l) drag.moved = true;
    placeGhostRect(c.x, c.y, c.w, c.l);
    const dw = c.w - drag.cur.w, dl = c.l - drag.cur.l;
    const delta = (dw ? (dw > 0 ? '+' : '') + fmtFtIn(dw) + ' w' : '') +
      (dw && dl ? '  ' : '') + (dl ? (dl > 0 ? '+' : '') + fmtFtIn(dl) + ' l' : '');
    showDim(fmtFtIn(c.w) + ' × ' + fmtFtIn(c.l) +
      (delta ? '<span class="delta">' + delta + '</span>' : ''), e);
  } else if (drag.kind === 'fixture'){
    const nx = snap(drag.cur.x + (P.x - drag.P.x)), ny = snap(drag.cur.y + (P.y - drag.P.y));
    drag.calc = { x:nx, y:ny };
    if (nx !== drag.cur.x || ny !== drag.cur.y) drag.moved = true;
    placeGhostRect(nx, ny, drag.cur.w, drag.cur.l);
    const rm = allRooms.find(r => r.id === drag.f.room);
    const lx = nx - (rm ? rm.x : 0), ly = ny - (rm ? rm.y : 0);
    showDim(esc(drag.f.kind.replace(/_/g, ' ')) + ' — at ' + fmtFtIn(lx) + ', ' + fmtFtIn(ly), e);
  } else if (drag.kind === 'note'){
    const nx = snap(drag.cur.x + (P.x - drag.P.x)), ny = snap(drag.cur.y + (P.y - drag.P.y));
    drag.calc = { x:nx, y:ny };
    if (nx !== drag.cur.x || ny !== drag.cur.y) drag.moved = true;
    placeGhostNote(nx, ny);
    showDim('note — at ' + fmtFtIn(nx) + ', ' + fmtFtIn(ny), e);
  } else {
    const o = drag.o;
    const off = Math.max(o.min, Math.min(o.max, snap(projOffset(o, P) - o.width / 2)));
    drag.offset = off; if (Math.abs(off - o.offset) > 1e-9) drag.moved = true;
    placeGhostLine(o, off);
    showDim('offset ' + fmtFtIn(off), e);
  }
}
function onUp(e){
  editPtrs.delete(e.pointerId);
  try { svgEl.releasePointerCapture(e.pointerId); } catch(_){}
  if (editPtrs.size >= 1){ editPinchD = 0; return; }   // a finger of a pinch lifted — wait for the rest
  editPinchD = 0;
  if (drag && drag.kind === 'panedit'){ drag = null; return; }
  if (!drag) return;
  const d = drag; drag = null; removeGhost(); clearGuides(); hideDim();
  if (d.kind === 'measure'){
    // A click without a drag leaves nothing; a real span stays until the next one.
    if (d.a.x === d.b.x && d.a.y === d.b.y) clearMeasure();
    return;
  }
  if (d.kind === 'movegroup'){
    if (!d.moved){    // a plain click on a member collapses to single-selecting it
      clearMultiSel(false); dpSelect('room', d.pressed); return; }
    applyEdits(d.members.map(m => ({ kind:'move_room', room:m.id,
      x: snap(m.x0 + d.ddx), y: snap(m.y0 + d.ddy) })), 'move rooms');
    return;
  }
  if (d.kind === 'moveuse'){
    if (!d.moved){ dpSelect('inst', d.inst.alias);
      if (d.inst.line) jumpToLine(d.inst.line); return; }
    applyEdits([{ kind:'move_use', alias:d.inst.alias,
      x: snap(d.inst.x + d.ddx), y: snap(d.inst.y + d.ddy) }], 'move part');
    return;
  }
  if (d.kind === 'move'){
    if (!d.moved){ dpSelect('room', d.id);
      const ln = roomLine(d.id); if (ln) jumpToLine(ln); return; }
    applyEdits([{ kind:'move_room', room:d.id, x:d.calc.x, y:d.calc.y }]);
  } else if (d.kind === 'resize'){
    if (!d.moved) return;
    const edits = [];
    if (d.calc.w !== d.cur.w || d.calc.l !== d.cur.l)
      edits.push({ kind:'resize_room', room:d.id, w:d.calc.w, l:d.calc.l });
    if (d.calc.x !== d.cur.x || d.calc.y !== d.cur.y)
      edits.push({ kind:'move_room', room:d.id, x:d.calc.x, y:d.calc.y });
    applyEdits(edits);
  } else if (d.kind === 'fixture'){
    if (!d.moved){ dpSelect('fx', d.f.id);
      if (d.f.line) jumpToLine(d.f.line); return; }
    const rm = allRooms.find(r => r.id === d.f.room);
    const lx = d.calc.x - (rm ? rm.x : 0), ly = d.calc.y - (rm ? rm.y : 0);
    if (d.f.seed)   // materialise the seed into an authored `fixture` line
      applyEdits([{ kind:'add_fixture', room:d.f.room, fkind:d.f.kind, wall:d.f.wall, x:lx, y:ly }]);
    else            // rewrite the explicit fixture's `at x,y`
      applyEdits([{ kind:'move_fixture', key:d.f.id, x:lx, y:ly }]);
  } else if (d.kind === 'note'){
    if (!d.moved){ dpSelect('note', d.n.index);
      if (d.n.line) jumpToLine(d.n.line); return; }
    applyEdits([{ kind:'move_note', index:d.n.index, x:d.calc.x, y:d.calc.y }], 'move note');
  } else {
    if (!d.moved) return;
    applyEdits([{ kind:'move_opening', opening:d.o.kind, key:d.o.key, offset:d.offset }]);
  }
}
function cancelDrag(){ if (!drag) return; drag = null; removeGhost(); clearGuides(); hideDim(); buildOverlay(); }
// pointercancel (iOS fires it when the browser takes over the gesture) — drop the
// pointer, end any pinch, and abort the drag with no edit written or state stuck.
function onCancel(e){
  editPtrs.delete(e.pointerId);
  if (editPtrs.size < 2) editPinchD = 0;
  if (drag && drag.kind === 'panedit'){ drag = null; return; }
  cancelDrag();
}

// -- apply a sequence of edits atomically (from the client's view) --
async function applyEdits(edits, label){
  if (!edits.length){ buildOverlay(); return false; }
  autosaveOff = false;   // a layout edit is a deliberate action — resume autosave
  let src = editor.value, p = null;
  try {
    for (const ed of edits){
      const resp = await fetch('/api/edit', { method:'POST',
        headers:{ 'Content-Type':'application/json' }, body: JSON.stringify({ source:src, edit:ed }) });
      p = await resp.json();
      if (p.error) throw new Error(p.error.message || 'edit rejected');
      src = p.source;
    }
  } catch (err){
    editNote(String(err.message || err), true);
    dpNote(String(err.message || err), true);
    buildOverlay();                        // restore positions from the unchanged data
    return false;
  }
  applyEdit(src, null, null, label || 'layout edit');   // checkpoint, then swap in the rewrite
  renderGutter();
  applyResult(p);
  if (p.line) flashLine(p.line);
  editNote('');
  dpNote(p.summary || '');
  // add_room with a full envelope: the room was dropped at the origin (overlapping)
  // rather than refused — offer a one-tap envelope grow so it has somewhere to go.
  if (p.placed === 'fallback')
    offerEnvelopeGrow(edits.find(e => e && e.kind === 'add_room'));
  return true;
}
// Envelope is fully tiled: a just-added room fell back to the origin. Offer to
// enlarge the envelope (reusing the set_plan edit) so a free strip opens up.
function offerEnvelopeGrow(ed){
  const p = lastGood; if (!p) return;
  const env = (p.settings && p.settings.envelope) || [0, 0];
  const grow = Math.max(8, (ed && isFinite(ed.l)) ? Math.ceil(ed.l) : 10);
  const newW = env[0], newL = Math.ceil(env[1]) + grow;
  dpNote('No free space — the envelope is full; the room landed at the origin.', true);
  showNotice('No free space — the envelope is full. Enlarge it so the new room fits?',
    [{ label: 'Grow envelope to ' + trimNum(newW) + '×' + trimNum(newL) + '′',
       fn: () => growEnvelope(newW, newL) },
     { label: 'Leave as is', ghost: true }]);
}
function growEnvelope(w, l){
  applyEdits([{ kind:'set_plan', envelope:[w, l] }], 'grow envelope').then(ok => {
    if (ok) dpNote('Enlarged the envelope — drag the new room into the new space.');
  });
}
function flashLine(ln){
  const el = gutter.children[ln - 1]; if (!el) return;
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}

// --- design panel (outline + inspector + add/delete) --------------------------
// The no-code face of the DSL: an outline of the plan's structure and a property
// inspector whose every control emits ONE surgical edit through /api/edit — the
// code pane updates live, each change joins the unified undo timeline, and the
// text stays the single source of truth. Renders from `lastGood` (same payload
// the drag overlay reads) and re-renders on every refreshEditData.
const dpEl = document.getElementById('design-panel');
const panelBtn = document.getElementById('panel-btn');
const LS_PANEL = 'barndsl.playground.panelOpen';
let dpOpen = false;
let dpSel = null;            // {t:'room'|'op'|'fx', k:id-or-key} — the inspected object
let dpForm = null;           // 'room' | {op:'door'|'window'|'entry'} — the open add-form
let dpNoteMsg = '', dpNoteErr = false;

function dpNote(msg, err){
  dpNoteMsg = msg || ''; dpNoteErr = !!err;
  const el = document.getElementById('dp-note');
  if (el){ el.textContent = dpNoteMsg; el.classList.toggle('err', dpNoteErr); }
}
// Selection is shared with the drag overlay: picking a room here rings it there.
function dpSelect(t, k){
  // Notes are keyed by numeric index; a panel row passes it as a string, the
  // overlay as a number — normalise so every comparison downstream is number↔number.
  if (t === 'note' && k != null) k = parseInt(k, 10);
  dpSel = k == null ? null : { t: t, k: k };
  dpForm = null;
  if (t === 'room'){ selectedRoomId = k; if (editMode) buildOverlay(); }
  else if (t === 'note' && editMode) buildOverlay();
  renderPanel();
}
function togglePanel(open){
  dpOpen = open == null ? dpEl.hidden : open;
  dpEl.hidden = !dpOpen;
  panelBtn.classList.toggle('on', dpOpen);
  try { localStorage.setItem(LS_PANEL, dpOpen ? '1' : ''); } catch (e){}
  renderPanel(); planZoom.refit();          // the plan pane just changed width
}
panelBtn.addEventListener('click', () => togglePanel());

function optList(items, cur){
  let h = '';
  for (const v of items)
    h += '<option value="' + esc(v) + '"' + (v === cur ? ' selected' : '') + '>' + esc(v) + '</option>';
  return h;
}
function fnum(v){ return v == null ? '' : trimNum(v); }
function opKindWord(o){
  return o.kind === 'interior' ? (o.door ? 'door' : 'open') : (o.kind === 'window' ? 'window' : 'entry');
}
function opLabel(o){
  if (o.kind === 'interior') return (o.door ? 'door ' : 'open ') + o.a + ' ↔ ' + o.b;
  return opKindWord(o) + ' ' + o.room + ' ' + o.side;
}

function renderPanel(){
  if (!dpOpen) return;
  const p = lastGood;
  if (!p || !p.rooms){
    dpEl.innerHTML = '<div class="dp-note">Compile a plan (fix any errors) to use the design panel.</div>';
    return;
  }
  const s = p.settings || {};
  let h = '<h5>Plan</h5><div class="dp-grid">' +
    '<label>name</label><input class="wide" data-act="plan.name" value="' + esc(s.name || '') + '">' +
    '<label>envelope</label>' +
    '<input type="number" min="1" step="1" data-act="plan.envw" title="Envelope width (ft, east–west)" value="' + fnum(s.envelope && s.envelope[0]) + '">' +
    '<input type="number" min="1" step="1" data-act="plan.envl" title="Envelope length (ft, south–north)" value="' + fnum(s.envelope && s.envelope[1]) + '">' +
    '<label>ceiling</label><input type="number" min="1" step="0.5" data-act="plan.ceil" value="' + fnum(s.ceiling) + '"><span></span>' +
    '</div>';
  // Stamped rooms (from `use` instances) are read-only members — kept out of the
  // Rooms/Openings lists and shown under Parts (greyed) instead.
  const stampedSet = new Set();
  (p.instances || []).forEach(inst => inst.rooms.forEach(rr => stampedSet.add(rr)));
  const opStamped = o => o.kind === 'interior'
    ? (stampedSet.has(o.a) || stampedSet.has(o.b)) : stampedSet.has(o.room);
  // The room/opening list is variable-length; cap it in its own scroll region so
  // the primary actions below (＋ Room and the Add-room form) stay on-screen on a
  // short (tablet, 768px) viewport instead of being pushed below the fold.
  h += '<div class="dp-scroll">';
  h += '<h5>Rooms</h5>';
  const levels = p.levels || [0];
  for (const lv of levels){
    if (levels.length > 1) h += '<div class="dp-level">Level ' + lv + '</div>';
    for (const r of p.rooms.filter(r => r.level === lv && !r.instance)){
      const sel = dpSel && dpSel.t === 'room' && dpSel.k === r.id;
      h += '<div class="dp-row' + (sel ? ' sel' : '') + '" data-sel="room:' + esc(r.id) + '">' +
        '<span class="swatch" style="background:' + esc(r.color) + '"></span>' +
        '<span class="dp-id">' + esc(r.id) + '</span><span class="dp-kind">' + esc(r.type) + '</span>' +
        '<span class="dp-dim">' + fmtFtIn(r.w) + '×' + fmtFtIn(r.l) + '</span></div>';
      for (const f of (p.fixtures || []).filter(f => f.room === r.id)){
        const fsel = dpSel && dpSel.t === 'fx' && dpSel.k === f.id;
        h += '<div class="dp-sub"><div class="dp-row' + (fsel ? ' sel' : '') + (f.seed ? ' dp-seed' : '') +
          '" data-sel="fx:' + esc(f.id) + '"><span class="dp-id">' + esc(f.kind.replace(/_/g, ' ')) +
          '</span>' + (f.seed ? '<span class="dp-kind">auto</span>' : '') + '</div></div>';
      }
    }
    const ops = (p.openings || []).filter(o => o.level === lv && !opStamped(o));
    if (ops.length){
      h += '<div class="dp-level">Openings' + (levels.length > 1 ? ' — level ' + lv : '') + '</div>';
      for (const o of ops){
        const osel = dpSel && dpSel.t === 'op' && dpSel.k === o.key;
        h += '<div class="dp-row' + (osel ? ' sel' : '') + '" data-sel="op:' + esc(o.key) + '">' +
          '<span class="dp-id">' + esc(opLabel(o)) + '</span>' +
          '<span class="dp-dim">' + fmtFtIn(o.width) + '</span></div>';
      }
    }
    const nts = (p.notes || []).filter(n => (n.level || 0) === lv);
    if (nts.length){
      h += '<div class="dp-level">Notes' + (levels.length > 1 ? ' — level ' + lv : '') + '</div>';
      for (const n of nts){
        const nsel = dpSel && dpSel.t === 'note' && dpSel.k === n.index;
        h += '<div class="dp-row' + (nsel ? ' sel' : '') + '" data-sel="note:' + n.index + '">' +
          '<span class="dp-id">✎ ' + esc(n.text) + '</span>' +
          '<span class="dp-dim">' + fmtFtIn(n.x) + ',' + fmtFtIn(n.y) + '</span></div>';
      }
    }
  }
  // Parts — each `use` instance as a collapsible group: a header row (▣ alias —
  // filename) and its greyed member rooms (read-only stamps). Selecting the header
  // opens the instance inspector (move/level/Delete/Duplicate/Inline).
  if ((p.instances || []).length){
    h += '<h5>Parts</h5>';
    for (const inst of p.instances){
      const file = (inst.relpath || '').split('/').pop();
      const isel = dpSel && dpSel.t === 'inst' && dpSel.k === inst.alias;
      let xform = '';
      if (inst.rotate) xform += ' ↻' + inst.rotate + '°';
      if (inst.mirror) xform += ' ⇄' + inst.mirror;
      h += '<div class="dp-row dp-inst' + (isel ? ' sel' : '') + '" data-sel="inst:' + esc(inst.alias) + '">' +
        '<span class="dp-id">▣ ' + esc(inst.alias) + '</span>' +
        '<span class="dp-kind">' + esc(file) + '</span>' +
        '<span class="dp-dim">' + fmtFtIn(inst.x) + ',' + fmtFtIn(inst.y) +
        (inst.level ? ' L' + inst.level : '') + esc(xform) + '</span></div>';
      for (const rid of (inst.rooms || [])){
        const rm = p.rooms.find(x => x.id === rid);
        h += '<div class="dp-sub"><div class="dp-row dp-stamped" title="Stamped from ' +
          esc(inst.relpath) + ' — edit the part, or Inline the instance">' +
          '<span class="dp-id">' + esc(rid) + '</span>' +
          (rm ? '<span class="dp-kind">' + esc(rm.type) + '</span>' : '') + '</div></div>';
      }
    }
  }
  h += '</div>';  // close .dp-scroll (the capped room/opening list)
  h += '<div class="dp-btns dp-actions"><button data-btn="addroom">＋ Room</button>' +
    '<button data-btn="addnote" title="Add a positioned note — a leader callout on the plan">＋ Note</button>' +
    '<button data-btn="parts" title="Insert a reusable part (a plan-less .barn file beside this plan)">▣ Parts</button></div>';
  if (dpForm === 'room') h += addRoomForm(p);
  if (dpForm === 'parts') h += partsBrowser(p);
  h += renderInspector(p);
  h += '<div class="dp-note' + (dpNoteErr ? ' err' : '') + '" id="dp-note">' + esc(dpNoteMsg) + '</div>';
  dpEl.innerHTML = h;
}

function renderInspector(p){
  if (!dpSel)
    return '<h5>Properties</h5><div class="dp-note">Select a room, opening or fixture above — or click one on the plan in edit mode.</div>';
  if (dpSel.t === 'inst'){
    const inst = (p.instances || []).find(x => x.alias === dpSel.k);
    if (!inst){ dpSel = null; return ''; }
    const levels = p.levels || [0];
    let lvOpts = '';
    for (const lv of levels) lvOpts += '<option value="' + lv + '"' +
      (lv === inst.level ? ' selected' : '') + '>' + lv + '</option>';
    const mir = inst.mirror || '';
    let mirOpts = '';
    for (const m of [['', '—'], ['x', 'x (N↔S)'], ['y', 'y (E↔W)']])
      mirOpts += '<option value="' + m[0] + '"' + (m[0] === mir ? ' selected' : '') + '>' + m[1] + '</option>';
    const rot = inst.rotate || 0;
    let rotOpts = '';
    for (const a of [0, 90, 180, 270])
      rotOpts += '<option value="' + a + '"' + (a === rot ? ' selected' : '') + '>' + a + '°</option>';
    return '<h5>Instance — ' + esc(inst.alias) + '</h5><div class="dp-grid">' +
      '<label>part</label><span class="dp-kind wide">' + esc(inst.relpath) + '</span>' +
      '<label>at</label><input type="text" inputmode="text" data-act="inst.x" title="South-west corner x (ft — accepts 12′6″)" value="' + trimNum(inst.x) + '">' +
      '<input type="text" inputmode="text" data-act="inst.y" title="South-west corner y (ft — accepts 12′6″)" value="' + trimNum(inst.y) + '">' +
      '<label>level</label><select data-act="inst.level">' + lvOpts + '</select><span></span>' +
      '<label>mirror</label><select data-act="inst.mirror" title="Reflect the part — y flips east↔west, x flips north↔south">' + mirOpts + '</select>' +
      '<select data-act="inst.rotate" title="Rotate the part counter-clockwise (90° steps)">' + rotOpts + '</select>' +
      '</div><div class="dp-btns">' +
      '<button data-btn="inlineinst" title="Replace the use with its stamped statements — makes the part local and editable">Inline</button>' +
      '<button data-btn="dupinst" title="Add another instance of this part at a small offset">Duplicate</button>' +
      '<button class="danger" data-btn="delinst" title="Remove the use line (and everything it stamped) — one undo brings it back">Delete instance</button></div>';
  }
  if (dpSel.t === 'room'){
    const r = p.rooms.find(x => x.id === dpSel.k);
    if (!r){ dpSel = null; return ''; }
    let h = '<h5>Room — ' + esc(r.id) + '</h5><div class="dp-grid">' +
      '<label>name</label><input class="wide" data-act="room.rename" value="' + esc(r.id) + '">' +
      '<label>type</label><select class="wide" data-act="room.type">' + optList(HIGHLIGHT.types || [], r.type) + '</select>' +
      '<label>size</label><input type="text" inputmode="text" data-act="room.w" title="Width (ft — accepts 12′6″, 12-6, 12.5)" value="' + trimNum(r.w) + '">' +
      '<input type="text" inputmode="text" data-act="room.l" title="Length (ft — accepts 12′6″, 12-6, 12.5)" value="' + trimNum(r.l) + '">' +
      '<label>corner</label><input type="text" inputmode="text" data-act="room.x" title="South-west corner x (ft east of origin — accepts 12′6″)" value="' + trimNum(r.x) + '">' +
      '<input type="text" inputmode="text" data-act="room.y" title="South-west corner y (ft north of origin — accepts 12′6″)" value="' + trimNum(r.y) + '">' +
      '</div><div class="dp-btns">' +
      '<button data-btn="adddoor">＋ Door</button><button data-btn="addwindow">＋ Window</button>' +
      '<button data-btn="addentry">＋ Entry</button>' +
      '<button data-btn="addfix" title="Furnish — place a fixture or furniture piece in this room">＋ Fixture</button>' +
      '<button data-btn="addelec" title="Add an outlet, switch or ceiling light to this room">＋ Electrical</button>' +
      '<button data-btn="duproom" title="Add a same-size twin beside this room (bed → bed2)">Duplicate</button>' +
      '<button class="danger" data-btn="delroom" title="Removes the room and everything on it — one undo brings it all back">Delete room</button></div>';
    if (dpForm && dpForm.op) h += addOpeningForm(p, r);
    if (dpForm === 'fx') h += addFixtureForm();
    if (dpForm === 'elec') h += addElectricalForm(r);
    return h;
  }
  if (dpSel.t === 'op'){
    const o = (p.openings || []).find(x => x.key === dpSel.k);
    if (!o){ dpSel = null; return ''; }
    let h = '<h5>' + esc(opLabel(o)) + '</h5><div class="dp-grid">' +
      '<label>width</label><input type="text" inputmode="text" data-act="op.width" title="Width (ft — accepts 2′8″, 2-8, 2.67)" value="' + trimNum(o.width) + '"><span></span>' +
      '<label>offset</label><input type="text" inputmode="text" data-act="op.offset" title="Offset (ft — accepts 2′8″, 2-8, 2.67)" value="' + fnum(o.offset) + '"><span></span>';
    if (o.kind === 'interior' && o.door){
      h += '<label>swings into</label><select data-act="op.into">' +
        '<option value=""' + (o.into ? '' : ' selected') + '>—</option>' + optList([o.a, o.b], o.into) + '</select>' +
        '<select data-act="op.hinge"><option value=""' + (o.hinge ? '' : ' selected') + '>hinge…</option>' +
        optList(['near', 'far'], o.hinge) + '</select>';
    }
    if (o.kind === 'window')
      h += '<label>sill</label><input type="number" min="0" step="0.25" data-act="op.sill" value="' + fnum(o.sill) + '"><span></span>';
    h += '</div><div class="dp-btns"><button class="danger" data-btn="delop">Delete ' + esc(opKindWord(o)) + '</button></div>';
    return h;
  }
  if (dpSel.t === 'note'){
    const n = (p.notes || []).find(x => x.index === dpSel.k);
    if (!n){ dpSel = null; return ''; }
    return '<h5>Note</h5><div class="dp-grid">' +
      '<label>text</label><input class="wide" data-act="note.text" value="' + esc(n.text) + '">' +
      '<label>x</label><input type="text" inputmode="text" data-act="note.x" title="Anchor x (ft east of origin — accepts 12′6″)" value="' + trimNum(n.x) + '"><span></span>' +
      '<label>y</label><input type="text" inputmode="text" data-act="note.y" title="Anchor y (ft north of origin — accepts 12′6″)" value="' + trimNum(n.y) + '"><span></span>' +
      '</div><div class="dp-btns"><button class="danger" data-btn="delnote">Delete note</button></div>';
  }
  const f = (p.fixtures || []).find(x => x.id === dpSel.k);
  if (!f){ dpSel = null; return ''; }
  return '<h5>Fixture — ' + esc(f.kind.replace(/_/g, ' ')) + (f.seed ? ' (auto)' : '') + '</h5><div class="dp-grid">' +
    '<label>rotate</label><input type="number" step="90" data-act="fx.rotate" value="' + fnum(f.rotate || 0) + '">' +
    '<select data-act="fx.wall" title="Snap to a wall"><option value=""' + (f.wall ? '' : ' selected') + '>free</option>' +
    optList(['N', 'S', 'E', 'W'], f.wall) + '</select>' +
    '<label>width</label><input type="number" min="0.5" step="0.5" data-act="fx.width" value="' + trimNum(f.w) + '"><span></span>' +
    '</div><div class="dp-btns">' + (f.seed
      ? '<span class="dp-note">Auto-placed — the first edit writes a `fixture` line you own.</span>'
      : '<button class="danger" data-btn="delfx">Delete fixture</button>') + '</div>';
}

const DP_ANCHORS = ['east-of', 'west-of', 'north-of', 'south-of',
  'right-of', 'left-of', 'above-of', 'below-of'];
function nextRoomId(p, base){
  const ids = new Set(p.rooms.map(r => r.id));
  let n = 1, id = base;
  while (ids.has(id)){ n++; id = base + n; }
  return id;
}
// A short, typed id stem for a room type, so "+ Room" defaults to bed2/bath2/…
// rather than the literal "room". Falls back to the type name itself.
const ROOM_STEM = { bedroom:'bed', bathroom:'bath', half_bath:'bath', hallway:'hall',
  mudroom:'mud', laundry:'laundry', utility:'util', kitchen:'kitchen', living:'living',
  dining:'dining', office:'office', closet:'closet', pantry:'pantry', garage:'garage',
  shop:'shop', porch:'porch', loft:'loft' };
function typedRoomId(p, type){ return nextRoomId(p, ROOM_STEM[type] || type || 'room'); }
// The Parts browser: the plan-less .barn parts beside the served file (scanned
// server-side into p.parts_available). Each row Inserts a `use` at plan centre
// with a fresh alias. Empty (or a browser-opened buffer with no folder) teaches
// where parts come from.
function partsBrowser(p){
  const parts = p.parts_available || [];
  if (!parts.length)
    return '<div class="dp-form"><div class="dp-note">Put plan-less .barn part ' +
      'files beside the served plan (or in parts/) and they appear here.</div>' +
      '<div class="dp-btns"><button data-btn="formcancel">Close</button></div></div>';
  let rows = '';
  for (const part of parts){
    const rn = (typeof part.rooms === 'number') ? part.rooms : null;
    rows += '<div class="dp-row"><span class="dp-id">▣ ' + esc(part.name || part.relpath) + '</span>' +
      '<span class="dp-kind">' + esc(part.relpath) + (rn != null ? ' · ' + rn + 'r' : '') + '</span>' +
      '<button data-part="' + esc(part.relpath) + '">Insert</button></div>';
  }
  return '<div class="dp-form">' + rows +
    '<div class="dp-btns"><button data-btn="formcancel">Close</button></div>' +
    '<div class="dp-note">Insert drops a `use` at the plan centre — move, mirror or rotate it in the inspector.</div></div>';
}
// Mint a fresh instance alias from a base (reused by Duplicate + Parts Insert):
// bath → bath2, bath3, … skipping any alias already taken.
function mintAlias(p, base){
  const aliases = new Set((p.instances || []).map(i => i.alias));
  const stem = (base || 'p').replace(/\d+$/, '').replace(/[^A-Za-z0-9_]/g, '') || 'p';
  let n = aliases.has(stem) || /\d$/.test(base || '') ? 2 : 0, alias = n ? stem + n : stem;
  while (aliases.has(alias)){ n = (n || 1) + 1; alias = stem + n; }
  return alias;
}
function insertPart(relpath){
  const p = lastGood; if (!p) return;
  let cx = 0, cy = 0;
  if (p.rooms && p.rooms.length){
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const r of p.rooms){ minX = Math.min(minX, r.x); minY = Math.min(minY, r.y);
      maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.l); }
    cx = snap((minX + maxX) / 2); cy = snap((minY + maxY) / 2);
  }
  const stem = (relpath.split('/').pop() || 'part').replace(/\.barn$/, '');
  const alias = mintAlias(p, stem.split(/[^A-Za-z0-9_]/)[0] || 'p');
  dpForm = null;
  applyEdits([{ kind:'add_use', relpath:relpath, alias:alias, x:cx, y:cy }], 'insert part')
    .then(ok => { if (ok) dpSelect('inst', alias); });
}
function addRoomForm(p){
  const rooms = p.rooms.map(r => r.id);
  const of0 = (dpSel && dpSel.t === 'room') ? dpSel.k : rooms[0];
  return '<div class="dp-form"><div class="dp-grid">' +
    '<label>name</label><input class="wide" id="nr-id" value="' + esc(typedRoomId(p, 'bedroom')) + '">' +
    '<label>type</label><select class="wide" id="nr-type">' + optList(HIGHLIGHT.types || [], 'bedroom') + '</select>' +
    '<label>size</label><input type="number" id="nr-w" min="1" step="0.5" value="12">' +
    '<input type="number" id="nr-l" min="1" step="0.5" value="12">' +
    '<label>place</label><select id="nr-anchor"><option value="auto" selected>auto — find space</option>' +
    optList(DP_ANCHORS, '') + '</select>' +
    '<select id="nr-of">' + optList(rooms, of0) + '</select>' +
    '</div><div class="dp-btns"><button data-btn="roomsubmit">Add room</button>' +
    '<button data-btn="formcancel">Cancel</button></div>' +
    '<div class="dp-note">Auto drops it in the first free spot beside a neighbour — ' +
    'drag it on the plan afterwards to fine-tune.</div></div>';
}
// The furnish palette: every fixture kind the compiler knows (HIGHLIGHT.fixtures,
// the same list the autocomplete offers), shown with spaces instead of underscores.
// The piece lands mid-room and is dragged into place like any other fixture.
function addFixtureForm(){
  let opts = '';
  for (const k of (HIGHLIGHT.fixtures || []))
    opts += '<option value="' + esc(k) + '"' + (k === 'sofa' ? ' selected' : '') + '>' +
      esc(k.replace(/_/g, ' ')) + '</option>';
  return '<div class="dp-form"><div class="dp-grid">' +
    '<label>piece</label><select class="wide" id="nf-kind">' + opts + '</select>' +
    '<label>wall</label><select class="wide" id="nf-wall" title="Back it to a wall, or leave it free-standing">' +
    '<option value="" selected>free-standing</option>' + optList(['N', 'S', 'E', 'W'], '') + '</select>' +
    '<label>along run</label><select class="wide" id="nf-along" title="Counter only: run a countertop the length of a wall (an L/U is several runs)">' +
    '<option value="" selected>— not a run —</option>' + optList(['N', 'S', 'E', 'W'], '') + '</select>' +
    '<label>from</label><input id="nf-from" placeholder="wall start" title="Optional: run start, ft along the wall (ft-in ok, e.g. 2-6)"><span></span>' +
    '<label>to</label><input id="nf-to" placeholder="wall end" title="Optional: run end, ft along the wall (ft-in ok)"><span></span>' +
    '</div><div class="dp-btns"><button data-btn="fxsubmit">Add fixture</button>' +
    '<button data-btn="formcancel">Cancel</button></div>' +
    '<div class="dp-note">Lands mid-room — drag it into place. A <b>counter</b> with an ' +
    '<b>along run</b> set spans that wall (full wall, or from/to); dragging it later ' +
    'slides it along the wall.</div></div>';
}
function addOpeningForm(p, r){
  const kind = dpForm.op;
  let inner;
  if (kind === 'door'){
    const others = p.rooms.filter(x => x.id !== r.id && x.level === r.level).map(x => x.id);
    if (!others.length) return '<div class="dp-form"><div class="dp-note">No other room on this level to connect to.</div></div>';
    inner = '<label>to</label><select class="wide" id="no-b">' + optList(others, others[0]) + '</select>' +
      '<label>style</label><select class="wide" id="no-doortype">' +
      '<option value="door">door (swinging)</option><option value="open">open (cased, no door)</option></select>';
  } else {
    inner = '<label>wall</label><select class="wide" id="no-side">' +
      optList(['north', 'south', 'east', 'west'], 'south') + '</select>';
  }
  return '<div class="dp-form"><div class="dp-grid">' + inner +
    '<label>width</label><input type="number" id="no-width" min="1" step="0.5" value="' + (kind === 'window' ? 4 : 3) + '"><span></span>' +
    '</div><div class="dp-btns"><button data-btn="opsubmit">Add ' + esc(kind) + '</button>' +
    '<button data-btn="formcancel">Cancel</button></div>' +
    '<div class="dp-note">The compiler checks placement — watch the diagnostics for a teaching hint.</div></div>';
}

// --- envelope resize assist -------------------------------------------------
// Shrinking the envelope can strand rooms OUT_OF_BOUNDS (everything turns red).
// When an envelope edit newly introduces that, OFFER a one-click "Fit rooms to
// new envelope" (a fit_envelope edit — proportional scale + clamp, one undo step);
// never rescale silently.
function oobCount(diags){
  let n = 0; for (const d of (diags || [])) if (d.code === 'OUT_OF_BOUNDS') n++;
  return n;
}
function fitEnvelope(){
  applyEdits([{ kind:'fit_envelope' }], 'fit rooms to envelope').then(ok => {
    if (ok) dpNote('Fit the rooms into the envelope.');
  });
}
function offerFitIfStranded(before){
  const after = oobCount(diagnostics);
  if (after > 0 && before === 0){
    dpNote(after + ' room(s) now fall outside the envelope.', true);
    showNotice('That envelope leaves ' + after + ' room(s) out of bounds. Fit them to the new size?',
      [{ label:'Fit rooms to new envelope', fn:fitEnvelope },
       { label:'Leave as is', ghost:true }]);
  }
}

function dpChange(act, el){
  const p = lastGood; if (!p) return;
  // Dimension fields accept feet-and-inches (12'6", 12-6) as well as decimals;
  // parseFtIn returns null on junk, which we map to NaN so the isFinite() guards
  // below reject it and the field keeps its prior value.
  const v = el.value; let num = parseFtIn(v); if (num == null) num = NaN;
  const s = p.settings || {};
  if (act === 'plan.name'){
    if (v.trim() && v.trim() !== s.name) applyEdits([{ kind:'set_plan', name:v.trim() }], 'plan settings');
    return;
  }
  if (act === 'plan.envw' || act === 'plan.envl'){
    const w = act === 'plan.envw' ? num : (s.envelope || [])[0];
    const l = act === 'plan.envl' ? num : (s.envelope || [])[1];
    if (isFinite(w) && isFinite(l) && w > 0 && l > 0){
      const before = oobCount(diagnostics);
      applyEdits([{ kind:'set_plan', envelope:[w, l] }], 'plan settings')
        .then(ok => { if (ok) offerFitIfStranded(before); });
    }
    return;
  }
  if (act === 'plan.ceil'){
    if (isFinite(num) && num > 0) applyEdits([{ kind:'set_plan', ceiling:num }], 'plan settings');
    return;
  }
  if (dpSel && dpSel.t === 'room'){
    const r = p.rooms.find(x => x.id === dpSel.k); if (!r) return;
    if (act === 'room.rename'){
      const id = v.trim();
      if (id && id !== r.id){
        dpSel = { t:'room', k:id }; selectedRoomId = id;   // follow the room across the rename
        applyEdits([{ kind:'rename_room', room:r.id, to:id }], 'rename room');
      }
    }
    else if (act === 'room.type') applyEdits([{ kind:'set_room_type', room:r.id, type:v }], 'room type');
    else if (act === 'room.w' || act === 'room.l'){
      const w = act === 'room.w' ? num : r.w, l = act === 'room.l' ? num : r.l;
      if (isFinite(w) && isFinite(l) && w > 0 && l > 0)
        applyEdits([{ kind:'resize_room', room:r.id, w:w, l:l }], 'resize room');
    }
    else if (act === 'room.x' || act === 'room.y'){
      const x = act === 'room.x' ? num : r.x, y = act === 'room.y' ? num : r.y;
      if (isFinite(x) && isFinite(y)) applyEdits([{ kind:'move_room', room:r.id, x:x, y:y }], 'move room');
    }
    return;
  }
  if (dpSel && dpSel.t === 'op'){
    const o = (p.openings || []).find(x => x.key === dpSel.k); if (!o) return;
    const base = { kind:'set_opening', opening:o.kind, key:o.key };
    if (act === 'op.width' && isFinite(num) && num > 0) applyEdits([Object.assign(base, { width:num })], 'opening width');
    else if (act === 'op.offset' && isFinite(num) && num >= 0) applyEdits([Object.assign(base, { offset:num })], 'opening offset');
    else if (act === 'op.into') applyEdits([Object.assign(base, { into: v || null })], 'door swing');
    else if (act === 'op.hinge' && v) applyEdits([Object.assign(base, { hinge:v })], 'door swing');
    else if (act === 'op.sill' && isFinite(num) && num >= 0) applyEdits([Object.assign(base, { sill:num })], 'window sill');
    return;
  }
  if (dpSel && dpSel.t === 'fx'){
    const f = (p.fixtures || []).find(x => x.id === dpSel.k); if (!f) return;
    const base = { kind:'set_fixture', id:f.id };
    if (act === 'fx.rotate' && isFinite(num)) applyEdits([Object.assign(base, { rotate:num })], 'fixture');
    else if (act === 'fx.wall' && v) applyEdits([Object.assign(base, { wall:v })], 'fixture');
    else if (act === 'fx.width' && isFinite(num) && num > 0) applyEdits([Object.assign(base, { width:num })], 'fixture');
    return;
  }
  if (dpSel && dpSel.t === 'inst'){
    const inst = (p.instances || []).find(x => x.alias === dpSel.k); if (!inst) return;
    if (act === 'inst.x' || act === 'inst.y'){
      const x = act === 'inst.x' ? num : inst.x, y = act === 'inst.y' ? num : inst.y;
      if (isFinite(x) && isFinite(y)) applyEdits([{ kind:'move_use', alias:inst.alias, x:x, y:y }], 'move part');
    } else if (act === 'inst.level'){
      const lv = parseInt(v, 10);
      if (isFinite(lv) && lv >= 0 && lv !== inst.level)
        applyEdits([{ kind:'set_use', alias:inst.alias, level:lv }], 'part level');
    } else if (act === 'inst.mirror'){
      applyEdits([{ kind:'set_use', alias:inst.alias, mirror: v || null }], 'part mirror');
    } else if (act === 'inst.rotate'){
      const a = parseInt(v, 10) || 0;
      applyEdits([{ kind:'set_use', alias:inst.alias, rotate: a }], 'part rotate');
    }
    return;
  }
  if (dpSel && dpSel.t === 'note'){
    const n = (p.notes || []).find(x => x.index === dpSel.k); if (!n) return;
    const base = { kind:'set_note', index:n.index };
    if (act === 'note.text'){ const t = v.trim();
      if (t && t !== n.text) applyEdits([Object.assign(base, { text:t })], 'edit note'); }
    else if (act === 'note.x' && isFinite(num)) applyEdits([Object.assign(base, { x:num })], 'move note');
    else if (act === 'note.y' && isFinite(num)) applyEdits([Object.assign(base, { y:num })], 'move note');
  }
}

function dpDelete(){
  const p = lastGood; if (!p || !dpSel) return;
  const sel = dpSel; dpSel = null;
  if (sel.t === 'room'){
    selectedRoomId = null;
    applyEdits([{ kind:'delete_room', room:sel.k }], 'delete room');
  } else if (sel.t === 'op'){
    const o = (p.openings || []).find(x => x.key === sel.k);
    if (o) applyEdits([{ kind:'delete_opening', opening:o.kind, key:o.key }], 'delete opening');
  } else if (sel.t === 'note'){
    applyEdits([{ kind:'delete_note', index:sel.k }], 'delete note');
  } else {
    applyEdits([{ kind:'delete_fixture', id:sel.k }], 'delete fixture');
  }
}

// The ＋ Note button: drop a placeholder callout at the centre of the current
// floor's rooms (or the whole plan), then select it — drag it or edit x/y after.
function addNoteAtCenter(){
  const p = lastGood;
  if (!p || !p.rooms || !p.rooms.length){ dpNote('add a room before placing a note', true); return; }
  const lvl = editMode ? editLevel : 0;
  let rs = p.rooms.filter(r => r.level === lvl);
  if (!rs.length) rs = p.rooms;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const r of rs){ minX = Math.min(minX, r.x); minY = Math.min(minY, r.y);
    maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.l); }
  const cx = snap((minX + maxX) / 2), cy = snap((minY + maxY) / 2);
  const ed = { kind:'add_note', text:'note', x:cx, y:cy };
  if (lvl) ed.level = lvl;
  applyEdits([ed], 'add note').then(ok => {
    if (!ok) return;
    const notes = (lastGood && lastGood.notes) || [];
    if (notes.length) dpSelect('note', notes[notes.length - 1].index);
  });
}

function submitRoomForm(){
  const p = lastGood; if (!p) return;
  const id = document.getElementById('nr-id').value.trim();
  const type = document.getElementById('nr-type').value;
  const w = parseFloat(document.getElementById('nr-w').value);
  const l = parseFloat(document.getElementById('nr-l').value);
  const anchor = document.getElementById('nr-anchor').value;
  const of = document.getElementById('nr-of').value;
  if (!id || !(w > 0) || !(l > 0)){ dpNote('a room needs a name and a positive size', true); return; }
  const ofRoom = p.rooms.find(r => r.id === of);
  dpForm = null;
  // "auto" lets the server pick the first free spot (no overlap); an explicit
  // anchor keeps the old relative placement.
  const ed = { kind:'add_room', id:id, type:type, w:w, l:l,
               level: ofRoom ? ofRoom.level : 0 };
  if (anchor === 'auto'){ ed.auto = true; }
  else { ed.anchor = anchor; ed.of = of; }
  applyEdits([ed], 'add room').then(ok => { if (ok) dpSelect('room', id); });
}
function submitFixtureForm(){
  const p = lastGood; if (!p || !dpSel || dpSel.t !== 'room') return;
  const r = p.rooms.find(x => x.id === dpSel.k); if (!r) return;
  const kind = document.getElementById('nf-kind').value;
  const wall = document.getElementById('nf-wall').value;
  const along = document.getElementById('nf-along').value;
  dpForm = null;
  // A counter with an `along run` wall set becomes a wall-length countertop run
  // (the compiler sizes it from the wall); an optional from/to (ft-in aware) makes
  // it partial. Everything else lands mid-room and is dragged into place.
  if (kind === 'counter' && along){
    const ed = { kind:'add_fixture', room:r.id, fkind:'counter', along:along };
    const a = parseFtIn(document.getElementById('nf-from').value);
    const b = parseFtIn(document.getElementById('nf-to').value);
    if (a != null && b != null && isFinite(a) && isFinite(b) && b > a){ ed.from = a; ed.to = b; }
    applyEdits([ed], 'add counter run');
    return;
  }
  // Room-local drop point: roughly centred (the footprint isn't known here — the
  // compiler sizes the piece), clamped so a tiny room still gets a legal corner.
  const ed = { kind:'add_fixture', room:r.id, fkind:kind,
    x: snap(Math.max(0, r.w / 2 - 1.5)), y: snap(Math.max(0, r.l / 2 - 1.5)) };
  if (wall) ed.wall = wall;
  applyEdits([ed], 'add fixture');
}
// ＋ Electrical — mirrors ＋ Fixture: a type selector plus a wall+offset for an
// outlet/switch (a light lands mid-room and can be dragged, like a fixture).
function addElectricalForm(r){
  return '<div class="dp-form"><div class="dp-grid">' +
    '<label>type</label><select class="wide" id="ne-kind">' +
    '<option value="outlet" selected>outlet</option>' +
    '<option value="gfci">outlet (GFCI)</option>' +
    '<option value="switch">switch</option>' +
    '<option value="light">ceiling light</option>' +
    '<option value="smoke">smoke alarm</option>' +
    '<option value="co">CO alarm</option>' +
    '<option value="smoke_co">smoke+CO alarm</option></select>' +
    '<label>wall</label><select class="wide" id="ne-wall" title="Wall the outlet/switch sits on (ignored for a light/alarm)">' +
    optList(['N', 'S', 'E', 'W'], 'S') + '</select>' +
    '<label>offset</label><input type="text" inputmode="text" id="ne-offset" ' +
    'title="ft from the wall\\u2019s S/W end (outlet/switch)" value="3">' +
    '</div><div class="dp-btns"><button data-btn="elecsubmit">Add device</button>' +
    '<button data-btn="formcancel">Cancel</button></div>' +
    '<div class="dp-note">Outlet/switch sit on the chosen wall; a light or smoke/CO alarm lands mid-room. Toggle ⚡ to see them.</div></div>';
}
function submitElectricalForm(){
  const p = lastGood; if (!p || !dpSel || dpSel.t !== 'room') return;
  const r = p.rooms.find(x => x.id === dpSel.k); if (!r) return;
  const kind = document.getElementById('ne-kind').value;
  const wall = document.getElementById('ne-wall').value;
  let offset = parseFtIn(document.getElementById('ne-offset').value);
  if (!isFinite(offset) || offset < 0) offset = 3;
  dpForm = null;
  let ed;
  if (kind === 'smoke' || kind === 'co' || kind === 'smoke_co'){
    ed = { kind:'add_alarm', room:r.id, akind:kind };   // room-level ceiling device
  } else if (kind === 'light'){
    ed = { kind:'add_light', room:r.id, lkind:'ceiling',
      x: snap(Math.max(0, r.w / 2)), y: snap(Math.max(0, r.l / 2)) };
  } else if (kind === 'switch'){
    ed = { kind:'add_switch', room:r.id, wall:wall, offset:snap(offset) };
  } else {
    ed = { kind:'add_outlet', room:r.id, wall:wall, offset:snap(offset), gfci:(kind === 'gfci') };
  }
  applyEdits([ed], 'add electrical');
}
// A same-type, same-size twin on the first side with clear floor (checked against
// this level's rooms and the envelope); when every side is taken it still lands
// east — the compiler's overlap diagnostic takes over as the teacher.
function duplicateRoom(r){
  const p = lastGood; if (!p) return;
  const id = nextRoomId(p, r.id.replace(/\d+$/, '') || r.id);
  const env = (p.settings && p.settings.envelope) || null;
  const clear = (x, y) =>
    (!env || (x >= 0 && y >= 0 && x + r.w <= env[0] && y + r.l <= env[1])) &&
    !p.rooms.some(o => o.level === r.level &&
      x < o.x + o.w && o.x < x + r.w && y < o.y + o.l && o.y < y + r.l);
  const sides = [['east-of', r.x + r.w, r.y], ['west-of', r.x - r.w, r.y],
    ['south-of', r.x, r.y - r.l], ['north-of', r.x, r.y + r.l]];
  const pick = sides.find(s => clear(s[1], s[2]));
  applyEdits([{ kind:'add_room', id:id, type:r.type, w:r.w, l:r.l,
                anchor:(pick ? pick[0] : 'east-of'), of:r.id, level:r.level }], 'duplicate room')
    .then(ok => { if (ok) dpSelect('room', id); });
}
// Duplicate an instance: a fresh alias for the same part, offset a little so the
// stamped twin doesn't land exactly on the original (an add_use edit).
function duplicateInstance(){
  const p = lastGood; if (!p || !dpSel || dpSel.t !== 'inst') return;
  const inst = (p.instances || []).find(x => x.alias === dpSel.k); if (!inst) return;
  const alias = mintAlias(p, inst.alias);
  const off = 3;
  const ed = { kind:'add_use', relpath:inst.relpath, alias:alias,
    x:inst.x + off, y:inst.y + off, level:inst.level };
  if (inst.mirror) ed.mirror = inst.mirror;
  if (inst.rotate) ed.rotate = inst.rotate;
  applyEdits([ed], 'duplicate part').then(ok => { if (ok) dpSelect('inst', alias); });
}
function submitOpeningForm(){
  const p = lastGood; if (!p || !dpSel || dpSel.t !== 'room') return;
  const r = p.rooms.find(x => x.id === dpSel.k); if (!r) return;
  const width = parseFloat(document.getElementById('no-width').value);
  if (!(width > 0)){ dpNote('width must be positive', true); return; }
  const kind = dpForm.op; dpForm = null;
  if (kind === 'door'){
    const b = document.getElementById('no-b').value;
    const style = document.getElementById('no-doortype').value;   // door | open
    applyEdits([{ kind:'add_opening', opening:style, a:r.id, b:b, width:width }], 'add ' + style);
  } else {
    const side = document.getElementById('no-side').value;
    applyEdits([{ kind:'add_opening', opening:kind, room:r.id, side:side, width:width }], 'add ' + kind);
  }
}

dpEl.addEventListener('click', e => {
  const part = e.target.closest('[data-part]');
  if (part){ insertPart(part.getAttribute('data-part')); return; }
  const row = e.target.closest('[data-sel]');
  if (row){
    const raw = row.getAttribute('data-sel'), i = raw.indexOf(':');
    dpSelect(raw.slice(0, i), raw.slice(i + 1));
    return;
  }
  const btn = e.target.closest('[data-btn]');
  if (!btn) return;
  const b = btn.getAttribute('data-btn');
  if (b === 'addroom'){ dpForm = dpForm === 'room' ? null : 'room'; renderPanel(); }
  else if (b === 'parts'){ dpForm = dpForm === 'parts' ? null : 'parts'; renderPanel(); }
  else if (b === 'addnote'){ addNoteAtCenter(); }
  else if (b === 'adddoor' || b === 'addwindow' || b === 'addentry'){
    dpForm = { op: b.slice(3) }; renderPanel();
  }
  else if (b === 'addfix'){ dpForm = dpForm === 'fx' ? null : 'fx'; renderPanel(); }
  else if (b === 'addelec'){ dpForm = dpForm === 'elec' ? null : 'elec'; renderPanel(); }
  else if (b === 'formcancel'){ dpForm = null; renderPanel(); }
  else if (b === 'roomsubmit') submitRoomForm();
  else if (b === 'opsubmit') submitOpeningForm();
  else if (b === 'fxsubmit') submitFixtureForm();
  else if (b === 'elecsubmit') submitElectricalForm();
  else if (b === 'duproom'){
    const p = lastGood, r = p && dpSel && dpSel.t === 'room' && p.rooms.find(x => x.id === dpSel.k);
    if (r) duplicateRoom(r);
  }
  else if (b === 'dupinst'){ duplicateInstance(); }
  else if (b === 'inlineinst'){
    if (dpSel && dpSel.t === 'inst') applyEdits([{ kind:'inline_use', alias:dpSel.k }], 'inline part');
  }
  else if (b === 'delinst'){
    if (dpSel && dpSel.t === 'inst'){ const a = dpSel.k; dpSel = null;
      applyEdits([{ kind:'delete_use', alias:a }], 'delete part'); }
  }
  else if (b === 'delroom' || b === 'delop' || b === 'delfx' || b === 'delnote') dpDelete();
});
dpEl.addEventListener('change', e => {
  // "+ Room": follow the type with a matching default name (bed2/bath2/…) until
  // the user hand-edits the name field.
  if (e.target.id === 'nr-type'){
    const idEl = document.getElementById('nr-id');
    if (idEl && !idEl.dataset.dirty && lastGood)
      idEl.value = typedRoomId(lastGood, e.target.value);
  }
  const act = e.target.getAttribute && e.target.getAttribute('data-act');
  if (act) dpChange(act, e.target);
});
dpEl.addEventListener('input', e => {
  if (e.target.id === 'nr-id') e.target.dataset.dirty = '1';
});
// On-screen-keyboard safety: when a panel field takes focus, scroll it into view so
// the iPad keyboard (which resizes the layout via interactive-widget) can't hide it.
dpEl.addEventListener('focusin', e => {
  const el = e.target;
  if (el && (el.tagName === 'INPUT' || el.tagName === 'SELECT'))
    try { el.scrollIntoView({ block:'nearest' }); } catch(_){}
});
(function initPanel(){
  let v = null; try { v = localStorage.getItem(LS_PANEL); } catch (e){}
  if (v) togglePanel(true);
})();

// -- unified undo/redo history (one timeline for typing, smart edits and drags) --
// history[histIndex] always mirrors the on-screen text (value+selection); its label
// names the change that produced it, so undo removes history[histIndex] and redo
// re-applies history[histIndex+1]. Programmatic replacement breaks the textarea's
// native undo, so every writer records here instead.
function histInit(v){
  history = [{ v: v, s: 0, e: 0, label: 'initial' }];
  histIndex = 0; histMirror = v; lastEditKind = 'boot';
  updateUndoRedo();
}
function histCommit(v, s, e, label){
  // A write that changes nothing on screen must not mint an undo step — e.g. a
  // blur re-firing `change` after a committed edit posts a no-op whose returned
  // source is identical; pushing it would make the next undo appear dead.
  if (v === histMirror) return;
  history.length = histIndex + 1;                 // a new change discards any redo tail
  history.push({ v: v, s: s, e: e, label: label });
  histIndex = history.length - 1;
  if (history.length > HIST_CAP){ history.shift(); histIndex--; }   // cap depth, drop oldest
  histMirror = v;
  updateUndoRedo();
}
// The single funnel every programmatic mutation uses: snapshot the pre-edit state
// into history, then swap in the new text + selection. Callers run their own refresh
// (schedule / compile / applyResult) afterwards, exactly as before.
function applyEdit(v, s, e, label){
  histCommit(v, s, e, label);
  editor.value = v;
  if (s != null){ editor.selectionStart = s; editor.selectionEnd = (e == null ? s : e); }
  lastEditKind = 'edit';                           // the next keystroke opens a fresh typing burst
}
// Typing path: the editor already holds the new text when `input` fires, so we fold
// keystrokes into the current burst (updating history[histIndex] in place) unless a
// new burst is warranted — a >COALESCE_MS gap, or the previous change wasn't typing.
// Composition never fractures a burst.
function recordTyping(){
  const v = editor.value, s = editor.selectionStart, e = editor.selectionEnd, now = Date.now();
  const cont = lastEditKind === 'type' && (composing || (now - lastTypeTime) <= COALESCE_MS);
  if (cont){
    const st = history[histIndex]; st.v = v; st.s = s; st.e = e; histMirror = v;
  } else {
    histCommit(v, s, e, 'typing');                 // the pre-input state stays at the prior index
  }
  lastEditKind = 'type'; lastTypeTime = now;
  updateUndoRedo();
}
// Restore whatever history[histIndex] now points at, then refresh like an ordinary
// source change (immediate compile, not the debounced schedule).
function histRestore(){
  const st = history[histIndex];
  editor.value = st.v;
  editor.selectionStart = st.s != null ? st.s : st.v.length;
  editor.selectionEnd = st.e != null ? st.e : editor.selectionStart;
  histMirror = st.v; lastEditKind = 'restore';     // next keystroke = fresh burst
  if (acOpen) hideAc();
  renderGutter();
  if (findOpen) runFind(true); else renderHighlight();
  compile();
  updateUndoRedo();
}
function doUndo(){ if (histIndex <= 0) return; histIndex--; histRestore(); }
function doRedo(){ if (histIndex >= history.length - 1) return; histIndex++; histRestore(); }
function updateUndoRedo(){
  const canU = histIndex > 0, canR = histIndex < history.length - 1;
  undoBtn.disabled = !canU; redoBtn.disabled = !canR;
  undoBtn.title = canU ? ('Undo ' + history[histIndex].label + ' (' + MOD + '+Z)') : 'Nothing to undo';
  redoBtn.title = canR ? ('Redo ' + history[histIndex + 1].label + ' (' + MOD + '+Shift+Z)')
                       : 'Nothing to redo';
}
function isUndoKey(e){ return (e.metaKey || e.ctrlKey) && !e.shiftKey && (e.key === 'z' || e.key === 'Z'); }
function isRedoKey(e){ return (e.metaKey || e.ctrlKey) &&
  ((e.shiftKey && (e.key === 'z' || e.key === 'Z')) || e.key === 'y' || e.key === 'Y'); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape'){ cancelDrag(); cancelNudge(); clearMultiSel(true);
    if (measureMode) setMeasure(false); return; }
  if (isUndoKey(e) || isRedoKey(e)){
    const el = document.activeElement;
    // The editor handles its own (and stopped propagation); the agent brief, find /
    // replace inputs, help search and design-panel fields keep native per-field undo.
    if (el === editor || el === briefEl || el === findInput ||
        el === replaceInput || el === helpSearch ||
        (el && el.closest && el.closest('.design-panel'))) return;
    e.preventDefault();
    if (isRedoKey(e)) doRedo(); else doUndo();
    return;
  }
  // Layout keys below never fire while typing in a field, and take no ⌘/Ctrl/Alt.
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (document.activeElement || {}).tagName;
  if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT') return;
  // `[` / `]` step the active floor while editing.
  if (editMode && editLevels.length > 1 && (e.key === '[' || e.key === ']')){
    e.preventDefault();
    const i = editLevels.indexOf(editLevel), j = e.key === ']' ? i + 1 : i - 1;
    if (j >= 0 && j < editLevels.length) setEditLevel(editLevels[j]);
    return;
  }
  // `m` toggles the measure tape (edit mode only — that's where the overlay lives).
  if (editMode && (e.key === 'm' || e.key === 'M')){
    e.preventDefault(); setMeasure(!measureMode); return;
  }
  // Arrows nudge the selection; Shift steps a whole 3 ft build module. With a
  // 2+ room multi-selection the whole set moves as one batched edit; otherwise
  // the single selected room, unchanged.
  if (editMode && (e.key === 'ArrowLeft' || e.key === 'ArrowRight' ||
      e.key === 'ArrowUp' || e.key === 'ArrowDown')){
    const members = multiSel.size >= 2 ? selectedRooms()
      : (roomById(selectedRoomId) ? [roomById(selectedRoomId)] : []);
    if (!members.length) return;
    e.preventDefault();
    const step = e.shiftKey ? 3 : 1;
    nudgeMembers(members,
      e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0,
      e.key === 'ArrowDown' ? -step : e.key === 'ArrowUp' ? step : 0,
      members.length > 1 ? 'nudge rooms' : 'nudge room');
    return;
  }
  // `r` spins the inspected fixture a quarter turn (a seed materialises, like a drag).
  if ((e.key === 'r' || e.key === 'R') && dpSel && dpSel.t === 'fx' && lastGood){
    const f = (lastGood.fixtures || []).find(x => x.id === dpSel.k); if (!f) return;
    e.preventDefault();
    applyEdits([{ kind:'set_fixture', id:f.id, rotate: ((f.rotate || 0) + 90) % 360 }], 'rotate fixture');
    return;
  }
  // Delete removes whatever the inspector holds — one undo brings it all back.
  if (e.key === 'Delete' && dpSel){
    e.preventDefault(); dpDelete();
  }
});
initEdit();

// --- workspace: autosave / open / save / new / export -----------------------
const noticeEl = document.getElementById('notice');
const noticeMsg = document.getElementById('notice-msg');
const noticeActions = document.getElementById('notice-actions');
const noticeDismiss = document.getElementById('notice-dismiss');
const newBtn = document.getElementById('new-btn');
const openBtn = document.getElementById('open-btn');
const saveBtn = document.getElementById('save-btn');
const fileInput = document.getElementById('file-input');
const exportBtn = document.getElementById('export-btn');
const exportMenu = document.getElementById('export-menu');
const editorWrap = document.getElementById('editor-wrap');

let savedSource = INITIAL_SOURCE;   // last text persisted to localStorage (beforeunload baseline)
let checkpoint = INITIAL_SOURCE;    // last deliberate known state (New confirms if we've drifted)
let lastCompileOk = false;          // did the last compile pass cleanly? (gates compiled exports)
let autosaveOff = false;            // suppressed while offering a newer session over a FILE arg

// -- dismissible, non-modal notice bar --
function showNotice(msg, actions){
  noticeMsg.textContent = msg;
  noticeActions.innerHTML = '';
  for (const a of (actions || [])){
    const b = document.createElement('button');
    b.className = 'na' + (a.ghost ? ' ghost' : '');
    b.textContent = a.label;
    b.addEventListener('click', () => { hideNotice(); if (a.fn) a.fn(); });
    noticeActions.appendChild(b);
  }
  noticeEl.hidden = false;
}
function hideNotice(){ noticeEl.hidden = true; noticeActions.innerHTML = ''; }
noticeDismiss.addEventListener('click', hideNotice);

// -- autosave to localStorage (called on every applyResult) --
function autosave(){
  if (autosaveOff) return;
  try {
    localStorage.setItem(LS_SOURCE, editor.value);
    localStorage.setItem(LS_SAVED_AT, String(Date.now()));
    savedSource = editor.value;      // now safely persisted → beforeunload stays quiet
  } catch (e){ /* private mode / quota: leave savedSource stale so beforeunload warns */ }
}
function readSaved(){ try { return localStorage.getItem(LS_SOURCE); } catch (e){ return null; } }

// -- one funnel for programmatic source replacement (open / new / example) --
// Loading a document is itself undoable — Ctrl+Z after it returns to what you had.
function setSource(src, label){
  checkpoint = src;
  applyEdit(src, 0, 0, label || 'load');
  renderGutter(); compile();
}

// -- export menu --
function updateExportState(ok){
  lastCompileOk = ok;
  exportBtn.title = ok ? 'Export the current plan'
    : 'Only the .barn source exports until the plan compiles cleanly';
}
function toggleExportMenu(show){
  const open = show == null ? exportMenu.hidden : show;
  exportMenu.hidden = !open;
  exportBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) exportMenu.querySelectorAll('.menu-item').forEach(it => {
    const dis = it.getAttribute('data-fmt') !== 'barn' && !lastCompileOk;
    it.disabled = dis;
    it.title = dis ? 'Fix the plan’s errors to export this format' : '';
    it.style.opacity = dis ? '.45' : '';
    it.style.cursor = dis ? 'default' : 'pointer';
  });
}
exportBtn.addEventListener('click', e => { e.stopPropagation(); toggleExportMenu(); });
document.addEventListener('click', e => {
  if (!exportMenu.hidden && !e.target.closest('.menu')) toggleExportMenu(false);
});
exportMenu.addEventListener('click', e => {
  const item = e.target.closest('.menu-item'); if (!item || item.disabled) return;
  toggleExportMenu(false); doExport(item.getAttribute('data-fmt'));
});

function planSlug(){
  const s = (titleEl.textContent || '').trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return s || 'barndo';
}
function downloadBlob(blob, name){
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; document.body.appendChild(a); a.click();
  a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
}
function downloadSource(){
  const src = editor.value;
  downloadBlob(new Blob([src], { type:'text/plain' }), planSlug() + '.barn');
  checkpoint = src; autosave();      // downloaded → a known, persisted state
}
async function doExport(fmt){
  if (fmt === 'barn'){ downloadSource(); return; }
  if (!lastCompileOk){ showNotice('Fix the plan’s errors before exporting ' + fmt + '.'); return; }
  try {
    const resp = await fetch('/api/export', { method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ source: editor.value, format: fmt }) });
    if ((resp.headers.get('Content-Type') || '').indexOf('application/json') >= 0){
      let m = 'the server refused it';
      try { const j = await resp.json(); m = (j.error && (j.error.message || j.error)) || m; } catch (e){}
      showNotice('Export failed: ' + m); return;
    }
    const blob = await resp.blob();
    let name = planSlug() + '.' + fmt;
    const mm = /filename="?([^"]+)"?/.exec(resp.headers.get('Content-Disposition') || '');
    if (mm) name = mm[1];
    downloadBlob(blob, name);
  } catch (err){ showNotice('Export failed: ' + String(err)); }
}

// -- open (file picker + drag/drop, read client-side) --
function openText(text){ autosaveOff = false; hideNotice(); setSource(text, 'open file'); }
function readFileInto(f){
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => openText(String(rd.result || ''));
  rd.readAsText(f);
}
openBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  readFileInto(fileInput.files && fileInput.files[0]);
  fileInput.value = '';              // let the same file be re-opened
});
['dragenter','dragover'].forEach(ev => editorWrap.addEventListener(ev, e => {
  if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0){
    e.preventDefault(); editorWrap.classList.add('dragover'); }
}));
['dragleave','dragend'].forEach(ev => editorWrap.addEventListener(ev, e => {
  if (e.target === editorWrap) editorWrap.classList.remove('dragover'); }));
editorWrap.addEventListener('drop', e => {
  e.preventDefault(); editorWrap.classList.remove('dragover');
  readFileInto(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
});

// -- new plan (scaffold; confirm via the notice if the user has drifted) --
function newPlan(){ autosaveOff = false; hideNotice(); setSource(SCAFFOLD_SOURCE, 'new plan'); }
newBtn.addEventListener('click', () => {
  if (editor.value.trim() && editor.value !== checkpoint){
    showNotice('Start a new plan? The current one is replaced (it stays autosaved in this browser).',
      [{ label:'New plan', fn:newPlan }, { label:'Keep editing', ghost:true }]);
  } else { newPlan(); }
});

// -- save (download .barn) --
saveBtn.addEventListener('click', downloadSource);

// -- format (canonical reformat; comments & pragmas preserved) --
// POSTs to /api/fmt (the same normalizer the CLI's `barndsl fmt` uses) and lands
// the result through applyEdit, so it's ONE undo step (Ctrl+Z restores the
// pre-format text) — the same checkpoint funnel a quick-fix uses.
const fmtBtn = document.getElementById('fmt-btn');
async function formatSource(){
  const src = editor.value;
  try {
    const resp = await fetch('/api/fmt', { method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ source: src }) });
    const j = await resp.json();
    if (j.error){
      showNotice('Format: ' + ((j.error && (j.error.message || j.error)) || 'could not format'));
      return;
    }
    if (!j.changed){ showNotice('Already formatted — nothing to change.'); return; }
    applyEdit(j.source, null, null, 'format');   // one undo step
    renderGutter(); compile();
  } catch (err){ showNotice('Format failed: ' + String(err)); }
}
fmtBtn.addEventListener('click', formatSource);
document.addEventListener('keydown', e => {   // Shift+Alt+F = Format (VS Code parity)
  if (e.shiftKey && e.altKey && (e.key === 'f' || e.key === 'F')){
    e.preventDefault(); formatSource();
  }
});

// -- keyboard: Ctrl/Cmd+S = Save, +O = Open, +F = Find, +H = Find & replace --
document.addEventListener('keydown', e => {
  if (!(e.metaKey || e.ctrlKey)) return;
  const k = e.key.toLowerCase();
  if (k === 's'){ e.preventDefault(); downloadSource(); }
  else if (k === 'o'){ e.preventDefault(); fileInput.click(); }
  else if (k === 'f'){ e.preventDefault(); openFind(false); }
  else if (k === 'h'){ e.preventDefault(); openFind(true); }
});

// -- warn on leave only when the editor differs from the persisted copy --
window.addEventListener('beforeunload', e => {
  if (editor.value !== savedSource){ e.preventDefault(); e.returnValue = ''; return ''; }
});

// -- the first user edit ends any "we kept your file" suppression --
editor.addEventListener('input', () => { if (autosaveOff){ autosaveOff = false; autosave(); } });

// --- boot -------------------------------------------------------------------
(function boot(){
  const saved = readSaved();
  const hasNewer = saved != null && saved !== INITIAL_SOURCE;
  if (hasNewer && INITIAL_FROM_FILE){
    // The FILE argument wins on screen, but never clobber the autosaved session:
    // keep it in localStorage and merely offer it through the notice.
    autosaveOff = true;                 // this compile must not overwrite the saved copy
    savedSource = INITIAL_SOURCE; checkpoint = INITIAL_SOURCE;
    editor.value = INITIAL_SOURCE; renderGutter(); compile();
    showNotice('Showing the file you opened — a newer autosaved session is also available.',
      [{ label:'Restore session', fn: () => { autosaveOff = false; setSource(saved, 'restore session'); } }]);
  } else if (hasNewer){
    // No FILE argument → restore the autosaved session outright.
    savedSource = saved; checkpoint = saved;
    editor.value = saved; renderGutter(); compile();
    showNotice('Restored your last session — Load example or New plan to start over.',
      [{ label:'New plan', fn:newPlan }]);
  } else {
    savedSource = INITIAL_SOURCE; checkpoint = INITIAL_SOURCE;
    editor.value = INITIAL_SOURCE; renderGutter(); compile();
  }
  histInit(editor.value);   // seed the undo timeline with the booted source (the floor state)
})();
</script>
</body>
</html>
"""
