"""Tests for the playground's agent (design) endpoint — Tier 3.

The contract these pin (see `barndsl.playground`):

- **SSE streaming**: `POST /api/design` is a `text/event-stream` of the
  compile-critique-revise loop — a `status` opener, one `iteration` per round
  carrying the FULL compile payload (so the viewport evolves live), and a final
  `done` that carries the **best** iteration, not the last;
- **refinement**: a `source` in the body seeds the loop (the fake asserts it
  arrived);
- **availability**: `GET /api/agent` reports `{available, reason}` and never the
  key's value; with the agent unavailable the design POST still opens a stream
  and emits a structured `error`, while the rest of the playground keeps working;
- **cancellation**: a running job stops when `POST /api/design/cancel` fires;
- **one job at a time**: a second concurrent design is a 409;
- the SPA ships the chat-pane markup and still has zero external references.

No network, no API key, no `anthropic`: a fake designer (injected via
`make_server(designer=...)`) yields scripted iterations of real compilable DSL.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
from contextlib import contextmanager
from typing import Any

from barndsl import compile_source
from barndsl.agent import DesignResult, DesignStep
from barndsl.playground import make_server, render_app
from barndsl.score import design_score

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

with open(os.path.join(EXAMPLES, "cedar_ridge.barn"), encoding="utf-8") as _fh:
    #: A known-clean, high-scoring plan.
    CLEAN = _fh.read()

#: Compiles fine but scores lower than CLEAN — the "regressed" final round.
MEDIOCRE = """\
plan "Mediocre"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,0 size 12 x 24
door living - bed width 2.67
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""


# --- fake designers ----------------------------------------------------------


def _step(i: int, source: str) -> DesignStep:
    result = compile_source(source, name=None)
    return DesignStep(i, result.source, result, None, design_score(result))


class FakeDesigner:
    """Yields scripted iterations of real DSL, best-iteration-wins on return.

    Mirrors `agent.design`'s hook protocol: it calls `on_phase`/`on_step` per
    round and returns a `DesignResult`. Records what it was called with so tests
    can assert the brief and refinement seed reached it.
    """

    def __init__(self, sources: list[str]):
        self.sources = sources
        self.record: dict[str, Any] = {}

    def __call__(
        self,
        brief: str,
        *,
        seed_source: str | None,
        max_iterations: int,
        on_step,
        on_phase,
        cancel,
    ) -> DesignResult:
        self.record.update(
            brief=brief, seed_source=seed_source, max_iterations=max_iterations
        )
        history: list[DesignStep] = []
        for i, src in enumerate(self.sources, start=1):
            if cancel():
                break
            on_phase("writing", i)
            on_phase("compiling", i)
            step = _step(i, src)
            history.append(step)
            on_step(step)
        if not history:
            empty = compile_source("", name=None)
            return DesignResult("", empty, history)
        best = max(history, key=lambda s: (s.score.total, s.iteration))
        return DesignResult(best.source, best.result, history)


