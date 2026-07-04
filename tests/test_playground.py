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

with open(os.path.join(EXAMPLES, "gallery", "two_story.barn"), encoding="utf-8") as _fh:
    #: A two-level plan (a loft on level 1, a stair) — pins the multi-level edit UI.
    TWO_STORY = _fh.read()

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


def test_compile_payload_scene_carries_the_walk_block():
    # First-person walk mode reads its collision/floor/stair/spawn data from the
    # scene JSON the playload ships, so it must ride along automatically.
    p = compile_payload(CLEAN)
    walk = p["scene"]["walk"]
    assert {"segments", "floors", "stairs", "spawn", "eyeHeight"} <= set(walk)


def test_app_wires_and_documents_walk_mode():
    # The SPA embeds the walk-mode entry points and its help panel documents it,
    # and leaving the 3D tab exits walk mode cleanly.
    html = render_app(CLEAN)
    assert "enterWalk" in html and "exitWalk" in html  # from the shared renderer
    assert "ctrl.exitWalk" in html                     # tab-switch cleanup hook
    assert "Walk mode" in html                          # help-panel THREE_TIPS line


def test_compile_payload_carries_edit_overlay_arrays():
    # Tier 5: the payload gains compact rooms/openings arrays for the edit overlay.
    p = compile_payload(CLEAN)
    assert p["rooms"] and all({"id", "x", "y", "w", "l", "level", "color"} <= set(r) for r in p["rooms"])
    assert p["openings"] and all({"kind", "key", "offset", "ax", "by"} <= set(o) for o in p["openings"])
    assert p["levels"] == sorted(set(p["levels"]))


def test_compile_payload_carries_stairs_for_the_multilevel_overlay():
    # The edit overlay draws each stair on both the level it runs from and the one
    # it lands on, so the payload carries compact stair footprints.
    p = compile_payload(TWO_STORY)
    assert p["levels"] == [0, 1]
    assert p["stairs"] and all(
        {"id", "x", "y", "w", "l", "from", "to"} <= set(s) for s in p["stairs"]
    )
    st = p["stairs"][0]
    assert (st["from"], st["to"]) == (0, 1)
    # a loft lives on level 1, so the overlay can filter to an upper floor
    assert any(r["level"] == 1 for r in p["rooms"])


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


def test_app_contains_level_switcher_markup_and_shortcut():
    html = render_app(CLEAN)
    for token in ('id="level-switch"', "function renderLevelSwitcher(",
                  "function setEditLevel(", "class=\"lvl-chip"):
        assert token in html, token
    # the floor-switch keyboard shortcut is documented in the help panel
    assert "Switch floor (edit mode)" in html
    # the old fixed "Editing level 0 of N" note is gone in favour of the switcher
    assert "Editing level 0 of" not in html
    # still no external network references (the offline guarantee holds)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html


def test_edit_upper_level_room_changes_only_its_line(server):
    # Moving the two_story loft (level 1) must rewrite exactly the loft's line and
    # leave every other byte — including the ground floor — untouched.
    p = compile_payload(TWO_STORY)
    loft = next(r for r in p["rooms"] if r["id"] == "loft")
    assert loft["level"] == 1
    status, res = _edit(
        server, TWO_STORY,
        {"kind": "move_room", "room": "loft", "x": loft["x"] + 2, "y": loft["y"]},
    )
    assert status == 200
    assert res["changed"] is True and res["line"]
    before, after = TWO_STORY.split("\n"), res["source"].split("\n")
    assert len(before) == len(after)
    changed = [i for i in range(len(before)) if before[i] != after[i]]
    assert changed == [res["line"] - 1]
    assert "loft" in after[res["line"] - 1] and "level 1" in after[res["line"] - 1]


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


# --- the report payload (Report tab data: cost / schedules / energy / areas) --

#: The clean plan with an IECC climate zone declared, to light up the energy
#: section (no bundled example declares one).
CLIMATE = CLEAN + "\nclimate 5\n"


