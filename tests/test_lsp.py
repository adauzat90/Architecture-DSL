"""The stdlib Language Server (``barndsl lsp``) — Phase 8.

Framing round-trips (split reads, unicode, bad-JSON recovery), the lifecycle
dispatch, every pure feature function on fixtures (diagnostics mapping incl.
accepted→Hint and part relatedInformation; hover cards; each completion context;
definition targets incl. cross-file; the symbols tree; quick-fix + accept code
actions; formatting parity with fmt; rename parity with the edit engine), the
quick-fix/JS parity pin, and one end-to-end subprocess wire test (timeout-bounded).
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from barndsl import lsp
from barndsl.lsp import (
    Server,
    code_actions,
    completions,
    definition,
    diagnostics,
    formatting,
    hover,
    path_to_uri,
    prepare_rename,
    quickfix_snippet,
    read_message,
    rename,
    server_capabilities,
    symbols,
    uri_to_path,
    word_at,
    write_message,
)

REPO = Path(__file__).resolve().parent.parent
COMPOSED = REPO / "examples" / "composed"
COMPOSED_FILE = COMPOSED / "cedar_ridge.barn"


# --- helpers -----------------------------------------------------------------


class ByteStream:
    """A read-only binary stream over fixed bytes, handing back at most
    ``chunk`` bytes per ``read`` — ``chunk=1`` feeds byte-by-byte to prove the
    framer survives arbitrarily split reads."""

    def __init__(self, data: bytes, chunk: int = 1 << 20):
        self.data = data
        self.pos = 0
        self.chunk = chunk

    def read(self, n: int) -> bytes:
        take = min(n, self.chunk, len(self.data) - self.pos)
        out = self.data[self.pos : self.pos + take]
        self.pos += take
        return out


def _frame(obj: dict) -> bytes:
    buf = io.BytesIO()
    write_message(buf, obj)
    return buf.getvalue()


def _compile(uri: str, text: str):
    return lsp.compile_document(text, lsp.base_dir_for(uri))


def _find_line(text: str, needle: str) -> tuple[int, int]:
    for i, line in enumerate(text.split("\n")):
        if needle in line:
            return i, line.index(needle)
    raise AssertionError(f"{needle!r} not found")


# --- framing -----------------------------------------------------------------


def test_write_then_read_round_trips():
    obj = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"a": [1, 2, 3]}}
    got = read_message(ByteStream(_frame(obj)))
    assert got == obj


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13])
def test_read_survives_split_reads(chunk):
    obj = {"jsonrpc": "2.0", "id": 42, "method": "x", "params": {"k": "v" * 100}}
    got = read_message(ByteStream(_frame(obj), chunk=chunk))
    assert got == obj


def test_read_handles_unicode_prime_glyphs():
    obj = {"jsonrpc": "2.0", "id": 1, "params": {"text": "size 12′6″ x 10′"}}
    data = _frame(obj)
    # The Content-Length is the byte count, not the character count.
    assert b"Content-Length: " in data
    got = read_message(ByteStream(data, chunk=1))
    assert got["params"]["text"] == "size 12′6″ x 10′"


def test_read_clean_eof_returns_none():
    assert read_message(ByteStream(b"")) is None


def test_read_bad_json_raises_protocol_error_not_crash():
    body = b"{not json"
    frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
    with pytest.raises(lsp.ProtocolError):
        read_message(ByteStream(frame))


def test_read_missing_content_length_raises_protocol_error():
    frame = b"X-Other: 1\r\n\r\n{}"
    with pytest.raises(lsp.ProtocolError):
        read_message(ByteStream(frame))


def test_read_two_messages_in_one_buffer():
    a = {"jsonrpc": "2.0", "id": 1}
    b = {"jsonrpc": "2.0", "id": 2}
    stream = ByteStream(_frame(a) + _frame(b))
    assert read_message(stream) == a
    assert read_message(stream) == b


def test_uri_path_round_trip():
    p = "/home/user/plan with space.barn"
    assert uri_to_path(path_to_uri(p)) == p
    assert uri_to_path("untitled:Untitled-1") is None


# --- lifecycle ---------------------------------------------------------------


def _run_server(messages: list[dict]) -> list[dict]:
    """Drive a Server over an in-memory transport with a scripted message list;
    return every framed reply/notification it wrote."""
    inp = io.BytesIO(b"".join(_frame(m) for m in messages))
    out = io.BytesIO()
    Server(inp, out).run()
    out.seek(0)
    replies = []
    while True:
        msg = read_message(out)
        if msg is None:
            break
        replies.append(msg)
    return replies


def test_initialize_advertises_capabilities():
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    init = next(r for r in replies if r.get("id") == 1)
    assert init["result"]["capabilities"] == server_capabilities()


def test_shutdown_then_exit_is_clean_exit_code():
    inp = io.BytesIO(_frame({"jsonrpc": "2.0", "id": 1, "method": "shutdown"})
                     + _frame({"jsonrpc": "2.0", "method": "exit"}))
    out = io.BytesIO()
    assert Server(inp, out).run() == 0


def test_exit_without_shutdown_is_nonzero():
    inp = io.BytesIO(_frame({"jsonrpc": "2.0", "method": "exit"}))
    assert Server(inp, io.BytesIO()).run() == 1


def test_unknown_request_returns_method_not_found():
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 7, "method": "textDocument/nonesuch", "params": {}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    err = next(r for r in replies if r.get("id") == 7)
    assert err["error"]["code"] == lsp.METHOD_NOT_FOUND


def test_unknown_notification_is_ignored():
    # A bare notification (no id) for an unknown method must produce no reply.
    replies = _run_server([
        {"jsonrpc": "2.0", "method": "telemetry/event", "params": {}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    assert replies == []


def test_cancel_request_is_ignored():
    replies = _run_server([
        {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 1}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    assert replies == []


def test_malformed_frame_does_not_kill_server():
    good = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    bad_body = b"{oops"
    stream = (b"Content-Length: %d\r\n\r\n%s" % (len(bad_body), bad_body)
              + _frame(good)
              + _frame({"jsonrpc": "2.0", "method": "exit"}))
    out = io.BytesIO()
    Server(io.BytesIO(stream), out).run()
    out.seek(0)
    ids = []
    while (m := read_message(out)) is not None:
        if m.get("id") is not None:
            ids.append(m["id"])
    assert 1 in ids  # the good message after the bad frame was still served


def test_bad_content_length_resyncs_to_next_frame():
    """A frame with a non-integer Content-Length must not brick the stream: the
    reader resyncs to the following valid frame instead of feeding its body into
    every subsequent header. The exact 'brick' scenario — a corrupt frame, then
    valid shutdown/exit — the server must still answer."""
    stream = (b"Content-Length: notanumber\r\n\r\n"
              + _frame({"jsonrpc": "2.0", "id": 9, "method": "shutdown"})
              + _frame({"jsonrpc": "2.0", "method": "exit"}))
    out = io.BytesIO()
    code = Server(io.BytesIO(stream), out).run()
    assert code == 0  # clean shutdown → exit reached after recovery
    out.seek(0)
    ids = []
    while (m := read_message(out)) is not None:
        if m.get("id") is not None:
            ids.append(m["id"])
    assert 9 in ids  # the shutdown request after the corrupt frame was answered


def test_resync_recovers_when_bad_frame_carries_a_body():
    """The body-prepend case: the corrupt frame has a body, and frames are
    concatenated with no separator, so the next header is mid-buffer. Resync
    still finds it byte-for-byte."""
    good = {"jsonrpc": "2.0", "id": 3, "method": "shutdown"}
    stream = (b"Content-Type: x\r\n\r\n{\"stray\":\"body\"}"
              + _frame(good)
              + _frame({"jsonrpc": "2.0", "method": "exit"}))
    st = ByteStream(stream)
    first = read_message(st)
    assert first == good  # resynced straight to the next valid frame
    assert read_message(st) == {"jsonrpc": "2.0", "method": "exit"}


def test_garbage_bytes_before_a_frame_recover():
    """Leading garbage with no recoverable header, then a clean frame: the
    garbage frame resyncs onto the good one."""
    good = {"jsonrpc": "2.0", "id": 5}
    st = ByteStream(b"Nonsense: yes\r\n\r\nzzzz" + _frame(good))
    assert read_message(st) == good


def test_resync_unrecoverable_raises_then_clean_eof():
    # No Content-Length anywhere → resync exhausts at EOF and raises (existing
    # missing-header contract), and a plain empty stream is still a clean None.
    with pytest.raises(lsp.ProtocolError):
        read_message(ByteStream(b"X-Other: 1\r\n\r\n{}"))
    assert read_message(ByteStream(b"")) is None


# --- diagnostics (§3.1) ------------------------------------------------------


def _full_range(text: str) -> dict:
    lines = text.split("\n")
    return {"start": {"line": 0, "character": 0},
            "end": {"line": len(lines), "character": 0}}


def test_diagnostics_severity_and_range_mapping():
    # A room-level warning carries a column-accurate line (back-filled from the
    # room statement): NAT_LIGHT fires on line 4 (0-based 3), col 6 (0-based 5).
    text = 'plan "P"\nenvelope 20 x 20\nceiling 9\nroom bed: bedroom at 0,0 size 14 x 11\n'
    uri = "untitled:x"
    res = _compile(uri, text)
    ds = diagnostics(res, uri)
    nat = next(d for d in ds if d["code"] == "NAT_LIGHT")
    assert nat["severity"] == 2  # warning
    assert nat["source"] == "barndsl"
    assert nat["range"]["start"] == {"line": 3, "character": 5}
    assert "\n" in nat["message"]  # hint appended after a newline
    # A plan-level issue with no line anchors at line 0.
    ceil_text = 'plan "P"\nenvelope 20 x 20\nceiling 4\n'
    ceil = next(d for d in diagnostics(_compile(uri, ceil_text), uri) if d["code"] == "CEILING")
    assert ceil["severity"] == 1 and ceil["range"]["start"]["line"] == 0


def test_accepted_diagnostic_maps_to_hint_with_reason_in_message():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    ds = diagnostics(res, uri)
    hints = [d for d in ds if d["severity"] == 4]
    assert hints, "the composed example carries accepted (audited) deviations"
    assert any("accepted:" in d["message"] for d in hints)


def test_part_internal_diagnostic_carries_related_information(tmp_path):
    (tmp_path / "pod.barn").write_text("room lounge: office at 0,0 size 24 x 7\n")
    host = (
        'plan "H"\nenvelope 40 x 30\nceiling 9\n'
        'use "pod.barn" as p at 0,0\n'
        "room main: living at 0,10 size 20 x 20\n"
    )
    hp = tmp_path / "host.barn"
    hp.write_text(host)
    uri = path_to_uri(str(hp))
    res = _compile(uri, host)
    ds = diagnostics(res, uri)
    rel = [d for d in ds if "relatedInformation" in d]
    assert rel, "a part-internal finding should attach relatedInformation"
    loc = rel[0]["relatedInformation"][0]["location"]
    assert loc["uri"] == path_to_uri(str(tmp_path / "pod.barn"))


# --- hover (§3.2) ------------------------------------------------------------


def test_hover_on_statement_keyword_shows_grammar():
    text = 'plan "P"\nroom bed: bedroom at 0,0 size 12 x 11\n'
    res = _compile("untitled:x", text)
    h = hover(text, res, 1, 0)  # cursor on "room"
    assert "room <id>" in h["contents"]["value"]


def test_hover_on_room_id_shows_fact_card():
    text = 'plan "P"\nenvelope 20 x 20\nceiling 9\nroom bed: bedroom at 0,0 size 14 x 11\n'
    res = _compile("untitled:x", text)
    line, col = _find_line(text, "bed:")
    h = hover(text, res, line, col)
    val = h["contents"]["value"]
    assert "bedroom" in val and "sq ft" in val and "level 0" in val


def test_hover_on_stamped_id_names_its_part():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    line, col = _find_line(text, "m.bed")
    h = hover(text, res, line, col)
    assert "stamped from" in h["contents"]["value"]


def test_hover_on_instance_alias_shows_transform_and_bbox():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    line, raw = _find_line(text, "as b1")
    col = raw + text.split("\n")[line][raw:].index("b1")
    h = hover(text, res, line, col)
    val = h["contents"]["value"]
    assert "instance of" in val and "mirror y" in val and "bbox" in val


def test_hover_on_accept_pragma_code_explains_it():
    text = 'plan "P"\nroom hall: hallway at 0,0 size 4 x 20   # barndsl: accept HALL_DEADEND "spine"\n'
    res = _compile("untitled:x", text)
    line, col = _find_line(text, "HALL_DEADEND")
    h = hover(text, res, line, col)
    assert "HALL_DEADEND" in h["contents"]["value"]


# --- completion (§3.3) -------------------------------------------------------


def _labels(items):
    return {it["label"] for it in items}


def test_completion_line_start_lists_statements():
    text = "\n"
    items = completions(text, 0, 0, _compile("untitled:x", text), None)
    assert {"room", "door", "window", "envelope"} <= _labels(items)
    assert all(it["kind"] == lsp._KIND_KEYWORD for it in items)


def test_completion_room_type_slot():
    text = "room bed: "
    items = completions(text, 0, len(text), _compile("untitled:x", text), None)
    assert "bedroom" in _labels(items) and "kitchen" in _labels(items)


def test_completion_room_id_slot_after_door():
    text = 'plan "P"\nenvelope 30 x 20\nroom a: living at 0,0 size 15 x 20\nroom b: kitchen at 15,0 size 15 x 20\ndoor '
    res = _compile("untitled:x", text)
    items = completions(text, 4, len("door "), res, None)
    assert {"a", "b"} <= _labels(items)


def test_completion_includes_stamped_ids_in_id_slot():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    items = completions("door ", 0, len("door "), res, None)
    assert any(lbl.startswith("m.") for lbl in _labels(items))


def test_completion_fixture_kinds():
    text = "fixture "
    items = completions(text, 0, len(text), _compile("untitled:x", text), None)
    labels = _labels(items)
    assert "bed_queen" in labels or "sofa" in labels


def test_completion_alarm_kinds():
    text = "alarm "
    items = completions(text, 0, len(text), _compile("untitled:x", text), None)
    assert _labels(items) == {"smoke", "co", "smoke_co"}


def test_completion_wall_direction_slot():
    text = "window great "
    items = completions(text, 0, len(text), _compile("untitled:x", text), None)
    assert _labels(items) == {"north", "south", "east", "west"}


def test_completion_use_string_lists_part_paths():
    text = 'use "'
    items = completions(text, 0, len(text), _compile("x", text), str(COMPOSED))
    labels = _labels(items)
    assert any(lbl.endswith(".barn") for lbl in labels)
    assert all(it["kind"] == lsp._KIND_FILE for it in items)


def test_completion_accept_pragma_lists_codes():
    text = "room x: bedroom at 0,0 size 12 x 11  # barndsl: accept "
    items = completions(text, 0, len(text), _compile("untitled:x", text), None)
    assert "BEDROOM_AREA" in _labels(items)


# --- formatting (§3.4) -------------------------------------------------------


def test_formatting_matches_fmt():
    from barndsl.fmt import format_source

    text = "plan   \"P\"\nENVELOPE   20 x 20\nceiling 12-6\n"
    edits = formatting(text)
    assert len(edits) == 1
    assert edits[0]["newText"] == format_source(text)


def test_formatting_refuses_parse_errors():
    text = 'plan "P"\nroom :: broken\n'  # a line that won't parse
    assert formatting(text) == []


def test_formatting_no_edit_when_already_formatted():
    from barndsl.fmt import format_source

    text = format_source('plan "P"\nenvelope 20 x 20\nceiling 9\nroom a: living at 0,0 size 20 x 20\n')
    assert formatting(text) == []


# --- definition (§3.5) -------------------------------------------------------


def test_definition_local_room_id():
    text = 'plan "P"\nenvelope 30 x 20\nroom a: living at 0,0 size 15 x 20\nroom b: kitchen at 15,0 size 15 x 20\ndoor a - b cased width 4\n'
    uri = "untitled:x"
    res = _compile(uri, text)
    line, col = _find_line(text, "door a")
    col = text.split("\n")[line].index(" a ") + 1
    locs = definition(text, res, line, col, uri, None)
    assert locs == [{"uri": uri, "range": lsp._range(2, 0, 0)}]


def test_definition_stamped_id_returns_two_locations():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    line, col = _find_line(text, "door great - m.bed")
    col = text.split("\n")[line].index("m.bed")
    locs = definition(text, res, line, col, uri, lsp.base_dir_for(uri))
    assert len(locs) == 2
    targets = {loc["uri"] for loc in locs}
    assert uri in targets  # the use line in the host
    assert any(t.endswith("master_suite.barn") for t in targets)  # the part file


def test_definition_use_path_points_to_part_file():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    line, raw = _find_line(text, 'use "parts/master_suite.barn"')
    col = text.split("\n")[line].index("master_suite")
    locs = definition(text, res, line, col, uri, lsp.base_dir_for(uri))
    assert locs and locs[0]["uri"].endswith("master_suite.barn")


# --- document symbols (§3.6) -------------------------------------------------


def test_symbols_tree_shape():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    syms = symbols(res)
    assert len(syms) == 1
    root = syms[0]
    assert root["name"] == res.plan.name
    names = {c["name"] for c in root["children"]}
    assert "great" in names  # a host room
    assert any(n.startswith("▣ ") for n in names)  # an instance node
    inst = next(c for c in root["children"] if c["name"].startswith("▣ "))
    assert inst["children"]  # stamped members nested under it


# --- code actions (§3.7) -----------------------------------------------------


def test_code_action_offers_snippet_insert_and_accept():
    # A plan with a warning that carries a paste-able hint AND is acceptable.
    text = 'plan "P"\nenvelope 20 x 20\nceiling 9\nroom bed: bedroom at 0,0 size 12 x 11\n'
    uri = "untitled:x"
    res = _compile(uri, text)
    actions = code_actions(text, res, uri, _full_range(text))
    titles = [a["title"] for a in actions]
    assert any(t.startswith("Accept ") for t in titles)
    for a in actions:
        assert a["kind"] == "quickfix"
        assert uri in a["edit"]["changes"]


def test_code_action_accept_appends_pragma_to_line():
    text = 'plan "P"\nenvelope 20 x 20\nceiling 9\nroom bed: bedroom at 0,0 size 14 x 11\n'
    uri = "untitled:x"
    res = _compile(uri, text)
    actions = code_actions(text, res, uri, _full_range(text))
    accept = next(a for a in actions if a["title"].startswith("Accept "))
    edit = accept["edit"]["changes"][uri][0]
    assert "# barndsl: accept " in edit["newText"]


def test_code_action_errors_get_no_accept():
    text = 'plan "P"\nenvelope 20 x 20\nceiling 4\n'  # CEILING error
    uri = "untitled:x"
    res = _compile(uri, text)
    actions = code_actions(text, res, uri, _full_range(text))
    assert not any(a["title"] == "Accept CEILING (audited deviation)" for a in actions)


# --- quick-fix parity with the playground JS heuristic (§3.7) ----------------

#: (hint, expected snippet-or-None) — the shared fixtures both the Python
#: ``quickfix_snippet`` and the playground ``quickFixSnippet`` JS must agree on.
_QUICKFIX_FIXTURES = [
    ("Add `alarm smoke in bed`.", "alarm smoke in bed"),
    # A genuine "add the missing X" fix leads with a colon (not `e.g.`) so it is
    # offered — the reworded ENVELOPE/EMPTY hints look like this.
    ("Declare the footprint: `envelope 60 x 40`.", "envelope 60 x 40"),
    ("Add rooms: `room living: living at 0,0 size 20 x 16`.",
     "room living: living at 0,0 size 20 x 16"),
    ("Set `ceiling 9` or greater (9–12 is typical).", "ceiling 9"),
    ("Use positive feet, e.g. `size 12 x 10`.", None),   # `size` is a modifier, not a head
    ("Declare a real lot: `site <W> x <L>`.", None),     # carries placeholders
    ("Give each room a unique id.", None),               # no backticked snippet
    # An example lead-in (`e.g.`/`for example`/`like`) marks an *illustration* of
    # syntax for a malformed line, not a droppable fix — never offered.
    ('Add the closing quote, e.g. `plan "Name"`.', None),   # UNTERMINATED_STRING
    ("The program starts with a bedroom count, e.g. `program 3 bed`.", None),
    ("Use positive feet, for example `wing 20 x 24 at 40,0`.", None),
    ("Concentrate glass, like `roof gable`.", None),
]


def test_quickfix_snippet_fixtures():
    for hint, expected in _QUICKFIX_FIXTURES:
        assert quickfix_snippet(hint) == expected, hint


def test_quickfix_parity_with_playground_js():
    """Pin the Python heuristic against a faithful re-derivation of the JS
    ``quickFixSnippet`` (same backtick scan, same placeholder regex, same
    statement-head set the playground injects as ``HIGHLIGHT.statements``)."""
    import re

    from barndsl.playground import _STATEMENT_KEYWORDS

    stmt = set(_STATEMENT_KEYWORDS)
    placeholder = re.compile(r"\.\.\.|…|[<>]")
    example_lead = re.compile(r"(?:e\.g\.|for example|like)[\s,]*$", re.IGNORECASE)

    def js_quickfix(hint):
        if not hint:
            return None
        for m in re.finditer(r"`([^`]+)`", hint):
            snip = m.group(1).strip()
            head = (snip.split() or [""])[0].lower()
            if head not in stmt or placeholder.search(snip):
                continue
            if example_lead.search(hint[: m.start()]):
                continue
            return snip
        return None

    for hint, _expected in _QUICKFIX_FIXTURES:
        assert quickfix_snippet(hint) == js_quickfix(hint)


def test_playground_js_still_defines_the_heuristic():
    # The parity claim is only meaningful if the JS function still exists.
    from barndsl.playground import _APP_HTML

    assert "function quickFixSnippet(hint)" in _APP_HTML
    assert "QUICKFIX_PLACEHOLDER = /\\.\\.\\.|…|[<>]/" in _APP_HTML
    assert "QUICKFIX_EXAMPLE_LEAD = /(?:e\\.g\\.|for example|like)[\\s,]*$/i" in _APP_HTML


def test_unterminated_string_offers_no_quickfix():
    """Regression: an unterminated-string error illustrates the closed-quote
    syntax with ``e.g. `plan "Name"`` — that is NOT an insertable fix (inserting
    it leaves the real error), so no Apply/code-action is offered. Pinned against
    the REAL compiled hint, in both the Python and JS heuristics."""
    from barndsl.compiler import compile_source

    res = compile_source('plan "Untied\n')
    stringy = [d for d in res.diagnostics if d.code == "UNTERMINATED_STRING"]
    assert stringy, "expected an UNTERMINATED_STRING diagnostic"
    for d in stringy:
        assert quickfix_snippet(d.hint) is None, d.hint


# --- rename (§3.8) -----------------------------------------------------------


def test_prepare_rename_accepts_room_rejects_stamped():
    uri = path_to_uri(str(COMPOSED_FILE))
    text = COMPOSED_FILE.read_text()
    res = _compile(uri, text)
    line, col = _find_line(text, "room great:")
    col = text.split("\n")[line].index("great")
    pr = prepare_rename(text, res, line, col)
    assert pr["placeholder"] == "great"
    line2, col2 = _find_line(text, "door great - m.bed")
    col2 = text.split("\n")[line2].index("m.bed")
    with pytest.raises(lsp._RenameRejected):
        prepare_rename(text, res, line2, col2)


def test_rename_matches_edit_engine():
    from barndsl.edits import Edit, apply_edit

    text = 'plan "P"\nenvelope 30 x 20\nceiling 9\nroom a: living at 0,0 size 15 x 20\nroom b: kitchen at 15,0 size 15 x 20\ndoor a - b cased width 4\n'
    uri = "untitled:x"
    res = _compile(uri, text)
    line, _ = _find_line(text, "room a:")
    col = text.split("\n")[line].index("a")
    wedit = rename(text, res, line, col, "lounge", uri, None)
    new_source = wedit["changes"][uri][0]["newText"]
    expected = apply_edit(text, Edit("rename_room", room="a", to="lounge")).source
    assert new_source == expected
    assert "room lounge:" in new_source
    assert "door lounge - b" in new_source


def test_rename_returns_none_off_a_room():
    text = 'plan "P"\nenvelope 20 x 20\nceiling 9\nroom a: living at 0,0 size 20 x 20\n'
    res = _compile("untitled:x", text)
    assert rename(text, res, 0, 0, "x", "untitled:x", None) is None  # cursor on `plan`


# --- word extraction ---------------------------------------------------------


def test_word_at_handles_dotted_and_dashed_ids():
    assert word_at("door great - m.bed", 15)[0] == "m.bed"
    assert word_at("room bed_2: bedroom", 6)[0] == "bed_2"


# --- server request round-trips (in-memory) ----------------------------------


def test_server_publishes_diagnostics_on_open():
    open_msg = {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": "untitled:x", "languageId": "barndsl", "version": 1,
            "text": 'plan "P"\nenvelope 20 x 20\nceiling 4\n',
        }},
    }
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        open_msg,
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    pub = next(r for r in replies if r.get("method") == "textDocument/publishDiagnostics")
    codes = {d["code"] for d in pub["params"]["diagnostics"]}
    assert "CEILING" in codes


def test_server_hover_request_round_trip():
    text = 'plan "P"\nenvelope 20 x 20\nceiling 9\nroom bed: bedroom at 0,0 size 14 x 11\n'
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen",
         "params": {"textDocument": {"uri": "untitled:x", "version": 1, "text": text}}},
        {"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
         "params": {"textDocument": {"uri": "untitled:x"}, "position": {"line": 3, "character": 5}}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    hov = next(r for r in replies if r.get("id") == 2)
    assert "bedroom" in hov["result"]["contents"]["value"]


def test_server_didchange_recompiles():
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen",
         "params": {"textDocument": {"uri": "untitled:x", "version": 1, "text": 'plan "P"\nceiling 9\n'}}},
        {"jsonrpc": "2.0", "method": "textDocument/didChange",
         "params": {"textDocument": {"uri": "untitled:x", "version": 2},
                    "contentChanges": [{"text": 'plan "P"\nceiling 4\n'}]}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    pubs = [r for r in replies if r.get("method") == "textDocument/publishDiagnostics"]
    last = pubs[-1]["params"]["diagnostics"]
    assert any(d["code"] == "CEILING" for d in last)


# --- end-to-end subprocess (proves the wire format) --------------------------


def test_end_to_end_subprocess():
    """Spawn the real ``barndsl lsp`` process, initialize, open a doc with a known
    warning, read publishDiagnostics, hover, then shut down — timeout-bounded so a
    hang can never wedge the suite."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "barndsl.cli", "lsp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    text = 'plan "P"\nenvelope 20 x 20\nceiling 4\nroom bed: bedroom at 0,0 size 14 x 11\n'
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen",
         "params": {"textDocument": {"uri": "untitled:x", "version": 1, "text": text}}},
        {"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
         "params": {"textDocument": {"uri": "untitled:x"}, "position": {"line": 2, "character": 0}}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}},
        {"jsonrpc": "2.0", "method": "exit"},
    ]
    payload = b"".join(_frame(m) for m in msgs)
    try:
        out, _err = proc.communicate(payload, timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("barndsl lsp did not exit within the timeout")
    assert proc.returncode == 0
    stream = ByteStream(out)
    replies = []
    while (m := read_message(stream)) is not None:
        replies.append(m)
    # initialize reply, a publishDiagnostics with the CEILING warning, hover reply
    assert any(r.get("id") == 1 and "capabilities" in r.get("result", {}) for r in replies)
    pubs = [r for r in replies if r.get("method") == "textDocument/publishDiagnostics"]
    assert pubs and any(d["code"] == "CEILING" for d in pubs[-1]["params"]["diagnostics"])
    hov = next(r for r in replies if r.get("id") == 2)
    assert hov["result"] is not None


# -- part-file diagnostics: mirror a host's part-internal findings onto an
#    open part file (Phase 8 deferral, now shipped) ---------------------------


def _part_scenario(tmp_path):
    """A part with an odd-proportion office (a part-internal ROOM_PROPORTION) and
    a host that uses it. Returns (host_uri, part_uri, host_src)."""
    (tmp_path / "pod.barn").write_text("room lounge: office at 0,0 size 24 x 7\n")
    host = (
        'plan "H"\nenvelope 40 x 30\nceiling 9\n'
        'use "pod.barn" as p at 0,0\n'
        "room main: living at 0,10 size 20 x 20\n"
    )
    (tmp_path / "host.barn").write_text(host)
    return (
        path_to_uri(str(tmp_path / "host.barn")),
        path_to_uri(str(tmp_path / "pod.barn")),
        host,
    )


def _publishes_to(replies, uri):
    """Every publishDiagnostics for ``uri``, in order, as diagnostic lists."""
    return [
        r["params"]["diagnostics"]
        for r in replies
        if r.get("method") == "textDocument/publishDiagnostics"
        and r["params"]["uri"] == uri
    ]


def test_open_part_gets_host_part_internal_diagnostics_then_cleared(tmp_path):
    host_uri, part_uri, host = _part_scenario(tmp_path)
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": part_uri,
                             "text": (tmp_path / "pod.barn").read_text(), "version": 1}}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": host_uri, "text": host, "version": 1}}},
        {"jsonrpc": "2.0", "method": "textDocument/didClose", "params": {
            "textDocument": {"uri": part_uri}}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])

    part_pubs = _publishes_to(replies, part_uri)
    # At some point the part received the host's in-context finding, mapped to the
    # part's OWN first line (0-based line 0), not the host's `use` line.
    host_derived = [
        d for pub in part_pubs for d in pub
        if d["code"] == "ROOM_PROPORTION"
    ]
    assert host_derived, "the open part should receive the host's part-internal finding"
    assert host_derived[0]["range"]["start"]["line"] == 0
    # The host-facing "in part …" prefix is stripped on the part's own file.
    assert "in part" not in host_derived[0]["message"]
    # Closing the part clears its diagnostics (last publish to it is empty).
    assert part_pubs[-1] == []


def test_never_publishes_to_a_part_that_is_not_open(tmp_path):
    host_uri, part_uri, host = _part_scenario(tmp_path)
    replies = _run_server([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        # Only the host is open — the part file is never opened by the editor.
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": host_uri, "text": host, "version": 1}}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    # Nothing is ever published to the un-opened part URI (the Phase 8 rule).
    assert _publishes_to(replies, part_uri) == []