class BlockingFakeDesigner:
    """Emits one phase, then blocks until `cancel()` — for the cancel/409 tests."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def __call__(self, brief, *, seed_source, max_iterations, on_step, on_phase, cancel):
        on_phase("writing", 1)
        self.started.set()
        while not cancel():
            threading.Event().wait(0.01)
        empty = compile_source("", name=None)
        return DesignResult("", empty, [])


# --- server + SSE helpers ----------------------------------------------------


@contextmanager
def running(designer=None):
    srv = make_server(host="127.0.0.1", port=0, designer=designer)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        event, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        if data:
            events.append((event, json.loads(data)))
    return events


def _design(srv, body):
    """POST /api/design and read the whole SSE stream (blocks until it closes)."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
    conn.request(
        "POST", "/api/design", body=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    ctype = resp.getheader("Content-Type") or ""
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, ctype, _parse_sse(raw)


def _kinds(events):
    return [e for e, _ in events]


def _first(events, kind):
    return next(d for e, d in events if e == kind)


# --- streaming: best-of, live payloads ---------------------------------------


def test_design_streams_status_iterations_and_done(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")  # availability gate passes
    # Sanity: the scenario really regresses on the final round.
    assert design_score(compile_source(CLEAN)).total > design_score(compile_source(MEDIOCRE)).total

    fake = FakeDesigner([CLEAN, MEDIOCRE])
    with running(fake) as srv:
        status, ctype, events = _design(srv, {"brief": "a cottage"})

    assert status == 200
    assert "text/event-stream" in ctype
    kinds = _kinds(events)
    assert kinds[0] == "status"  # opens with a phase update
    assert kinds.count("iteration") == 2
    assert kinds[-1] == "done"

    # iteration events carry the FULL compile payload so the viewport evolves live
    iters = [d for e, d in events if e == "iteration"]
    assert "<svg" in iters[0]["payload"]["svg"]
    assert iters[0]["payload"]["scene"]["nodes"]
    assert iters[0]["round"] == 1 and iters[1]["round"] == 2
    assert isinstance(iters[0]["score"]["total"], (int, float))

    # done carries the BEST iteration (round 1 = CLEAN), not the last (MEDIOCRE)
    done = _first(events, "done")
    assert done["round"] == 1
    assert done["source"].strip() == CLEAN.strip()
    assert done["payload"]["ok"] is True


def test_design_refinement_seeds_the_designer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    fake = FakeDesigner([CLEAN])
    with running(fake) as srv:
        status, _ctype, events = _design(
            srv, {"brief": "make the kitchen bigger", "source": MEDIOCRE}
        )
    assert status == 200
    assert fake.record["brief"] == "make the kitchen bigger"
    assert fake.record["seed_source"] == MEDIOCRE  # the editor source reached the loop
    assert _kinds(events)[-1] == "done"


def test_empty_source_is_a_fresh_generation_not_a_seed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    fake = FakeDesigner([CLEAN])
    with running(fake) as srv:
        _design(srv, {"brief": "a cottage", "source": ""})
    assert fake.record["seed_source"] is None


def test_iterations_count_is_honoured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    fake = FakeDesigner([CLEAN])
    with running(fake) as srv:
        _design(srv, {"brief": "a cottage", "iterations": 5})
    assert fake.record["max_iterations"] == 5


# --- availability ------------------------------------------------------------


def test_agent_probe_available_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
    with running() as srv:
        status, data = _request(srv, "GET", "/api/agent")
    assert status == 200
    body = json.loads(data)
    assert body["available"] is True and body["reason"] is None
    # never leak the key's value
    assert "test-key-never-used" not in data.decode("utf-8")


def test_agent_probe_reports_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with running() as srv:
        status, data = _request(srv, "GET", "/api/agent")
    body = json.loads(data)
    assert body["available"] is False
    assert "ANTHROPIC_API_KEY" in body["reason"]


def test_agent_probe_reports_missing_dependency(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # `import anthropic` fails
    with running() as srv:
        status, data = _request(srv, "GET", "/api/agent")
    body = json.loads(data)
    assert body["available"] is False
    assert "barndsl[agent]" in body["reason"]


def test_design_without_availability_streams_a_structured_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with running() as srv:  # default designer — must never be reached
        status, ctype, events = _design(srv, {"brief": "a cottage"})
        assert status == 200 and "text/event-stream" in ctype
        err = _first(events, "error")
        assert err["kind"] in ("unavailable", "missing_dependency")
        assert err["message"]
        # the rest of the playground still works
        cstatus, cdata = _request(
            srv, "POST", "/api/compile", json.dumps({"source": CLEAN})
        )
        assert cstatus == 200 and json.loads(cdata)["ok"] is True


# --- request validation ------------------------------------------------------


def test_design_missing_brief_is_400():
    with running(FakeDesigner([CLEAN])) as srv:
        status, _ = _request(srv, "POST", "/api/design", json.dumps({"source": CLEAN}))
    assert status == 400


def test_design_blank_brief_is_400():
    with running(FakeDesigner([CLEAN])) as srv:
        status, _ = _request(srv, "POST", "/api/design", json.dumps({"brief": "   "}))
    assert status == 400


# --- one job at a time + cancellation ----------------------------------------


def test_second_concurrent_design_is_409(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    fake = BlockingFakeDesigner()
    with running(fake) as srv:
        holder: dict = {}

        def run():
            holder["res"] = _design(srv, {"brief": "first"})

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert fake.started.wait(3), "the blocking design never started"

        # a second design while the first holds the lock → 409
        status, _ = _request(srv, "POST", "/api/design", json.dumps({"brief": "second"}))
        assert status == 409

        # compile stays responsive while the design job runs
        cstatus, cdata = _request(
            srv, "POST", "/api/compile", json.dumps({"source": CLEAN})
        )
        assert cstatus == 200 and json.loads(cdata)["ok"] is True

        # release the first job by cancelling it
        job = srv.current_job
        assert job is not None
        _request(srv, "POST", "/api/design/cancel", json.dumps({"id": job["id"]}))
        t.join(timeout=4)
        assert not t.is_alive()


def test_cancel_stops_a_running_job(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    fake = BlockingFakeDesigner()
    with running(fake) as srv:
        holder: dict = {}

        def run():
            holder["res"] = _design(srv, {"brief": "a cottage"})

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert fake.started.wait(3)

        job = srv.current_job
        assert job is not None
        status, data = _request(
            srv, "POST", "/api/design/cancel", json.dumps({"id": job["id"]})
        )
        assert status == 200 and json.loads(data)["cancelled"] is True

        t.join(timeout=4)
        assert not t.is_alive()
        _status, _ctype, events = holder["res"]
        err = _first(events, "error")
        assert err["kind"] == "cancelled"


def test_cancel_unknown_job_is_404():
    with running(FakeDesigner([CLEAN])) as srv:
        status, data = _request(
            srv, "POST", "/api/design/cancel", json.dumps({"id": "nope"})
        )
    assert status == 404
    assert json.loads(data)["cancelled"] is False


# --- the SPA markup ----------------------------------------------------------


def test_app_ships_the_chat_pane_and_no_external_references():
    html = render_app(CLEAN)
    for needle in ('id="agent-pane"', 'id="thread"', 'id="brief"', 'id="send-btn"',
                   'id="stop-btn"', "/api/design", "/api/agent"):
        assert needle in html, needle
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html


def test_default_server_uses_the_real_agent_designer():
    # No injected designer → the endpoint falls back to the real (lazy) agent
    # loop; we don't run it (no key), just assert the wiring default.
    srv = make_server(host="127.0.0.1", port=0)
    try:
        assert srv.designer is None
    finally:
        srv.server_close()