def test_compile_payload_carries_report_block():
    rep = compile_payload(CLEAN)["report"]
    # cost estimate, reused verbatim from cost.estimate_cost (total + breakdown)
    assert rep["cost"]["total"]["expected"] > 0
    assert rep["cost"]["assemblies"] and rep["cost"]["disclaimer"]
    assert rep["cost"]["subtotals"] and rep["cost_per_sqft"] > 0
    # the three architect schedules, each shaped columns + count-consistent rows
    titles = {s["title"] for s in rep["schedules"]}
    assert {"Room Schedule", "Door Schedule", "Window Schedule"} <= titles
    for s in rep["schedules"]:
        assert s["columns"] and s["count"] == len(s["rows"])
        assert all(len(row) == len(s["columns"]) for row in s["rows"])
    # areas: one row per room with the required fields
    assert rep["areas"]["rooms"]
    assert all(
        {"name", "type", "level", "width", "length", "area"} <= set(r)
        for r in rep["areas"]["rooms"]
    )


def test_report_area_total_is_consistent_with_metrics():
    p = compile_payload(CLEAN)
    total = p["report"]["areas"]["total_area"]
    # the per-room areas sum to the plan's assigned area (metrics), within rounding
    assert abs(total - p["metrics"]["assigned_sqft"]) < 0.01


def test_report_energy_present_only_with_a_climate_zone():
    assert compile_payload(CLEAN)["report"]["energy"] is None
    energy = compile_payload(CLIMATE)["report"]["energy"]
    assert energy["zone"] == 5
    assert "R-" in energy["summary"]
    assert energy["targets"]["ceiling"].startswith("R-")


def test_report_absent_for_uncompilable_source():
    # An erroring / non-plan source carries no report block (and never crashes).
    assert "report" not in compile_payload("total garbage that is not dsl")


def test_report_data_returns_empty_for_no_plan_and_never_raises():
    from barndsl.compiler import compile_source
    from barndsl.playground import report_data

    assert report_data(compile_source("not dsl at all")) == {}
    good = report_data(compile_source(CLEAN))
    assert good["cost"]["total"]["expected"] > 0 and good["schedules"]


# --- the export `packet` format (print-ready permit packet) -------------------


def test_export_packet_returns_self_contained_html(server):
    status, hdrs, data = _export(server, CLEAN, "packet")
    assert status == 200
    html = data.decode("utf-8")
    assert html.lstrip().lower().startswith("<!doctype")
    # carries the plan title and the inlined plan SVG
    assert "cedar" in html.lower()
    assert "<svg" in html
    # self-contained: no external stylesheet/script/CDN. The only http:// is the
    # inlined-SVG XML namespace identifier (not a network fetch).
    assert "https://" not in html
    assert "<script src" not in html and "//cdn" not in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert hdrs["Content-Type"].startswith("text/html")
    assert hdrs["Content-Disposition"].endswith('-packet.html"')


def test_export_packet_erroring_source_is_typed_error(server):
    status, hdrs, data = _export(server, WITH_ERROR, "packet")
    assert status == 200
    assert json.loads(data)["error"]["kind"] == "compile_error"
    assert "Content-Disposition" not in hdrs


# --- the Report tab + Print packet SPA markup --------------------------------


def test_app_contains_report_and_print_markup_and_no_external_refs():
    html = render_app(CLEAN)
    for token in ('data-tab="report"', 'id="print-btn"', 'id="report-wrap"',
                  'data-fmt="packet"', "function reportHTML(", "function buildPrintDoc(",
                  "function openPrint(", "@media print"):
        assert token in html, token
    # still no external network references (the offline guarantee holds)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html


# --- wave 3: viewport zoom, help, highlighting, score popover, dimensions -----


def test_app_contains_zoom_controls_and_fit_math():
    html = render_app(CLEAN)
    for token in ('id="plan-zoom"', 'id="plan-zpct"', 'data-z="fit"', 'data-z="in"',
                  'data-z="out"', "function makeZoom(", "planZoom.refit(",
                  "function planClickToSource(", "openLightbox("):
        assert token in html, token


