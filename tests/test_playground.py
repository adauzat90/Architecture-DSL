"""Tests for the local web playground (`barndsl.playground`).

The playground's contract: a stdlib-only local server whose ``/api/compile`` is a
pure function of the DSL (render artifacts on a good plan, diagnostics on a bad
one — but always HTTP 200 for *any* DSL, never a 500), a stateless set of exactly
four routes, and a single-page app with no external network references that
embeds the same inline WebGL renderer the single-file viewer uses. These pin
that.
"""

from __future__ import annotations

import http.client
import json
import os
import threading

import pytest

from barndsl.playground import (
    MAX_BODY,
    compile_payload,
    load_examples,
    make_server,
    render_app,
)
from barndsl.viewer import RENDERER_JS

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

with open(os.path.join(EXAMPLES, "cedar_ridge.barn"), encoding="utf-8") as _fh:
    #: A known-clean plan (0 errors) — pins the ok=True / full-render path.
    CLEAN = _fh.read()

#: A plan that *builds* but trips a code check (bath has no access): it still
#: renders, but ``ok`` stays false — the semantic-error path.
WITH_ERROR = """\
plan "Willow Bend"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bedroom width 2.67
entry living south width 3 offset 10
window bedroom east width 4 offset 4
"""


@pytest.fixture()
def server():
    """A playground server on an ephemeral port, running in a background thread."""
    srv = make_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(srv, method, path, body=None):
    """Issue one request against ``srv`` and return ``(status, bytes)``."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _request_full(srv, method, path, body=None):
    """Like :func:`_request` but also returns the response headers (for downloads)."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    hdrs = dict(resp.getheaders())
    conn.close()
    return resp.status, hdrs, data


def _compile(srv, source):
    status, data = _request(srv, "POST", "/api/compile", json.dumps({"source": source}))
    return status, json.loads(data)


# --- the compile endpoint ----------------------------------------------------


def test_compile_happy_path_has_all_fields(server):
    status, p = _compile(server, CLEAN)
    assert status == 200
    assert p["ok"] is True
    assert "<svg" in p["svg"]
    assert p["scene"]["nodes"] and p["scene"]["layers"]
    assert isinstance(p["score"]["total"], (int, float))
    assert isinstance(p["diagnostics"], list)
    assert isinstance(p["metrics"]["footprint_sqft"], (int, float))
    # elevations + section are pure functions, included when cheap
    assert "<svg" in p["section"]
    assert set(p["elevations"]) == {"north", "south", "east", "west"}


def test_bad_dsl_is_200_with_diagnostics_not_500(server):
    # A parse error is a normal response: 200, ok=false, diagnostics with a line.
    status, p = _compile(server, 'plan "x"\nenvelope not a number\n')
    assert status == 200
    assert p["ok"] is False
    assert p["diagnostics"]
    assert any(d["line"] for d in p["diagnostics"])
    # last-good render fields are omitted on a parse-recovered plan
    assert "svg" not in p


def test_semantic_errors_still_render(server):
    # A plan that builds but fails a code check keeps its render (ok stays false).
    status, p = _compile(server, WITH_ERROR)
    assert status == 200
    assert p["ok"] is False
    assert p["counts"]["error"] >= 1
    assert "<svg" in p["svg"]  # geometry renders regardless of code diagnostics


def test_every_example_compiles_through_the_api(server):
    for ex in load_examples():
        status, p = _compile(server, ex["source"])
        assert status == 200, ex["name"]
        assert p["ok"] is True, f"{ex['name']}: {p['diagnostics']}"
        assert "<svg" in p["svg"]


def test_malformed_json_is_400(server):
    status, _ = _request(server, "POST", "/api/compile", "{not valid json")
    assert status == 400


def test_missing_source_is_400(server):
    status, _ = _request(server, "POST", "/api/compile", json.dumps({"nope": 1}))
    assert status == 400


def test_oversize_body_is_rejected(server):
    status, _ = _request(server, "POST", "/api/compile", "x" * (MAX_BODY + 1000))
    assert status == 400


# --- routing / statelessness -------------------------------------------------


def test_unknown_path_is_404_and_serves_no_files(server):
    for path in ("/nope", "/../pyproject.toml", "/etc/passwd", "/api/", "/index.html"):
        status, data = _request(server, "GET", path)
        assert status == 404, path
        # a JSON error, never file contents
        assert json.loads(data)["error"]


def test_get_root_serves_the_app_with_no_external_references(server):
    status, data = _request(server, "GET", "/")
    assert status == 200
    html = data.decode("utf-8")
    assert html.lstrip().startswith("<!doctype html>")
    assert "http://" not in html
    assert "https://" not in html
    assert "//cdn" not in html
    assert "<script src" not in html


