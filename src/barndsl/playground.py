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
    (:mod:`barndsl.edits`: move/resize a room, slide an opening) and return
    ``{source, line, changed, ...compile_payload(new_source)}``. A refused edit
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

import json
import os
import threading
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .compiler import DSL_REFERENCE, compile_source
from .elements import RoomType
from .cost import estimate_cost
from .dxf import to_dxf
from .edits import EditError, apply_edit, edit_from_json, opening_overlays
from .energy import describe_targets, envelope_targets
from .fixtures import resolve_room_fixtures
from .gltf import build_scene, to_glb
from .ifc import to_ifc
from .packet import build_packet
from .render import ROOM_COLORS, render_svg
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
    "open", "entry", "window", "porch", "stair", "frame",
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
)


def _highlight_tokens() -> dict:
    """The token vocabulary the editor's syntax highlighter uses.

    ``types`` comes straight from :class:`~barndsl.elements.RoomType` (the real
    source of room-type names) and ``keywords`` from the compiler's statement
    dispatch plus the grammar's modifier words — derived, not re-invented, so the
    highlighting tracks the language rather than drifting from it.
    """
    return {
        "keywords": sorted(set(_STATEMENT_KEYWORDS) | set(_MODIFIER_KEYWORDS)),
        "types": [t.value for t in RoomType],
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
    """Starter DSL to preload the editor: the cedar_ridge example, else a scaffold."""
    path = os.path.join(_examples_dir(), "cedar_ridge.barn")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        from .scaffold import starter_dsl

        return starter_dsl("My Barndo")


# --- the compile endpoint payload --------------------------------------------


def compile_payload(source: str) -> dict:
    """Compile ``source`` and build the JSON the playground returns.

    Always includes ``ok``/``counts``/``diagnostics`` (the same shape as
    :meth:`CompileResult.to_dict`). When the source built a real (non-recovered)
    plan it also carries the render artifacts — ``svg``, ``scene``, ``score``,
    ``metrics``, ``elevations``, ``section``, ``title`` — which are pure
    functions of the plan. A parse-recovered partial plan omits them (the
    frontend keeps its last good render), matching the CLI's build contract.
    Never raises on bad DSL; artifact-build failures are reported as
    ``render_error`` rather than propagating.
    """
    result = compile_source(source)
    payload = result.to_dict()
    payload["recovered"] = result.recovered
    plan = result.plan
    if plan is not None and not result.recovered:
        try:
            payload["title"] = plan.name
            payload["svg"] = render_svg(plan)
            payload["scene"] = scene_json(build_scene(plan))
            payload["score"] = design_score(result).to_dict()
            payload["metrics"] = plan.metrics()
            payload["elevations"] = {
                side: elevation_svg(plan, side) for side in _ELEVATION_SIDES
            }
            payload["section"] = section_svg(plan)
            # Compact overlay data for Tier 5 edit mode — the frontend draws its
            # interactive SVG from these (not the static plan SVG).
            payload["rooms"] = [
                {
                    "id": r.id, "type": r.type.value,
                    "x": r.x, "y": r.y, "w": r.width, "l": r.length,
                    "level": r.level, "color": ROOM_COLORS.get(r.type, "#f0f0f0"),
                    "line": result.room_lines.get(r.id),
                }
                for r in plan.rooms
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
                    })
            payload["fixtures"] = fixtures
            # The Report tab's data — cost, schedules, areas and (if the plan
            # declares one) the climate envelope. Cheap enough to inline: for the
            # gallery plans it adds <1 ms and <8 KB to the compile payload (measured),
            # so it rides along rather than a lazily-fetched second endpoint.
            payload["report"] = report_data(result)
        except Exception as exc:  # a plan that lowers oddly must not 500 the API
            payload["render_error"] = str(exc)
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
    bytes; text formats are UTF-8 (DXF R12 is ASCII, matching :func:`~barndsl.dxf.save_dxf`).
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
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: object, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, html: str, status: int = 200) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _download(self, body: bytes, ctype: str, filename: str) -> None:
        """Send ``body`` as a file download (attachment ``Content-Disposition``)."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

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
                from .agent import agent_availability

                available, reason = agent_availability()
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
            "/api/compile", "/api/edit", "/api/export", "/api/design", "/api/design/cancel"
        ):
            self._json({"error": "not found"}, status=404)
            return
        data = self._read_json_body()
        if data is self._BODY_ERROR:
            return
        if path == "/api/compile":
            self._handle_compile(data)
        elif path == "/api/edit":
            self._handle_edit(data)
        elif path == "/api/export":
            self._handle_export(data)
        elif path == "/api/design":
            self._handle_design(data)
        else:
            self._handle_cancel(data)

    def _handle_compile(self, data: object) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("source"), str):
            self._json({"error": 'expected {"source": "<dsl>"}'}, status=400)
            return
        try:
            payload = compile_payload(data["source"])
        except Exception as exc:  # a real bug — bad DSL never reaches here
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        self._json(payload)

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
        try:
            result = apply_edit(data["source"], edit)
        except Exception as exc:  # a real bug — refused edits return typed errors
            self._json({"error": f"internal error: {exc}"}, status=500)
            return
        if result.error is not None:
            self._json({"error": {"kind": result.error.kind, "message": result.error.message}})
            return
        payload = compile_payload(result.source)
        payload["source"] = result.source
        payload["line"] = result.line
        payload["changed"] = result.changed
        payload["summary"] = result.summary
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
        from .agent import resolve_max_iterations

        default_rounds = min(8, max(1, resolve_max_iterations()))
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
            from .agent import agent_availability

            available, reason = agent_availability()
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
    ):
        super().__init__(address, _Handler)
        self.initial_source = initial_source
        self.app_html = render_app(initial_source, from_file=from_file)
        self.examples = load_examples()
        self.designer = designer
        self.design_lock = threading.Lock()
        self.jobs_lock = threading.Lock()
        self.current_job: dict | None = None


def make_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    initial_source: str | None = None,
    designer: Designer | None = None,
    from_file: bool = False,
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
    return _PlaygroundServer((host, port), source, designer=designer, from_file=from_file)


def run(
    initial_source: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
    from_file: bool = False,
) -> int:
    """Start the playground and serve until interrupted. Returns a process code."""
    httpd = make_server(host, port, initial_source=initial_source, from_file=from_file)
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
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  .left { width:36%; min-width:280px; display:flex; flex-direction:column;
    border-right:1px solid var(--line); }
  .right { flex:1; display:flex; flex-direction:column; min-width:0; }

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
    border-top:1px solid var(--line); font-size:12.5px; }
  .diag-head { display:flex; gap:8px; align-items:center; padding:7px 12px;
    position:sticky; top:0; background:var(--panel); border-bottom:1px solid var(--line); }
  .count { font-size:11.5px; font-weight:600; padding:2px 8px; border-radius:20px;
    border:1px solid var(--line); }
  .count.error { color:var(--err); } .count.warning { color:var(--warn); }
  .count.info { color:var(--info); } .count.zero { color:var(--faint); opacity:.65; }
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
  .plan-body { position:relative; flex:1; min-height:0; }
  .edit-layer { position:absolute; inset:0; background:var(--bg); }
  .edit-layer svg { width:100%; height:100%; display:block; touch-action:none;
    -webkit-user-select:none; user-select:none; }
  .ov-room { cursor:move; }
  .ov-open { cursor:grab; }
  .ov-handle { fill:var(--accent2); stroke:#fff; }
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
    padding:0; }
  .plan-body .svgbox svg { position:absolute; top:0; left:0; transform-origin:0 0; }

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

  /* --- print the current viewport, not the three-pane app chrome --- */
  @media print {
    header, #notice, .agent, .left, .tabs, .edit-bar, #three-panel,
    .drop-hint, .zoom-ctl, #dim-chip, .score-pop, .help-backdrop, .help-panel,
    .lightbox { display:none !important; }
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
    <input type="file" id="file-input" accept=".barn,.txt" hidden>
  </div>
  <label class="examples">example
    <select id="example-select"><option value="">loading…</option></select>
  </label>
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
      <textarea id="brief" spellcheck="false"
        placeholder="Describe the barndo you want — e.g. &quot;3 bed 2 bath, open kitchen, 2-car shop bay, ~1800 sq ft&quot;. Then Design."></textarea>
      <div class="composer-row">
        <button id="send-btn">Design</button>
        <button id="stop-btn" hidden>Stop</button>
      </div>
      <div class="agent-note" id="agent-note"></div>
    </div>
  </section>
  <section class="left">
    <div class="editor-wrap" id="editor-wrap">
      <div class="gutter" id="gutter"></div>
      <div class="editor-stack">
        <pre id="hl" aria-hidden="true"></pre>
        <textarea id="editor" spellcheck="false" autocapitalize="off"
          autocomplete="off" wrap="off"></textarea>
      </div>
      <div class="drop-hint">Drop a .barn file to open</div>
    </div>
    <div class="diagnostics" id="diagnostics"></div>
  </section>
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
          <label class="edit-toggle"><input type="checkbox" id="edit-mode"> Edit layout</label>
          <button id="undo-btn" disabled title="Undo last edit (Ctrl/Cmd+Z)">↶ Undo</button>
          <span class="level-switch" id="level-switch" hidden></span>
          <span class="edit-note" id="edit-note"></span>
        </div>
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
      <div class="pane" id="pane-three">
        <canvas id="three-canvas"></canvas>
        <div id="three-panel"><div class="hd">Layers</div><div id="three-toggles"></div></div>
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
<script>__RENDERER_JS__</script>
<script>
const LAYER_LABELS = __LAYER_LABELS__;
const INITIAL_SOURCE = __INITIAL_SOURCE__;
const INITIAL_FROM_FILE = __INITIAL_FROM_FILE__;   // server started with an explicit FILE arg
const SCAFFOLD_SOURCE = __SCAFFOLD_SOURCE__;        // "New plan" starter
const HIGHLIGHT = __HIGHLIGHT__;                    // {keywords, types} for the editor highlighter
const LS_SOURCE = 'barndsl.playground.source';
const LS_SAVED_AT = 'barndsl.playground.savedAt';

const editor = document.getElementById('editor');
const gutter = document.getElementById('gutter');
const hl = document.getElementById('hl');
const diagEl = document.getElementById('diagnostics');
const planSvg = document.getElementById('plan-svg');
const viewsPane = document.getElementById('pane-views');
const reportWrap = document.getElementById('report-wrap');
const titleEl = document.getElementById('plan-title');
const scoreChip = document.getElementById('score-chip');
const metricsEl = document.getElementById('metrics');
const viewport = document.getElementById('viewport');

let diagnostics = [];
let lastGood = null;     // last payload that carried a full render
let scene3d = null;      // last good 3D scene json
let ctrl = null;         // 3D renderer controller
let threeInit = false;   // mountScene attempted (canvas may be replaced)
let sceneLoaded = false;  // scene3d currently uploaded to ctrl
let currentTab = 'plan';

function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmt(n){ return (Math.round(n*10)/10).toString(); }
function trimNum(n){ return (Math.round(n*10)/10).toString().replace(/\.0$/,''); }

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
function renderHighlight(){
  // A trailing newline keeps the <pre> the same height as the textarea's content.
  hl.innerHTML = editor.value.split('\n').map(hlLine).join('\n') + '\n';
  hl.scrollTop = editor.scrollTop; hl.scrollLeft = editor.scrollLeft;
}
function syncScroll(){ gutter.scrollTop = editor.scrollTop;
  hl.scrollTop = editor.scrollTop; hl.scrollLeft = editor.scrollLeft; }

editor.addEventListener('scroll', syncScroll);
editor.addEventListener('input', () => { renderGutter(); schedule(); });
editor.addEventListener('keydown', e => {
  if (e.key === 'Tab'){ e.preventDefault(); insertText('  '); }
  else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); compile(); }
});
function insertText(t){
  const s = editor.selectionStart, e = editor.selectionEnd;
  editor.value = editor.value.slice(0, s) + t + editor.value.slice(e);
  editor.selectionStart = editor.selectionEnd = s + t.length;
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
    planSvg.innerHTML = p.svg;
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
  ['Send to the agent', MOD + '+Enter'], ['Undo a layout edit', MOD + '+Z'],
  ['Zoom in / out / fit', '+  −  0'], ['Compile now', MOD + '+Enter'],
  ['Cancel a drag', 'Esc'], ['Switch floor (edit mode)', '[  ]'],
  ['Open this help', '?'],
];
let helpRefLines = null;   // cached parsed reference lines (fetched once)

//: 3D-view tips, shown in the help panel (the first-person walkthrough in particular).
const THREE_TIPS = [
  'Walk mode: on the 3D tab, click Walk (or press Enter) to step inside at eye height.',
  'WASD or the arrow keys move relative to where you look; the mouse looks around; ' +
    'Shift runs. You slide along walls and pass through doorways.',
  'Walk up the stairs to reach the upper floor; Esc (or leaving the tab) exits back to orbit.',
];

//: Edit-mode direct-manipulation tips (drag behaviours), shown in the help panel.
const EDIT_TIPS = [
  'Drag a room to move it; drag its handles to resize. Edges snap to neighbours.',
  'Drag a door or window along its wall to re-position it.',
  'Drag a fixture to move it. An authored fixture rewrites its `at x,y`; a dashed ' +
    'auto-seed (bath/kitchen/laundry) becomes an authored `fixture` line where you drop it.',
  'Add fixtures in the DSL: `fixture <kind> in <room> [at <x>,<y>] [wall N|S|E|W] [rotate <deg>]`.',
  'Every drag is one surgical text edit and joins the Undo stack.',
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

// Global keys: `?` opens help (when not typing); Esc closes the open overlay.
document.addEventListener('keydown', e => {
  const el = document.activeElement, tag = el && el.tagName;
  const typing = tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT';
  if (e.key === '?' && !typing){ e.preventDefault(); openHelp(); return; }
  if (e.key === 'Escape'){
    if (lb && !lb.hidden){ closeLightbox(); return; }
    if (!helpPanel.hidden){ closeHelp(); return; }
    if (!scorePop.hidden){ toggleScorePop(false); return; }
  }
});

// --- diagnostics list -------------------------------------------------------
function countChip(kind, n){
  return '<span class="count ' + kind + (n ? '' : ' zero') + '">' + n + ' ' + kind +
    (n === 1 ? '' : 's') + '</span>';
}
function renderDiagnostics(p){
  const ds = p.diagnostics || [], c = p.counts || { error:0, warning:0, info:0 };
  let head = '<div class="diag-head">' + countChip('error', c.error) +
    countChip('warning', c.warning) + countChip('info', c.info) +
    (p.ok ? '<span class="ok">✓ compiles clean</span>' : '') + '</div>';
  if (!ds.length){ diagEl.innerHTML = head + '<div class="diag-empty">No diagnostics.</div>'; return; }
  let rows = '';
  for (const d of ds){
    rows += '<div class="diag-row sev-' + d.severity + '" data-line="' + (d.line || '') + '">' +
      '<span class="sev">' + d.severity + '</span>' +
      '<span class="code">' + esc(d.code) + '</span>' +
      '<span class="msg">' +
        (d.line ? '<span class="loc">L' + d.line + (d.col ? ':' + d.col : '') + '</span> ' : '') +
        esc(d.message) + (d.room ? ' <em>(' + esc(d.room) + ')</em>' : '') +
        (d.hint ? '<span class="hint">' + esc(d.hint) + '</span>' : '') +
      '</span></div>';
  }
  diagEl.innerHTML = head + rows;
}
diagEl.addEventListener('click', e => {
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
}
function renderViews(p){
  if (!p.elevations){ viewsPane.innerHTML = '<div class="diag-empty">No views.</div>'; return; }
  const order = [['south','South'],['north','North'],['east','East'],['west','West']];
  let html = '<div class="views-grid">';
  for (const pair of order){ const svg = p.elevations[pair[0]];
    if (svg) html += '<figure data-view="' + pair[0] + '" title="Click to zoom"><figcaption>' +
      pair[1] + ' elevation</figcaption><div class="svgbox">' + svg + '</div></figure>'; }
  if (p.section) html += '<figure data-view="section" title="Click to zoom">' +
    '<figcaption>Section</figcaption><div class="svgbox">' + p.section + '</div></figure>';
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
  west:'West elevation', section:'Section' };
function openLightbox(view){
  const p = lastGood; if (!p) return;
  const svg = view === 'section' ? p.section : (p.elevations && p.elevations[view]);
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
        '<td class="num">' + trimNum(r.width) + '′ × ' + trimNum(r.length) + '′</td>' +
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
  const body =
    '<section class="sheet cover"><h1>' + esc(title) + '</h1>' +
      '<p class="tb">Drawing packet · ' + esc(date) + ' · score ' + esc(score) +
      (foot ? ' · ' + esc(foot) : '') + '</p></section>' +
    '<section class="sheet"><h2>Floor plan</h2><div class="svgwrap">' + p.svg + '</div>' +
      '<p class="pnote">Dimensions in feet — not to scale when printed; verify all dimensions.</p></section>' +
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
  box.addEventListener('pointerdown', e => { if (!svg()) return; dragging = true; moved = false;
    sx = e.clientX - tx; sy = e.clientY - ty; box.setPointerCapture(e.pointerId);
    if (box.hasAttribute('tabindex')) try { box.focus({ preventScroll:true }); } catch(_){}
    box.style.cursor = 'grabbing'; });
  box.addEventListener('keydown', zoomKeys);
  box.addEventListener('pointerup', e => { dragging = false; box.style.cursor = '';
    if (!moved && opts.onClick) opts.onClick(e); });
  box.addEventListener('pointermove', e => { if (!dragging) return;
    tx = e.clientX - sx; ty = e.clientY - sy; moved = true; apply(); });
  box.addEventListener('dblclick', () => fit());
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
    if (opt && opt.dataset.src != null){ hideNotice(); setSource(opt.dataset.src); }
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

collapseBtn.addEventListener('click', () => {
  const collapsed = agentPane.classList.toggle('collapsed');
  collapseBtn.textContent = collapsed ? '›' : '‹';
  collapseBtn.title = (collapsed ? 'expand' : 'collapse') + ' the agent pane';
});

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
    if (ev.source != null && ev.source !== editor.value){ pushUndo(editor.value); }
    if (ev.source != null){ editor.value = ev.source; renderGutter(); }
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

// --- Tier 5: direct-manipulation edit mode ----------------------------------
// An interactive SVG overlay drawn from the payload's `rooms`/`openings`. Drags
// become surgical DSL text edits (POST /api/edit) so the source stays the source
// of truth; the editor text and viewport swap to the server's rewritten source.
const editChk = document.getElementById('edit-mode');
const editLayer = document.getElementById('edit-layer');
const undoBtn = document.getElementById('undo-btn');
const editNoteEl = document.getElementById('edit-note');
const dimChip = document.getElementById('dim-chip');
const planBody = document.querySelector('.plan-body');
const levelSwitch = document.getElementById('level-switch');
let editLevel = 0;                   // the floor the overlay currently edits
let editMode = false, editReady = false;
let editRooms = [], editOpens = [], editLevels = [0], editFixtures = [];
let allRooms = [], allOpens = [], allStairs = [], allFixtures = [];   // every level — the dimmed underlay
let selectedRoomId = null, svgEl = null, ghostEl = null, drag = null, ov = null;
const undoStack = [];

function snap(v){ return Math.round(v * 2) / 2; }          // 0.5 ft grid
function Y(py){ return ov.MID - py; }                       // plan y (north up) → svg y
function roomById(id){ return editRooms.find(r => r.id === id); }
function openByKey(k){ return editOpens.find(o => o.key === k); }
function roomLine(id){ const r = roomById(id); return r ? r.line : null; }
function editNote(msg, isErr){ editNoteEl.textContent = msg || '';
  editNoteEl.className = 'edit-note' + (isErr ? ' err' : ''); }

function initEdit(){
  // Parse an inline <svg> so the SVG namespace comes from the DOM (no namespace
  // URL literal in the page — the app stays free of external-looking references).
  editLayer.innerHTML = '<svg preserveAspectRatio="xMidYMid meet"></svg>';
  svgEl = editLayer.firstChild;
  svgEl.addEventListener('pointerdown', onDown);
  svgEl.addEventListener('pointermove', onMove);
  svgEl.addEventListener('pointerup', onUp);
  svgEl.addEventListener('pointercancel', cancelDrag);
  editChk.addEventListener('change', () => {
    editMode = editChk.checked; editLayer.hidden = !editMode;
    planSvg.style.display = editMode ? 'none' : '';
    document.getElementById('plan-zoom').style.display = editMode ? 'none' : '';
    renderLevelSwitcher();
    if (editMode) buildOverlay(); else { selectedRoomId = null; editNote(''); planZoom.refit(); }
  });
  undoBtn.addEventListener('click', doUndo);
  levelSwitch.addEventListener('click', e => {
    const b = e.target.closest('[data-level]'); if (!b) return;
    setEditLevel(parseInt(b.getAttribute('data-level'), 10));
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
}
function fixtureByKey(k){ return editFixtures.find(f => f.id === k); }
function setEditLevel(lvl){
  if (editLevels.indexOf(lvl) < 0 || lvl === editLevel) return;
  editLevel = lvl; selectedRoomId = null;
  applyLevelFilter(); renderLevelSwitcher();
  if (editMode) buildOverlay();
}

function refreshEditData(p){
  if (p && p.rooms){
    allRooms = p.rooms; allOpens = p.openings || []; allStairs = p.stairs || [];
    allFixtures = p.fixtures || [];
    editLevels = p.levels || [0];
    if (editLevels.indexOf(editLevel) < 0) editLevel = editLevels[0] || 0;  // clamp
    applyLevelFilter(); editReady = true;
  }
  renderLevelSwitcher();
  if (editMode) buildOverlay();
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
  const hs = Math.max(0.8, Math.min(2.2, Math.min(W, H) * 0.032));
  let s = '';
  // Dimmed context: rooms on the other floors, as non-interactive outlines.
  for (const r of allRooms){
    if (r.level === editLevel) continue;
    s += '<rect class="ov-under" x="' + r.x + '" y="' + Y(r.y + r.l) +
      '" width="' + r.w + '" height="' + r.l + '" vector-effect="non-scaling-stroke"/>' +
      '<text class="ov-under-t" x="' + (r.x + r.w / 2) + '" y="' + (Y(r.y + r.l / 2) + fs * 0.3) +
      '" text-anchor="middle" font-size="' + (fs * 0.72) + '">' + esc(r.id) + '</text>';
  }
  for (const r of editRooms){
    const sel = r.id === selectedRoomId;
    s += '<rect class="ov-room" data-room="' + esc(r.id) + '" x="' + r.x + '" y="' + Y(r.y + r.l) +
      '" width="' + r.w + '" height="' + r.l + '" fill="' + r.color + '" stroke="' +
      (sel ? '#2F6FB0' : '#2b2b2b') + '" stroke-width="' + (sel ? 2.4 : 1) +
      '" vector-effect="non-scaling-stroke"/>' +
      '<text x="' + (r.x + r.w / 2) + '" y="' + (Y(r.y + r.l / 2) - fs * 0.1) + '" text-anchor="middle" ' +
      'font-size="' + fs + '" fill="#333" style="pointer-events:none">' + esc(r.id) + '</text>' +
      '<text x="' + (r.x + r.w / 2) + '" y="' + (Y(r.y + r.l / 2) + fs * 1.05) + '" text-anchor="middle" ' +
      'font-size="' + (fs * 0.72) + '" fill="#777" style="pointer-events:none">' +
      trimNum(r.w) + '×' + trimNum(r.l) + '</text>';
  }
  // Fixtures on this floor — draggable. A seed is dashed (a drag materialises it
  // into an authored `fixture` line); an authored fixture is solid.
  for (const f of editFixtures){
    s += '<rect class="ov-fixture' + (f.seed ? ' seed' : '') + '" data-fixkey="' + esc(f.id) +
      '" x="' + f.x + '" y="' + Y(f.y + f.l) + '" width="' + f.w + '" height="' + f.l +
      '" vector-effect="non-scaling-stroke"/>' +
      '<text class="ov-fix-t" x="' + (f.x + f.w / 2) + '" y="' + (Y(f.y + f.l / 2) + fs * 0.28) +
      '" text-anchor="middle" font-size="' + (fs * 0.6) + '" style="pointer-events:none">' +
      esc(f.kind.replace(/_/g, ' ')) + '</text>';
  }
  for (const o of editOpens){
    const seg = openSeg(o, o.offset);
    const col = o.kind === 'window' ? '#2F6FB0' : '#c0392b';
    s += '<line class="ov-open" data-okey="' + esc(o.key) + '" x1="' + seg[0].x + '" y1="' + Y(seg[0].y) +
      '" x2="' + seg[1].x + '" y2="' + Y(seg[1].y) + '" stroke="' + col +
      '" stroke-width="4.5" vector-effect="non-scaling-stroke" stroke-linecap="round"/>';
  }
  // Stair footprints touching this floor (run or landing) — drawn over the rooms
  // so the cross-level anchor stays visible even where a room sits on it; inert
  // (pointer-events:none) so the room beneath stays draggable.
  for (const t of allStairs){
    if (t.from !== editLevel && t.to !== editLevel) continue;
    const up = t.from === editLevel;
    s += '<rect class="ov-stair" x="' + t.x + '" y="' + Y(t.y + t.l) +
      '" width="' + t.w + '" height="' + t.l + '" vector-effect="non-scaling-stroke"/>' +
      '<text class="ov-stair-t" x="' + (t.x + t.w / 2) + '" y="' + (Y(t.y + t.l / 2) + fs * 0.3) +
      '" text-anchor="middle" font-size="' + (fs * 0.72) + '">' +
      esc(t.id) + (up ? ' ↑' + t.to : ' ↓' + t.from) + '</text>';
  }
  const r = roomById(selectedRoomId);
  if (r){
    const pts = [['sw', r.x, r.y], ['s', r.x + r.w / 2, r.y], ['se', r.x + r.w, r.y],
      ['e', r.x + r.w, r.y + r.l / 2], ['ne', r.x + r.w, r.y + r.l], ['n', r.x + r.w / 2, r.y + r.l],
      ['nw', r.x, r.y + r.l], ['w', r.x, r.y + r.l / 2]];
    for (const p of pts){
      s += '<rect class="ov-handle h-' + p[0] + '" data-handle="' + p[0] + '" data-room="' + esc(r.id) +
        '" x="' + (p[1] - hs / 2) + '" y="' + (Y(p[2]) - hs / 2) + '" width="' + hs + '" height="' + hs +
        '" vector-effect="non-scaling-stroke"/>';
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

// -- pointer interactions --
function onDown(e){
  if (!editMode) return;
  const P = toPlan(e);
  const handleEl = e.target.closest('[data-handle]');
  const openEl = e.target.closest('[data-okey]');
  const fixEl = e.target.closest('[data-fixkey]');
  const roomEl = e.target.closest('[data-room]');
  if (fixEl){
    const f = fixtureByKey(fixEl.getAttribute('data-fixkey')); if (!f) return;
    drag = { kind:'fixture', f, P, cur:{ x:f.x, y:f.y, w:f.w, l:f.l }, calc:{ x:f.x, y:f.y }, moved:false };
  } else if (handleEl){
    const id = handleEl.getAttribute('data-room'), r = roomById(id); if (!r) return;
    drag = { kind:'resize', id, h:handleEl.getAttribute('data-handle'), P,
      cur:{ x:r.x, y:r.y, w:r.w, l:r.l }, calc:{ x:r.x, y:r.y, w:r.w, l:r.l }, moved:false };
  } else if (openEl){
    const o = openByKey(openEl.getAttribute('data-okey')); if (!o) return;
    drag = { kind:'open', o, P, offset:o.offset, moved:false };
  } else if (roomEl){
    const id = roomEl.getAttribute('data-room'), r = roomById(id); if (!r) return;
    if (id !== selectedRoomId){ selectedRoomId = id; buildOverlay(); }
    drag = { kind:'move', id, P, cur:{ x:r.x, y:r.y, w:r.w, l:r.l },
      calc:{ x:r.x, y:r.y, w:r.w, l:r.l }, moved:false };
  } else { return; }
  addGhost(drag);
  try { svgEl.setPointerCapture(e.pointerId); } catch(_){}
  e.preventDefault();
}
function onMove(e){
  if (!drag) return;
  const P = toPlan(e);
  clearGuides();
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
    showDim(esc(drag.id) + ' — ' + trimNum(drag.cur.w) + ' × ' + trimNum(drag.cur.l) +
      ' at ' + trimNum(nx) + ', ' + trimNum(ny), e);
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
    const delta = (dw ? (dw > 0 ? '+' : '') + trimNum(dw) + "' w" : '') +
      (dw && dl ? '  ' : '') + (dl ? (dl > 0 ? '+' : '') + trimNum(dl) + "' l" : '');
    showDim(trimNum(c.w) + ' × ' + trimNum(c.l) +
      (delta ? '<span class="delta">' + delta + '</span>' : ''), e);
  } else if (drag.kind === 'fixture'){
    const nx = snap(drag.cur.x + (P.x - drag.P.x)), ny = snap(drag.cur.y + (P.y - drag.P.y));
    drag.calc = { x:nx, y:ny };
    if (nx !== drag.cur.x || ny !== drag.cur.y) drag.moved = true;
    placeGhostRect(nx, ny, drag.cur.w, drag.cur.l);
    const rm = allRooms.find(r => r.id === drag.f.room);
    const lx = nx - (rm ? rm.x : 0), ly = ny - (rm ? rm.y : 0);
    showDim(esc(drag.f.kind.replace(/_/g, ' ')) + ' — at ' + trimNum(lx) + ', ' + trimNum(ly), e);
  } else {
    const o = drag.o;
    const off = Math.max(o.min, Math.min(o.max, snap(projOffset(o, P) - o.width / 2)));
    drag.offset = off; if (Math.abs(off - o.offset) > 1e-9) drag.moved = true;
    placeGhostLine(o, off);
    showDim('offset ' + trimNum(off), e);
  }
}
function onUp(e){
  if (!drag) return;
  const d = drag; drag = null; removeGhost(); clearGuides(); hideDim();
  try { svgEl.releasePointerCapture(e.pointerId); } catch(_){}
  if (d.kind === 'move'){
    if (!d.moved){ const ln = roomLine(d.id); if (ln) jumpToLine(ln); return; }
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
    if (!d.moved){ if (d.f.line) jumpToLine(d.f.line); return; }
    const rm = allRooms.find(r => r.id === d.f.room);
    const lx = d.calc.x - (rm ? rm.x : 0), ly = d.calc.y - (rm ? rm.y : 0);
    if (d.f.seed)   // materialise the seed into an authored `fixture` line
      applyEdits([{ kind:'add_fixture', room:d.f.room, fkind:d.f.kind, wall:d.f.wall, x:lx, y:ly }]);
    else            // rewrite the explicit fixture's `at x,y`
      applyEdits([{ kind:'move_fixture', key:d.f.id, x:lx, y:ly }]);
  } else {
    if (!d.moved) return;
    applyEdits([{ kind:'move_opening', opening:d.o.kind, key:d.o.key, offset:d.offset }]);
  }
}
function cancelDrag(){ if (!drag) return; drag = null; removeGhost(); clearGuides(); hideDim(); buildOverlay(); }

// -- apply a sequence of edits atomically (from the client's view) --
async function applyEdits(edits){
  if (!edits.length){ buildOverlay(); return; }
  autosaveOff = false;   // a layout edit is a deliberate action — resume autosave
  const before = editor.value;
  let src = before, p = null;
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
    buildOverlay();                        // restore positions from the unchanged data
    return;
  }
  pushUndo(before);                        // snapshot the pre-edit source for undo
  editor.value = src; renderGutter();
  applyResult(p);
  if (p.line) flashLine(p.line);
  editNote('');
}
function flashLine(ln){
  const el = gutter.children[ln - 1]; if (!el) return;
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}

// -- undo stack (programmatic textarea replacement breaks native undo) --
function pushUndo(v){ undoStack.push(v); if (undoStack.length > 50) undoStack.shift(); updateUndo(); }
function updateUndo(){ undoBtn.disabled = !undoStack.length; }
function doUndo(){ if (!undoStack.length) return;
  editor.value = undoStack.pop(); renderGutter(); updateUndo(); compile(); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape'){ cancelDrag(); return; }
  if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z') && !e.shiftKey){
    if (document.activeElement !== editor && document.activeElement !== briefEl){
      e.preventDefault(); doUndo(); }
    return;
  }
  // `[` / `]` step the active floor while editing (no modifiers, not while typing).
  if (editMode && editLevels.length > 1 && !e.metaKey && !e.ctrlKey && !e.altKey &&
      (e.key === '[' || e.key === ']')){
    const tag = (document.activeElement || {}).tagName;
    if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT') return;
    e.preventDefault();
    const i = editLevels.indexOf(editLevel), j = e.key === ']' ? i + 1 : i - 1;
    if (j >= 0 && j < editLevels.length) setEditLevel(editLevels[j]);
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
function setSource(src){ checkpoint = src; editor.value = src; renderGutter(); compile(); }

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
function openText(text){ autosaveOff = false; hideNotice(); setSource(text); }
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
function newPlan(){ autosaveOff = false; hideNotice(); setSource(SCAFFOLD_SOURCE); }
newBtn.addEventListener('click', () => {
  if (editor.value.trim() && editor.value !== checkpoint){
    showNotice('Start a new plan? The current one is replaced (it stays autosaved in this browser).',
      [{ label:'New plan', fn:newPlan }, { label:'Keep editing', ghost:true }]);
  } else { newPlan(); }
});

// -- save (download .barn) --
saveBtn.addEventListener('click', downloadSource);

// -- keyboard: Ctrl/Cmd+S = Save, Ctrl/Cmd+O = Open --
document.addEventListener('keydown', e => {
  if (!(e.metaKey || e.ctrlKey)) return;
  const k = e.key.toLowerCase();
  if (k === 's'){ e.preventDefault(); downloadSource(); }
  else if (k === 'o'){ e.preventDefault(); fileInput.click(); }
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
      [{ label:'Restore session', fn: () => { autosaveOff = false; setSource(saved); } }]);
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
})();
</script>
</body>
</html>
"""