def test_app_contains_help_panel_that_consumes_the_reference_endpoint():
    html = render_app(CLEAN)
    for token in ('id="help-btn"', 'id="help-panel"', 'id="help-search"',
                  "function loadReference(", "function renderReference("):
        assert token in html, token
    # the previously-dead /api/reference endpoint is now fetched by the app
    assert "'/api/reference'" in html or '"/api/reference"' in html
    # the keyboard-shortcuts list is present
    assert "Keyboard shortcuts" in html


def test_app_contains_syntax_highlight_layer_kept_in_sync():
    html = render_app(CLEAN)
    for token in ('id="hl"', 'aria-hidden="true"', "function renderHighlight(",
                  "function hlLine(", "const HL_KW", "const HL_TYPE"):
        assert token in html, token
    # the highlight vocabulary is derived from the compiler, not hardcoded blind:
    # statement heads and room-type names both reach the SPA
    assert '"room"' in html and '"envelope"' in html   # statement keywords
    assert '"bedroom"' in html and '"kitchen"' in html  # RoomType values


def test_app_contains_score_popover_and_dimension_readout():
    html = render_app(CLEAN)
    for token in ('id="score-pop"', "function renderScorePop(", "toggleScorePop(",
                  'id="dim-chip"', "function showDim(", "function neighborSnap("):
        assert token in html, token
    # the title-attr fallback on the score chip is kept
    assert 'id="score-chip"' in html


def test_highlight_tokens_come_from_the_real_sources():
    from barndsl.elements import RoomType
    from barndsl.playground import _highlight_tokens

    toks = _highlight_tokens()
    assert toks["types"] == [t.value for t in RoomType]
    # every statement head the highlighter knows is a real DSL keyword
    for kw in ("plan", "envelope", "room", "door", "window", "frame"):
        assert kw in toks["keywords"]


def test_plan_svg_rooms_are_clickable_for_source_linking():
    # render_svg tags each room rect with data-room so the plan links to source.
    from barndsl import compile_source
    from barndsl.render import render_svg

    plan = compile_source(CLEAN).plan
    assert plan is not None
    svg = render_svg(plan)
    assert 'data-room="' in svg
    # the payload carries the matching source line for each room
    p = compile_payload(CLEAN)
    assert all(r.get("line") for r in p["rooms"])


# --- wave 4: quick-fix, find/replace, autocomplete, comment, triage ----------


def test_highlight_tokens_carry_statements_and_fixtures():
    # The editor's autocomplete and the diagnostics quick-fix need statement heads
    # and fixture kinds split out of the merged highlight vocab — derived from the
    # same sources the parser uses, so the two can't drift.
    from barndsl.fixtures import FIXTURES
    from barndsl.playground import _STATEMENT_KEYWORDS, _highlight_tokens

    toks = _highlight_tokens()
    assert toks["statements"] == sorted(_STATEMENT_KEYWORDS)
    assert toks["fixtures"] == sorted(FIXTURES)
    # every statement head is also in the merged keyword set (colouring is unchanged)
    assert set(toks["statements"]) <= set(toks["keywords"])


def test_app_ships_the_statement_and_fixture_lists_to_the_page():
    html = render_app(CLEAN)
    # the split lists reach the SPA as JS sets it can branch on
    assert "const HL_STMT" in html and "const HL_FIX" in html
    assert "HIGHLIGHT.statements" in html and "HIGHLIGHT.fixtures" in html
    # a fixture kind and a statement head both round-trip into the page JSON
    assert '"toilet"' in html and '"fixture"' in html


def test_app_contains_diagnostic_quickfix_apply():
    html = render_app(CLEAN)
    for token in ("function quickFixSnippet(", "function applyQuickFix(",
                  "QUICKFIX_PLACEHOLDER", 'class="qfix"', 'data-qfix="'):
        assert token in html, token
    # the placeholder guard rejects the fill-in-the-blank hints (…/</>)
    assert "\\.\\.\\.|" in html or "QUICKFIX_PLACEHOLDER = /" in html