def test_examples_endpoint_lists_bundled_plans(server):
    status, data = _request(server, "GET", "/api/examples")
    assert status == 200
    names = {ex["name"] for ex in json.loads(data)}
    assert "cedar_ridge.barn" in names
    assert any(n.startswith("gallery/") for n in names)
    for ex in json.loads(data):
        assert ex["source"].strip()


def test_reference_endpoint(server):
    status, data = _request(server, "GET", "/api/reference")
    assert status == 200
    assert "barndsl" in json.loads(data)["reference"].lower()


# --- the shared-renderer refactor -------------------------------------------


def test_app_embeds_the_shared_webgl_renderer():
    html = render_app(CLEAN)
    # The playground and the single-file viewer embed the *same* renderer asset.
    assert "function mountScene(" in RENDERER_JS
    assert RENDERER_JS.strip() in html
    assert "http" not in RENDERER_JS


def test_viewer_still_self_contained():
    # The refactor must keep the single-file viewer offline + self-contained.
    from barndsl import compile_source, viewer_html

    plan = compile_source(CLEAN).plan
    assert plan is not None
    html = viewer_html(plan)
    assert "http://" not in html and "https://" not in html
    assert "function mountScene(" in html


def test_compile_payload_is_pure_and_never_raises():
    # Direct unit call: bad DSL yields diagnostics, not an exception.
    p = compile_payload("total garbage that is not dsl")
    assert p["ok"] is False
    assert "svg" not in p
    good = compile_payload(CLEAN)
    assert good["ok"] is True and good["scene"]["nodes"]


def test_compile_payload_carries_edit_overlay_arrays():
    # Tier 5: the payload gains compact rooms/openings arrays for the edit overlay.
    p = compile_payload(CLEAN)
    assert p["rooms"] and all({"id", "x", "y", "w", "l", "level", "color"} <= set(r) for r in p["rooms"])
    assert p["openings"] and all({"kind", "key", "offset", "ax", "by"} <= set(o) for o in p["openings"])
    assert p["levels"] == sorted(set(p["levels"]))


# --- the edit endpoint (Tier 5, direct manipulation) -------------------------


def _edit(srv, source, edit):
    status, data = _request(
        srv, "POST", "/api/edit", json.dumps({"source": source, "edit": edit})
    )
    return status, json.loads(data)


def test_edit_happy_path_changes_exactly_one_line_and_recompiles(server):
    p = compile_payload(CLEAN)
    room = p["rooms"][0]
    status, res = _edit(
        server, CLEAN,
        {"kind": "move_room", "room": room["id"], "x": room["x"] + 2, "y": room["y"]},
    )
    assert status == 200
    assert res["changed"] is True and res["line"]
    assert res["source"] != CLEAN
    before, after = CLEAN.split("\n"), res["source"].split("\n")
    assert len(before) == len(after)
    changed = [i for i in range(len(before)) if before[i] != after[i]]
    assert changed == [res["line"] - 1]
    # the response is a full recompiled payload (viewport updates from it)
    assert "svg" in res and res["rooms"]


def test_edit_unknown_room_is_200_with_typed_error(server):
    status, res = _edit(server, CLEAN, {"kind": "move_room", "room": "nope", "x": 1, "y": 1})
    assert status == 200
    assert res["error"]["kind"] == "unknown_room"
    assert "svg" not in res  # no recompiled payload on a refused edit


def test_edit_malformed_kind_is_200_with_typed_error(server):
    status, res = _edit(server, CLEAN, {"kind": "explode"})
    assert status == 200
    assert res["error"]["kind"] == "malformed"


def test_edit_missing_edit_field_is_400(server):
    status, _ = _request(server, "POST", "/api/edit", json.dumps({"source": CLEAN}))
    assert status == 400


def test_edit_malformed_json_is_400(server):
    status, _ = _request(server, "POST", "/api/edit", "{not json")
    assert status == 400


# --- the edit-mode SPA markup ------------------------------------------------


def test_app_contains_edit_mode_markup_and_no_external_refs():
    html = render_app(CLEAN)
    for token in ("id=\"edit-mode\"", "id=\"edit-layer\"", "id=\"undo-btn\"",
                  "function buildOverlay(", "function applyEdits(", "move_opening"):
        assert token in html, token
    # still no external network references (the offline guarantee holds)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html


# --- the export endpoint -----------------------------------------------------


def _export(srv, source, fmt):
    return _request_full(
        srv, "POST", "/api/export", json.dumps({"source": source, "format": fmt})
    )


