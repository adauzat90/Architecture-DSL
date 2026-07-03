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
    single-file viewer embeds), ``score``, ``metrics``, ``elevations`` and
    ``section``. Bad DSL is a normal ``200`` response with diagnostics, never a
    ``500``; only malformed/oversize JSON is ``400``.
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
from .gltf import build_scene
from .render import render_svg
from .score import design_score
from .viewer import RENDERER_JS, _LAYER_LABELS, scene_json
from .views import elevation_svg, section_svg

#: Maximum accepted request body (bytes) for POST /api/compile — a generous cap
#: for hand-written DSL; anything larger is rejected with 400 rather than parsed.
MAX_BODY = 1_000_000

_ELEVATION_SIDES = ("south", "north", "east", "west")


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
        except Exception as exc:  # a plan that lowers oddly must not 500 the API
            payload["render_error"] = str(exc)
    return payload


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
    """Routes the four playground endpoints; everything else is 404.

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
        if path not in ("/api/compile", "/api/design", "/api/design/cancel"):
            self._json({"error": "not found"}, status=404)
            return
        data = self._read_json_body()
        if data is self._BODY_ERROR:
            return
        if path == "/api/compile":
            self._handle_compile(data)
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
        rounds = data.get("iterations", 3)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 8:
            rounds = 3

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
        self, address: tuple[str, int], initial_source: str, designer: Designer | None = None
    ):
        super().__init__(address, _Handler)
        self.initial_source = initial_source
        self.app_html = render_app(initial_source)
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
) -> _PlaygroundServer:
    """Build (but do not start) the playground server bound to ``host:port``.

    ``port=0`` binds an ephemeral port (used by the tests). ``initial_source``
    preloads the editor; ``None`` uses :func:`default_source`. ``designer``
    overrides the agent loop driver (tests inject a keyless fake); ``None`` uses
    the real Claude agent, imported lazily only when a design job runs. Localhost
    by default — this is a local tool, so it never binds ``0.0.0.0`` implicitly.
    """
    source = initial_source if initial_source is not None else default_source()
    return _PlaygroundServer((host, port), source, designer=designer)


def run(
    initial_source: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
) -> int:
    """Start the playground and serve until interrupted. Returns a process code."""
    httpd = make_server(host, port, initial_source=initial_source)
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


def render_app(initial_source: str) -> str:
    """Return the playground SPA HTML with the renderer and starter source inlined.

    Placeholders are filled by :meth:`str.replace` (not ``str.format``) so the
    embedded CSS/JS braces need no escaping. The 3D view embeds the exact same
    :data:`~barndsl.viewer.RENDERER_JS` the single-file viewer uses.
    """
    return (
        _APP_HTML
        .replace("__RENDERER_JS__", RENDERER_JS)
        .replace("__LAYER_LABELS__", json.dumps(_LAYER_LABELS))
        .replace("__INITIAL_SOURCE__", _js_string(initial_source))
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
  header { display:flex; align-items:center; gap:14px; padding:9px 16px;
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
  main { display:flex; height:calc(100% - 44px); }
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
</style>
</head>
<body>
<header>
  <div class="brand">barndsl <span>playground</span></div>
  <div id="plan-title"></div>
  <div id="score-chip" class="chip" title="">score</div>
  <div id="metrics"></div>
  <div class="spacer"></div>
  <label class="examples">example
    <select id="example-select"><option value="">loading…</option></select>
  </label>
</header>
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
    <div class="editor-wrap">
      <div class="gutter" id="gutter"></div>
      <textarea id="editor" spellcheck="false" autocapitalize="off"
        autocomplete="off" wrap="off"></textarea>
    </div>
    <div class="diagnostics" id="diagnostics"></div>
  </section>
  <section class="right">
    <div class="tabs">
      <button class="tab active" data-tab="plan">2D plan</button>
      <button class="tab" data-tab="three">3D</button>
      <button class="tab" data-tab="views">Elevations</button>
    </div>
    <div class="viewport" id="viewport">
      <div class="pane active" id="pane-plan"><div class="svgbox" id="plan-svg"></div></div>
      <div class="pane" id="pane-three">
        <canvas id="three-canvas"></canvas>
        <div id="three-panel"><div class="hd">Layers</div><div id="three-toggles"></div></div>
      </div>
      <div class="pane" id="pane-views"></div>
    </div>
  </section>
</main>
<script>__RENDERER_JS__</script>
<script>
const LAYER_LABELS = __LAYER_LABELS__;
const INITIAL_SOURCE = __INITIAL_SOURCE__;

const editor = document.getElementById('editor');
const gutter = document.getElementById('gutter');
const diagEl = document.getElementById('diagnostics');
const planSvg = document.getElementById('plan-svg');
const viewsPane = document.getElementById('pane-views');
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
}
editor.addEventListener('scroll', () => { gutter.scrollTop = editor.scrollTop; });
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
    renderViews(p);
    if (currentTab === 'three') showThree();
  } else if (lastGood){
    viewport.classList.add('stale');  // keep the last good render, dimmed
  } else {
    planSvg.innerHTML = '';
  }
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
}

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
}
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
    if (svg) html += '<figure><figcaption>' + pair[1] +
      ' elevation</figcaption><div class="svgbox">' + svg + '</div></figure>'; }
  if (p.section) html += '<figure><figcaption>Section</figcaption>' +
    '<div class="svgbox">' + p.section + '</div></figure>';
  viewsPane.innerHTML = html + '</div>';
}

// --- 2D plan pan / zoom (CSS transform) -------------------------------------
(function(box){
  let scale = 1, tx = 0, ty = 0, dragging = false, ox = 0, oy = 0;
  function apply(){ const svg = box.querySelector('svg'); if (!svg) return;
    svg.style.transformOrigin = '0 0';
    svg.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')'; }
  box.addEventListener('wheel', e => { e.preventDefault();
    scale = Math.min(8, Math.max(0.2, scale * Math.exp(-e.deltaY * 0.0012))); apply(); },
    { passive:false });
  box.addEventListener('pointerdown', e => { dragging = true; ox = e.clientX - tx;
    oy = e.clientY - ty; box.setPointerCapture(e.pointerId); box.style.cursor = 'grabbing'; });
  box.addEventListener('pointerup', () => { dragging = false; box.style.cursor = 'grab'; });
  box.addEventListener('pointermove', e => { if (!dragging) return;
    tx = e.clientX - ox; ty = e.clientY - oy; apply(); });
  box.addEventListener('dblclick', () => { scale = 1; tx = 0; ty = 0; apply(); });
})(planSvg);

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
    if (opt && opt.dataset.src != null){ editor.value = opt.dataset.src;
      renderGutter(); compile(); }
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

// --- boot -------------------------------------------------------------------
editor.value = INITIAL_SOURCE;
renderGutter();
compile();
</script>
</body>
</html>
"""