def test_app_contains_find_and_replace_bar():
    html = render_app(CLEAN)
    for token in ('id="find-bar"', 'id="find-input"', 'id="replace-input"',
                  'id="find-count"', "function openFind(", "function replaceAll(",
                  "function cycleFind(", "mark.find"):
        assert token in html, token
    # find + replace are taught in the keyboard-shortcuts list
    assert "Find in the editor" in html and "Find & replace" in html


def test_app_contains_autocomplete_popup_and_context():
    html = render_app(CLEAN)
    for token in ('id="ac-pop"', "function completionContext(", "function acceptAc(",
                  "function roomIds(", "function updateAutocomplete("):
        assert token in html, token
    # the Ctrl+Space completion shortcut is documented
    assert "Autocomplete" in html


def test_app_contains_comment_toggle():
    html = render_app(CLEAN)
    assert "function toggleComment(" in html
    assert "Toggle comment" in html


def test_app_contains_diagnostics_triage():
    html = render_app(CLEAN)
    for token in ("function sortedDiagnostics(", "data-filter=", "let diagFilter",
                  ".count.active"):
        assert token in html, token


def test_wave4_markup_keeps_the_offline_guarantee():
    # None of the new UI reaches for the network — the offline promise holds.
    html = render_app(CLEAN)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html


# --- wave 5: split panes, theme toggle, 3D snapshot, label declutter, tab keys -


def test_app_contains_resizable_split_handles_and_persistence_keys():
    html = render_app(CLEAN)
    for token in ('id="split-agent"', 'id="split-editor"', "class=\"split-h",
                  "function startSplit(", "function afterSplitResize(",
                  "function clampAgent(", "function clampEditor("):
        assert token in html, token
    # the two split widths persist under the namespaced localStorage keys
    assert "barndsl.playground.agentWidth" in html
    assert "barndsl.playground.editorWidth" in html
    # a drag tick re-fits the 2D plan and re-sizes the live 3D canvas
    assert "ctrl.resize()" in html and "planZoom.refit()" in html
    # a collapsed agent pane hides its handle in CSS
    assert ".agent.collapsed + .split-h" in html
    # handles are hidden in the print stylesheet
    assert ".split-h" in html


def test_app_theme_toggle_pins_both_palettes_and_color_scheme():
    html = render_app(CLEAN)
    assert 'id="theme-btn"' in html
    assert "function applyTheme(" in html and "function cycleTheme(" in html
    # the previously-inert data-theme hooks now carry the full palette, not just
    # the #hl token colours — assert --bg reaches the dark/light attribute blocks
    assert ':root[data-theme="dark"] { color-scheme:dark;' in html
    assert ':root[data-theme="light"] { color-scheme:light;' in html
    for block in ('[data-theme="dark"]', '[data-theme="light"]'):
        i = html.index(block + " { color-scheme")
        assert "--bg:" in html[i:i + 300], block
    # persisted, and taught in the shortcuts list
    assert "barndsl.playground.theme" in html
    assert "Cycle theme" in html


def test_app_theme_auto_is_untouched_default():
    # Auto mode leaves the OS media query in charge — that block still exists and
    # boot applies 'auto' when nothing is saved.
    html = render_app(CLEAN)
    assert "@media (prefers-color-scheme: dark)" in html
    assert "applyTheme(saved || 'auto')" in html


def test_app_contains_3d_snapshot_with_draw_before_read():
    html = render_app(CLEAN)
    assert 'id="snap-btn"' in html
    assert "function snapshot3d(" in html
    # PNG named from the plan slug, reusing the existing blob-download idiom
    assert "'-3d.png'" in html and "downloadBlob(" in html
    # the WebGL buffer has no preserveDrawingBuffer, so draw() must precede the
    # synchronous read in the same task — assert that ordering literally
    draw_at = html.index("ctrl.draw();")
    read_at = html.index("canvas.toDataURL('image/png')")
    assert draw_at < read_at


def test_app_contains_edit_overlay_label_fit_or_hide():
    html = render_app(CLEAN)
    assert "function labelFits(" in html
    # applied to fixtures and rooms with a <title> fallback for the hidden label
    assert "labelFits(fk," in html and "labelFits(r.id," in html
    assert "<title>" in html