def test_export_svg_returns_svg_with_attachment(server):
    status, hdrs, data = _export(server, CLEAN, "svg")
    assert status == 200
    assert data.lstrip().startswith(b"<svg")
    assert hdrs["Content-Type"].startswith("image/svg+xml")
    assert "attachment; filename=" in hdrs["Content-Disposition"]
    assert hdrs["Content-Disposition"].endswith('.svg"')


def test_export_dxf_returns_dxf_header(server):
    status, hdrs, data = _export(server, CLEAN, "dxf")
    assert status == 200
    # DXF R12 opens with a SECTION/HEADER group (group code 0, value SECTION).
    assert data.startswith(b"0\nSECTION")
    assert b"AC1009" in data or b"HEADER" in data
    assert hdrs["Content-Disposition"].endswith('.dxf"')


def test_export_glb_streams_binary_magic(server):
    status, hdrs, data = _export(server, CLEAN, "glb")
    assert status == 200
    assert data[:4] == b"glTF"          # glTF binary container magic
    assert hdrs["Content-Type"] == "model/gltf-binary"
    assert hdrs["Content-Disposition"].endswith('.glb"')
    # bytes, not JSON: the body is not decodable/parseable as a JSON object
    with pytest.raises(UnicodeDecodeError):
        data.decode("ascii")


def test_export_ifc_returns_step_file(server):
    status, hdrs, data = _export(server, CLEAN, "ifc")
    assert status == 200
    assert data.startswith(b"ISO-10303-21")
    assert hdrs["Content-Disposition"].endswith('.ifc"')


def test_export_viewer_returns_self_contained_html(server):
    status, hdrs, data = _export(server, CLEAN, "viewer")
    assert status == 200
    html = data.decode("utf-8")
    assert html.lstrip().lower().startswith("<!doctype")
    assert "function mountScene(" in html          # the shared inline renderer
    assert "http://" not in html and "https://" not in html
    assert hdrs["Content-Type"].startswith("text/html")
    assert hdrs["Content-Disposition"].endswith('.html"')


def test_export_filename_derives_from_plan_name(server):
    status, hdrs, _ = _export(server, CLEAN, "svg")
    # cedar_ridge's plan name slugs into the download filename.
    assert 'filename="cedar' in hdrs["Content-Disposition"].lower()


def test_export_erroring_source_is_typed_error_not_500(server):
    # A plan with a code error (renders, but ok=False) is refused with a typed
    # error and a normal 200 — never a 500 and never a half-built artifact.
    status, hdrs, data = _export(server, WITH_ERROR, "glb")
    assert status == 200
    body = json.loads(data)
    assert body["error"]["kind"] == "compile_error"
    assert "Content-Disposition" not in hdrs


def test_export_parse_recovered_source_is_typed_error(server):
    status, _, data = _export(server, 'plan "x"\nenvelope not a number\n', "svg")
    assert status == 200
    assert json.loads(data)["error"]["kind"] == "compile_error"


def test_export_unknown_format_is_400(server):
    status, _, data = _export(server, CLEAN, "png")
    assert status == 400
    assert json.loads(data)["error"]


def test_export_malformed_json_is_400(server):
    status, _, _ = _request_full(server, "POST", "/api/export", "{not json")
    assert status == 400


def test_export_missing_format_is_400(server):
    status, _, _ = _request_full(server, "POST", "/api/export", json.dumps({"source": CLEAN}))
    assert status == 400


def test_export_oversize_body_is_400(server):
    status, _, _ = _request_full(server, "POST", "/api/export", "x" * (MAX_BODY + 1000))
    assert status == 400


# --- the workspace SPA markup (autosave / open / save / new / export) ---------


def test_app_contains_workspace_controls_and_localstorage_keys():
    html = render_app(CLEAN)
    for token in ("id=\"export-btn\"", "id=\"export-menu\"", "id=\"open-btn\"",
                  "id=\"save-btn\"", "id=\"new-btn\"", "id=\"file-input\"",
                  "id=\"notice\"", "function doExport(", "function autosave(",
                  "beforeunload", "data-fmt=\"viewer\"", "data-fmt=\"glb\""):
        assert token in html, token
    # the autosave/restore localStorage keys are referenced by the SPA
    assert "barndsl.playground.source" in html
    assert "barndsl.playground.savedAt" in html
    # still no external network references (the offline guarantee holds)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html


def test_app_from_file_flag_flows_into_the_spa():
    # The FILE-argument flag reaches the SPA so restore can prefer the file.
    assert "const INITIAL_FROM_FILE = true;" in render_app(CLEAN, from_file=True)
    assert "const INITIAL_FROM_FILE = false;" in render_app(CLEAN, from_file=False)