def test_app_contains_viewport_tab_shortcuts():
    html = render_app(CLEAN)
    # keys 1–4 map to the four tabs, guarded by the same typing/modifier predicate
    assert "'1':'plan'" in html and "'2':'three'" in html
    assert "'3':'views'" in html and "'4':'report'" in html
    assert "Switch viewport tab" in html


def test_wave5_markup_keeps_the_offline_guarantee():
    # The split handles, theme toggle, snapshot and declutter add no network refs.
    html = render_app(CLEAN)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html


# --- wave 6: unified undo/redo history --------------------------------------


def test_app_ships_the_unified_history_api():
    # One linear timeline replaces the old gesture-only undoStack: the state model,
    # the depth cap, and the single applyEdit funnel are all present, and the old
    # names are gone.
    html = render_app(CLEAN)
    for token in ("let history = [], histIndex", "function histCommit(",
                  "function applyEdit(", "function histRestore(",
                  "function doUndo(", "function doRedo(", "function updateUndoRedo("):
        assert token in html, token
    # cap ~200 entries, dropping oldest
    assert "HIST_CAP = 200" in html
    assert "history.length > HIST_CAP" in html
    # the retired undo-only stack API is entirely gone
    assert "undoStack" not in html
    assert "function pushUndo(" not in html


def test_app_typing_coalesces_into_bursts():
    # A shadow mirror + a coalescing constant fold a run of keystrokes into one undo
    # unit, and composition must not fracture the burst.
    html = render_app(CLEAN)
    assert "COALESCE_MS = 700" in html
    assert "function recordTyping(" in html
    assert "histMirror" in html
    # IME/composition is tracked so it does not split a burst
    assert "compositionstart" in html and "let lastEditKind" in html


def test_app_has_redo_button_paired_with_undo():
    html = render_app(CLEAN)
    assert 'id="redo-btn"' in html and 'id="undo-btn"' in html
    # the redo glyph and both wirings are present
    assert "↷" in html
    assert "redoBtn.addEventListener('click', doRedo)" in html
    # tooltips name the change they would undo/redo
    assert "'Undo ' + history[histIndex].label" in html
    assert "'Redo ' + history[histIndex + 1].label" in html


def test_app_undo_redo_shortcuts_wired_and_documented():
    html = render_app(CLEAN)
    # both directions: Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z and Ctrl/Cmd+Y
    assert "function isUndoKey(" in html and "function isRedoKey(" in html
    assert "e.key === 'y' || e.key === 'Y'" in html
    # intercepted in the editor's own keydown so native textarea undo can't fight it
    assert "e.preventDefault(); e.stopPropagation(); doUndo();" in html
    assert "e.preventDefault(); e.stopPropagation(); doRedo();" in html
    # the help panel now documents Undo / Redo with both keys, not the old single entry
    assert "Undo / Redo" in html
    assert "Undo a layout edit" not in html
    # the find/replace and help-search fields keep their native per-field undo
    assert "el === findInput" in html and "el === helpSearch" in html


def test_app_writers_route_through_the_history_funnel():
    # The known programmatic writers no longer assign editor.value directly for their
    # edit — they go through applyEdit so the change is undoable and coalesces sanely.
    html = render_app(CLEAN)
    # quick-fix, autocomplete-accept, comment-toggle, replace-all and the drag/agent
    # pipelines each carry a labelled applyEdit call
    for label in ("'quick-fix'", "'autocomplete'", "'toggle comment'",
                  "'replace all'", "'layout edit'", "'agent design'", "'load example'"):
        assert "applyEdit(" in html and label in html, label
    # applyQuickFix hands its new text to applyEdit rather than setting editor.value
    assert "applyEdit(lines.join('\\n'), null, null, 'quick-fix')" in html
    # loading a document is itself undoable (setSource funnels through applyEdit)
    assert "function setSource(src, label)" in html
    assert "applyEdit(src, 0, 0, label" in html


def test_wave6_markup_keeps_the_offline_guarantee():
    html = render_app(CLEAN)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "<script src" not in html
