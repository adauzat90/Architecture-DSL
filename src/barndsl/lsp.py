"""``barndsl lsp`` — a stdlib Language Server for the barndsl DSL.

This brings the compiler-as-teacher experience to any LSP editor (VS Code,
Neovim, Helix, Zed) without the playground: live teaching diagnostics, hover
docs for every statement, id-aware completions, go-to-definition across ``use``
boundaries, format-on-save and the playground's quick-fixes. See
``docs/design/lsp.md`` for the design.

**Zero dependencies.** The wire protocol is JSON-RPC 2.0 over stdio with
``Content-Length`` framing (:func:`read_message` / :func:`write_message`); every
capability is a *pure function* taking ``(text, …)`` and returning plain dicts
shaped like LSP types, tested directly. The stdio loop only frames, dispatches
and replies.

**Position model.** LSP positions are 0-based ``(line, character)`` with the
character measured in UTF-16 code units. barndsl sources are overwhelmingly
ASCII; the only non-ASCII glyphs the language uses are the prime marks ``′``
(U+2032) and ``″`` (U+2033), both in the Basic Multilingual Plane, so one code
point is exactly one UTF-16 unit for every character that can appear. We
therefore index positions by Python string code points, which equals the UTF-16
offset for all in-language text. (A stray astral-plane character in a comment
would shift by one; that is documented and accepted.)
"""

from __future__ import annotations

import json
import os
import re
import select
import sys
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .compiler import DSL_REFERENCE, CompileResult, compile_source
from .compose import scan_parts
from .diagnostics import REGISTRY, explain
from .edits import Edit, apply_edit
from .elements import ALARM_KINDS, RoomType
from .fixtures import FIXTURES
from .fmt import format_source
from .playground import _STATEMENT_KEYWORDS
from .render import fmt_ft_in
from .validation import Issue, Severity

# --- JSON-RPC framing --------------------------------------------------------

#: JSON-RPC error codes we use (a small subset of the LSP/JSON-RPC table).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """A framing/decoding failure that the read loop logs and recovers from."""


def read_message(stream: Any) -> dict | None:
    """Read one ``Content-Length``-framed JSON-RPC message from ``stream``.

    ``stream`` is a binary stream exposing ``read(n) -> bytes`` (``sys.stdin.buffer``
    or a test fake feeding bytes in arbitrary chunks). Returns the decoded object,
    or ``None`` at a clean end of input. Robust to split reads (the body is read
    in a loop until ``Content-Length`` bytes arrive) and to unicode bodies
    (decoded as UTF-8). A malformed header or an undecodable/invalid JSON body
    raises :class:`ProtocolError` so the caller can log and continue rather than
    crash. The body is decoded but *not* required to be a JSON object here —
    dispatch validates the shape.
    """
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = stream.read(1)
        if not chunk:
            if not header:
                return None  # clean EOF between messages
            raise ProtocolError("unexpected EOF in header")
        header += chunk
    raw_head, _, rest = header.partition(b"\r\n\r\n")
    length = None
    for line in raw_head.split(b"\r\n"):
        if not line:
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise ProtocolError(f"bad Content-Length: {value!r}") from exc
    if length is None:
        raise ProtocolError("missing Content-Length header")
    if length < 0 or length > 64 * 1024 * 1024:
        raise ProtocolError(f"implausible Content-Length: {length}")
    body = bytearray(rest[:length])
    # `rest` may already hold some (or all) of the body from a batched read.
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise ProtocolError("unexpected EOF in body")
        body += chunk
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"bad JSON body: {exc}") from exc


def write_message(stream: Any, obj: dict) -> None:
    """Frame ``obj`` as a ``Content-Length`` JSON-RPC message and write it.

    The body is UTF-8; the header length is the byte count (not the character
    count), so unicode content frames correctly. Flushes so the client sees the
    reply promptly.
    """
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


# --- URI <-> path ------------------------------------------------------------


def uri_to_path(uri: str) -> str | None:
    """``file:///a/b.barn`` → ``/a/b.barn``; ``None`` for a non-``file:`` URI
    (an ``untitled:`` scratch buffer, say). Percent-escapes are decoded."""
    if not uri:
        return None
    parts = urlsplit(uri)
    if parts.scheme != "file":
        return None
    return unquote(parts.path) or None


def path_to_uri(path: str) -> str:
    """``/a/b.barn`` → ``file:///a/b.barn`` (a plain, portable file URI)."""
    return "file://" + path


def base_dir_for(uri: str) -> str | None:
    """The directory ``use`` relpaths resolve against for a document URI — the
    file's own directory, exactly like ``compile_file``. ``None`` for an untitled
    buffer (leaving ``use`` unresolvable, the existing teaching diagnostic)."""
    path = uri_to_path(uri)
    return os.path.dirname(path) if path else None


# --- text / position helpers -------------------------------------------------


def _lines(text: str) -> list[str]:
    """Split into lines *without* stripping — indices match LSP line numbers."""
    return text.split("\n")


def _line_at(text: str, line: int) -> str:
    lines = _lines(text)
    return lines[line] if 0 <= line < len(lines) else ""


#: A DSL word: identifiers (``a-b`` allowed), stamped ids (``m.bed``) and codes.
_WORD_CHARS = re.compile(r"[A-Za-z0-9_.\-]")


def word_at(line: str, char: int) -> tuple[str, int, int]:
    """The maximal ``[A-Za-z0-9_.-]`` run covering column ``char`` on ``line``.

    Returns ``(word, start, end)`` (both 0-based columns, ``end`` exclusive). An
    empty word (``("", char, char)``) when the cursor isn't on a word char.
    """
    n = len(line)
    if char > n:
        char = n
    start = char
    while start > 0 and _WORD_CHARS.match(line[start - 1]):
        start -= 1
    end = char
    while end < n and _WORD_CHARS.match(line[end]):
        end += 1
    return line[start:end], start, end


def _range(line: int, start: int, end: int) -> dict:
    return {
        "start": {"line": line, "character": start},
        "end": {"line": line, "character": end},
    }


def _line_head(line: str) -> tuple[str, int]:
    """The first bare word of ``line`` and its start column, ignoring leading
    whitespace. ``("", 0)`` for a blank or comment-only line."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return "", 0
    start = len(line) - len(stripped)
    m = re.match(r"[A-Za-z0-9_\-]+", stripped)
    return (m.group(0).lower() if m else ""), start


def _comment_start(line: str) -> int | None:
    """Column of the ``#`` that begins a comment (not one inside a string), or
    ``None`` — mirrors the lexer's quote handling."""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "#":
            return i
        if ch == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 1
        i += 1
    return None


# --- shared statement docs (from DSL_REFERENCE) ------------------------------


def _build_statement_docs() -> dict[str, str]:
    """Parse ``DSL_REFERENCE`` into ``keyword -> markdown block`` for hover.

    The reference lists each statement under ``Statements:`` as a 2-space-indented
    grammar line optionally followed by more-indented ``# …`` prose. A keyword
    with several forms (``door`` has three) folds them into one block.
    """
    docs: dict[str, list[str]] = {}
    lines = DSL_REFERENCE.splitlines()
    in_section = False
    current: str | None = None
    stmt_set = set(_STATEMENT_KEYWORDS) | {"outlet", "switch", "light", "use", "building"}
    for line in lines:
        if line.strip() == "Statements:":
            in_section = True
            continue
        if not in_section:
            continue
        if line and not line.startswith(" "):
            break  # left the indented Statements block
        if line.startswith("  ") and not line.startswith("   "):
            m = re.match(r"  ([A-Za-z_][\w\-]*)", line)
            head = m.group(1).lower() if m else None
            if head in stmt_set:
                current = head
                docs.setdefault(current, []).append(line[2:])
                continue
        if current is not None and line.strip():
            docs[current].append(line.strip() if line.strip().startswith("#") else line[2:])
    return {k: "\n".join(v) for k, v in docs.items()}


_STATEMENT_DOCS = _build_statement_docs()


# --- quick-fix snippet (shared with the playground JS heuristic, §3.7) -------

#: A placeholder the author would still have to fill in — an incomplete snippet.
_QUICKFIX_PLACEHOLDER = re.compile(r"\.\.\.|…|[<>]")
_BACKTICKED = re.compile(r"`([^`]+)`")
#: The statement heads a quick-fix snippet must start with — the same set the
#: playground injects as ``HIGHLIGHT.statements`` (``HL_STMT`` in the JS), so the
#: Python function and the JS ``quickFixSnippet`` agree by construction.
_QUICKFIX_STATEMENTS = frozenset(_STATEMENT_KEYWORDS)


def quickfix_snippet(hint: str | None) -> str | None:
    """A paste-able DSL line embedded in a diagnostic ``hint``, or ``None``.

    A hint often carries an example in backticks (``…e.g. `entry a south width 3
    offset 4`.``). We surface one only when it is a *complete, literal* statement
    the author can drop in as-is: its first word is a statement head (not a
    modifier like ``size``) and it carries no placeholder (``…``/``<>``). This is
    the exact heuristic the playground's ``quickFixSnippet`` JS runs, ported here
    so the LSP code-action and the playground Apply button never drift (pinned by
    a shared-fixture parity test).
    """
    if not hint:
        return None
    for m in _BACKTICKED.finditer(hint):
        snip = m.group(1).strip()
        head = (snip.split() or [""])[0].lower()
        if head in _QUICKFIX_STATEMENTS and not _QUICKFIX_PLACEHOLDER.search(snip):
            return snip
    return None


# --- compile helper ----------------------------------------------------------


def compile_document(text: str, base_dir: str | None) -> CompileResult:
    """Compile a document's text the way the editor sees it (full-document sync).

    Never raises — a source that doesn't parse still returns a ``CompileResult``
    with diagnostics (and possibly a recovered partial plan), exactly as the CLI
    and playground use it.
    """
    return compile_source(text, base_dir=base_dir)


# --- 3.1 diagnostics ---------------------------------------------------------

#: barndsl severity → LSP DiagnosticSeverity. An *accepted* (audited-deviation)
#: finding drops to Hint (4); it keeps its ``(accepted: …)`` note in the message.
_LSP_SEVERITY = {Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3}


def _issue_range(issue: Issue) -> dict:
    """The 0-based range for an issue: ``(line-1, col-1)``–``(line-1, end_col-1)``.
    A missing column underlines the whole line; a missing line anchors at line 0."""
    line = (issue.line - 1) if issue.line else 0
    if issue.col:
        start = issue.col - 1
        end = (issue.end_col - 1) if issue.end_col else start + 1
    else:
        start, end = 0, 1_000  # whole-line: a generous end, editors clamp it
    return _range(line, start, max(end, start + 1))


def diagnostics(result: CompileResult, uri: str) -> list[dict]:
    """Map a compile's issues to LSP ``Diagnostic`` objects.

    Severity per :data:`_LSP_SEVERITY` with accepted → Hint(4); the hint is
    appended to the message (the teaching voice belongs in the popup); source is
    ``"barndsl"``. A *part-internal* finding (``issue.file`` set) carries
    ``relatedInformation`` pointing at the part file's own line (its ``file://``
    URI), the primary range staying anchored at the host's ``use`` line.
    """
    out: list[dict] = []
    for issue in result.diagnostics:
        accepted = getattr(issue, "accepted", False)
        sev = 4 if accepted else _LSP_SEVERITY.get(issue.severity, 3)
        message = issue.message
        if accepted and issue.accept_reason:
            message = f"{message}  (accepted: {issue.accept_reason})"
        elif accepted:
            message = f"{message}  (accepted)"
        if issue.hint:
            message = f"{message}\n{issue.hint}"
        diag: dict = {
            "range": _issue_range(issue),
            "severity": sev,
            "code": issue.code,
            "source": "barndsl",
            "message": message,
        }
        part_file = getattr(issue, "file", None)
        if part_file:
            # The related line is the part's own line, encoded in the message the
            # composer built (``in part <relpath>:<line> — …``). Fall back to the
            # part file's first line when it isn't recoverable.
            part_line = _part_line_from_message(issue)
            diag["relatedInformation"] = [
                {
                    "location": {
                        "uri": path_to_uri(part_file),
                        "range": _range(part_line, 0, 0),
                    },
                    "message": "declared here",
                }
            ]
        out.append(diag)
    return out


def _part_line_from_message(issue: Issue) -> int:
    """Recover the part-file line (0-based) a part-internal diagnostic points at,
    from the ``in part <relpath>:<line> — …`` prefix the composer wrote."""
    m = re.search(r"in part [^:—]+:(\d+)", issue.message)
    if m:
        return max(int(m.group(1)) - 1, 0)
    return 0


# --- 3.2 hover ---------------------------------------------------------------


def _room_card(plan: Any, room: Any, *, stamped_from: str | None = None) -> str:
    """A one-line fact card for a room: ``bed — bedroom · 14′ × 11′ · 154 sq ft ·
    level 0``, with an optional stamped-from tail."""
    type_name = room.type.value if isinstance(room.type, RoomType) else str(room.type)
    dims = f"{fmt_ft_in(room.width)} × {fmt_ft_in(room.length)}"
    card = (
        f"**{room.id}** — {type_name} · {dims} · "
        f"{room.area:.0f} sq ft · level {room.level}"
    )
    if stamped_from:
        card += f"\n\nstamped from `{stamped_from}`"
    return card


def hover(text: str, result: CompileResult, line: int, char: int) -> dict | None:
    """Hover contents for the token under ``(line, char)`` (0-based), or ``None``.

    Three cases: a statement keyword at the line head → its ``DSL_REFERENCE``
    entry; an accept-pragma diagnostic code inside a comment → the registry
    explanation; an identifier that resolves in the compiled plan → a fact card.
    """
    src_line = _line_at(text, line)
    head, head_col = _line_head(src_line)
    word, start, end = word_at(src_line, char)

    # 1. A statement keyword at the line head.
    if head and start == head_col and word.lower() == head and head in _STATEMENT_DOCS:
        body = _STATEMENT_DOCS[head]
        value = f"```\n{body}\n```"
        return {"contents": {"kind": "markdown", "value": value}, "range": _range(line, start, end)}

    # 2. A diagnostic code inside an accept pragma comment.
    cut = _comment_start(src_line)
    if cut is not None and start >= cut and "accept" in src_line[cut:].lower():
        code = word.upper()
        if code in REGISTRY:
            value = f"```\n{explain(code)}\n```"
            return {
                "contents": {"kind": "markdown", "value": value},
                "range": _range(line, start, end),
            }

    # 2b. Anywhere on a `fixture counter` line → a run fact card (run in ft-in).
    if head == "fixture" and result.plan is not None:
        card = _counter_card(result.plan, line)
        if card is not None:
            return {"contents": {"kind": "markdown", "value": card},
                    "range": _range(line, start, end)}

    # 3. An identifier that resolves in the compiled plan.
    if not word or result.plan is None:
        return None
    plan = result.plan

    # A stamped id (m.bed) — the card plus its part origin.
    inst = _instance_for_stamped(plan, word)
    if inst is not None:
        room = plan.room(word)
        if room is not None:
            value = _room_card(plan, room, stamped_from=inst.relpath)
            return {"contents": {"kind": "markdown", "value": value}, "range": _range(line, start, end)}

    # An instance alias — part path + transform + bbox.
    for i in plan.instances:
        if i.alias == word:
            xf = []
            if i.rotate:
                xf.append(f"rotate {i.rotate}")
            if i.mirror:
                xf.append(f"mirror {i.mirror}")
            xf_s = (" · " + ", ".join(xf)) if xf else ""
            bx0, by0, bx1, by1 = i.bbox
            value = (
                f"**{i.alias}** — instance of `{i.relpath}`{xf_s}\n\n"
                f"bbox {fmt_ft_in(bx1 - bx0)} × {fmt_ft_in(by1 - by0)} "
                f"at {bx0:g},{by0:g} · level {i.level} · {len(i.room_ids)} room(s)"
            )
            return {"contents": {"kind": "markdown", "value": value}, "range": _range(line, start, end)}

    # A plain room id.
    room = plan.room(word)
    if room is not None:
        value = _room_card(plan, room)
        return {"contents": {"kind": "markdown", "value": value}, "range": _range(line, start, end)}
    return None


def _counter_card(plan: Any, line: int) -> str | None:
    """A fact card for a counter authored on 0-based ``line`` — its resolved run and
    depth in feet-and-inches. ``None`` if no counter is placed by that source line."""
    from .fixtures import resolve_room_fixtures

    for room in plan.rooms:
        for f in resolve_room_fixtures(plan, room):
            if f.kind != "counter" or f.source_line != line + 1:
                continue
            run = max(f.width, f.length)
            depth = min(f.width, f.length)
            return (
                f"**counter run** — {fmt_ft_in(run)} long × {fmt_ft_in(depth)} deep · "
                f"along {f.wall} wall of `{room.id}` · {run * depth:.0f} sq ft"
            )
    return None


def _instance_for_stamped(plan: Any, rid: str) -> Any:
    """The :class:`Instance` a stamped room id belongs to, or ``None``."""
    for inst in getattr(plan, "instances", []):
        if rid in inst.room_ids:
            return inst
    return None


# --- 3.3 completion ----------------------------------------------------------

#: CompletionItemKind values we use (LSP §Completion).
_KIND_KEYWORD = 14
_KIND_VALUE = 12
_KIND_CLASS = 7
_KIND_FILE = 17

#: Opening statement heads whose next id slot is a room id (mirrors the
#: playground's ``AC_ROOM_HEADS``).
_ROOM_HEADS = frozenset({"door", "open", "window", "entry"})
_ANCHORS = frozenset({
    "east-of", "west-of", "north-of", "south-of",
    "right-of", "left-of", "above-of", "below-of",
})
_WALL_DIRS = ("north", "south", "east", "west")


def _room_ids(result: CompileResult) -> list[tuple[str, str]]:
    """``(id, detail)`` for every room in the last good compile, stamped ids
    included — the id slots complete against the real plan."""
    plan = result.plan
    if plan is None:
        return []
    out = []
    for r in plan.rooms:
        type_name = r.type.value if isinstance(r.type, RoomType) else str(r.type)
        out.append((r.id, type_name))
    return out


def _item(label: str, kind: int, detail: str | None = None) -> dict:
    item = {"label": label, "kind": kind}
    if detail:
        item["detail"] = detail
    return item


def _use_string_prefix(before: str) -> bool:
    """True when ``before`` (line text up to the cursor) sits inside the quoted
    relpath of a ``use "…`` statement (an odd number of quotes after ``use``)."""
    s = before.lstrip()
    if not s.lower().startswith("use"):
        return False
    return before.count('"') % 2 == 1


def completions(
    text: str, line: int, char: int, result: CompileResult, base_dir: str | None
) -> list[dict]:
    """Context-keyed completion items for the cursor at ``(line, char)``.

    Contexts (reusing the playground's autocomplete rules where practical):
    line start → statement keywords; an id slot (after ``door``/``open``/``in``/an
    anchor/``-``) → room ids incl. stamped; ``room <id>:`` → room types;
    ``fixture`` → the fixture catalog; a wall slot → N/S/E/W; ``alarm`` → the alarm
    kinds; inside ``use "`` → part relpaths; after ``# barndsl: accept `` →
    registry codes.
    """
    src_line = _line_at(text, line)
    before = src_line[:char]

    # Inside an accept pragma: complete diagnostic codes.
    cut = _comment_start(src_line)
    if cut is not None and char > cut:
        comment = src_line[cut:char].lower()
        if re.search(r"accept\s+\S*$", comment):
            return [_item(code, _KIND_VALUE, REGISTRY[code].title) for code in sorted(REGISTRY)]
        return []

    # Inside a `use "…` path: complete part relpaths.
    if _use_string_prefix(before):
        return [
            _item(p["relpath"], _KIND_FILE, f"{p['name']} · {p['rooms']} room(s)")
            for p in scan_parts(base_dir)
        ]

    _word, wstart, _wend = word_at(src_line, char)
    prefix_text = src_line[:wstart]
    toks = prefix_text.split()
    head = toks[0].lower() if toks else ""
    last = toks[-1].lower() if toks else ""

    # Line start → statement keywords.
    if not toks:
        return [
            _item(kw, _KIND_KEYWORD, _statement_summary(kw))
            for kw in sorted(_STATEMENT_KEYWORDS)
        ]

    # Room-id slot.
    if last in _ROOM_HEADS or last in _ANCHORS or last == "in" or last == "-":
        return [_item(rid, _KIND_VALUE, detail) for rid, detail in _room_ids(result)]

    # `room <id>:` → room types.
    if head == "room" and re.match(r"room\s+[A-Za-z_][\w\-]*:\s*$", prefix_text):
        return [_item(t.value, _KIND_CLASS) for t in RoomType]

    # `fixture <kind>`.
    if head == "fixture" and len(toks) == 1:
        return [_item(k, _KIND_VALUE) for k in sorted(FIXTURES)]

    # `fixture counter in <room> …` option slots: `along` (and `at`/`wall`) after
    # the room id, then `from`/`to`/`depth` inside an `along` run.
    if head == "fixture" and "in" in toks and "counter" in toks:
        if "along" in toks and last != "along":
            return [_item(k, _KIND_KEYWORD) for k in ("from", "to", "depth")]
        if len(toks) >= 4 and "along" not in toks and "at" not in toks:
            return [_item(k, _KIND_KEYWORD) for k in ("along", "at", "wall")]

    # `alarm <kind>`.
    if head == "alarm" and len(toks) == 1:
        return [_item(k, _KIND_VALUE) for k in ALARM_KINDS]

    # A wall-direction slot: after the `wall` modifier (or `along` on a counter),
    # or the wall slot of a `window`/`entry`/exterior `door` statement.
    if last in ("wall", "along"):
        return [_item(d, _KIND_VALUE) for d in _WALL_DIRS]
    if head in ("window", "entry") and len(toks) == 2 and last not in ("-",):
        return [_item(d, _KIND_VALUE) for d in _WALL_DIRS]

    return []


def _statement_summary(kw: str) -> str | None:
    """The grammar line (first line of the statement's doc block) as a one-line
    completion detail."""
    body = _STATEMENT_DOCS.get(kw)
    if not body:
        return None
    first = body.splitlines()[0].strip()
    return first or None


# --- 3.4 formatting ----------------------------------------------------------


def formatting(text: str) -> list[dict]:
    """A single full-document ``TextEdit`` with the canonically-formatted source,
    or an empty list when the source has parse errors (mirroring ``fmt``'s CLI
    refusal — a formatter must never mask breakage)."""
    result = compile_source(text)
    if result.plan is None or result.recovered:
        return []
    formatted = format_source(text)
    if formatted == text:
        return []
    lines = _lines(text)
    end_line = len(lines) - 1
    end_char = len(lines[-1]) if lines else 0
    full_range = {
        "start": {"line": 0, "character": 0},
        "end": {"line": end_line, "character": end_char},
    }
    return [{"range": full_range, "newText": formatted}]


# --- 3.5 definition ----------------------------------------------------------


def definition(
    text: str, result: CompileResult, line: int, char: int, uri: str, base_dir: str | None
) -> list[dict]:
    """Definition location(s) for the identifier under the cursor (LSP allows a
    list — editors show a picker for more than one).

    A local room id → its ``room`` statement line. A stamped id (``m.bed``) → two
    locations: the host ``use`` line, and the ``room`` line inside the part file.
    A path inside a ``use "…"`` string → the part file (its first line).
    """
    src_line = _line_at(text, line)
    before = src_line[:char]
    plan = result.plan

    # A part path inside a `use "…"` string → the part file.
    if _use_string_prefix(before) or _cursor_in_use_string(src_line, char):
        relpath = _use_relpath(src_line)
        if relpath and base_dir:
            target = os.path.realpath(os.path.join(base_dir, relpath))
            if os.path.isfile(target):
                return [{"uri": path_to_uri(target), "range": _range(0, 0, 0)}]
        return []

    word, _s, _e = word_at(src_line, char)
    if not word or plan is None:
        return []

    # A stamped id → the use line + the part file's room line.
    inst = _instance_for_stamped(plan, word)
    if inst is not None:
        locs: list[dict] = []
        if inst.line:
            locs.append({"uri": uri, "range": _range(inst.line - 1, 0, 0)})
        local = word[len(inst.alias) + 1:]
        part_line = _part_room_line(inst.part_path, local)
        if part_line is not None:
            locs.append({"uri": path_to_uri(inst.part_path), "range": _range(part_line, 0, 0)})
        return locs

    # A local room id → its declaring statement line.
    if word in result.room_lines:
        ln = result.room_lines[word] - 1
        return [{"uri": uri, "range": _range(ln, 0, 0)}]
    return []


def _cursor_in_use_string(line: str, char: int) -> bool:
    """True when ``char`` falls inside the quoted relpath of a ``use`` line."""
    if not line.lstrip().lower().startswith("use"):
        return False
    quotes = [i for i, c in enumerate(line) if c == '"']
    if len(quotes) < 2:
        return False
    return quotes[0] < char <= quotes[1]


def _use_relpath(line: str) -> str | None:
    """The quoted relpath from a ``use "<relpath>" …`` line, or ``None``."""
    m = re.match(r'\s*use\s+"([^"]*)"', line)
    return m.group(1) if m else None


def _part_room_line(part_path: str, local_id: str) -> int | None:
    """The 0-based line of ``room <local_id>:`` inside a part file (by
    fragment-compiling it), or ``None``."""
    try:
        with open(part_path, encoding="utf-8") as fh:
            part_text = fh.read()
    except OSError:
        return None
    part = compile_source(part_text, fragment=True, base_dir=os.path.dirname(part_path))
    ln = part.room_lines.get(local_id)
    return (ln - 1) if ln else None


# --- 3.6 document symbols ----------------------------------------------------

#: SymbolKind values (LSP §DocumentSymbol).
_SYM_NAMESPACE = 3
_SYM_FIELD = 8
_SYM_OBJECT = 19  # "Object" — used for the grouping nodes
_SYM_KEY = 20  # "Key" — instances


def _symbol(name: str, kind: int, line: int, *, detail: str = "", children: list | None = None) -> dict:
    rng = _range(line, 0, 0)
    sym: dict = {
        "name": name,
        "kind": kind,
        "range": rng,
        "selectionRange": rng,
    }
    if detail:
        sym["detail"] = detail
    if children:
        sym["children"] = children
    return sym


def symbols(result: CompileResult) -> list[dict]:
    """A hierarchical ``DocumentSymbol[]`` outline from the last good compile: the
    plan name at the root, rooms (with their statement-line ranges), and the
    stamped instances (``▣ alias``). Empty when there is no plan."""
    plan = result.plan
    if plan is None:
        return []
    children: list[dict] = []
    stamped: set[str] = getattr(plan, "stamped_rooms", set())
    for r in plan.rooms:
        if r.id in stamped:
            continue  # stamped rooms belong under their instance, not the root
        line = (result.room_lines.get(r.id, 1)) - 1
        type_name = r.type.value if isinstance(r.type, RoomType) else str(r.type)
        children.append(_symbol(r.id, _SYM_FIELD, max(line, 0), detail=type_name))
    for inst in getattr(plan, "instances", []):
        line = (inst.line - 1) if inst.line else 0
        members = []
        for rid in inst.room_ids:
            member = plan.room(rid)
            detail = member.type.value if member and isinstance(member.type, RoomType) else ""
            members.append(_symbol(rid, _SYM_FIELD, line, detail=detail))
        children.append(
            _symbol(f"▣ {inst.alias}", _SYM_KEY, line, detail=inst.relpath, children=members)
        )
    root = _symbol(plan.name, _SYM_NAMESPACE, 0, children=children)
    return [root]


# --- 3.7 code actions --------------------------------------------------------


def code_actions(
    text: str, result: CompileResult, uri: str, rng: dict
) -> list[dict]:
    """Quick-fix ``CodeAction`` list for the diagnostics overlapping ``rng``.

    For each diagnostic with a paste-able :func:`quickfix_snippet`: an action
    inserting the snippet as a fresh line after the diagnostic's line. For each
    WARNING/INFO diagnostic: an *Accept CODE (audited deviation)* action appending
    ``# barndsl: accept CODE ""`` to the offending line — the LSP twin of the
    pragma workflow. Errors can't be accepted, so they get no accept action.
    """
    lines = _lines(text)
    lo = rng.get("start", {}).get("line", 0)
    hi = rng.get("end", {}).get("line", len(lines) - 1)
    actions: list[dict] = []
    for issue in result.diagnostics:
        dline0 = (issue.line - 1) if issue.line else 0
        if issue.line and not (lo <= dline0 <= hi):
            continue
        lsp_diag = {
            "range": _issue_range(issue),
            "severity": 4 if getattr(issue, "accepted", False) else _LSP_SEVERITY.get(issue.severity, 3),
            "code": issue.code,
            "source": "barndsl",
            "message": issue.message,
        }
        snip = quickfix_snippet(issue.hint)
        if snip:
            insert_line = issue.line if issue.line else len(lines)
            edit = {
                "range": _range(insert_line, 0, 0),
                "newText": snip + "\n",
            }
            actions.append({
                "title": f"Insert: {snip if len(snip) <= 40 else snip[:37] + '…'}",
                "kind": "quickfix",
                "diagnostics": [lsp_diag],
                "edit": {"changes": {uri: [edit]}},
            })
        # Accept action — only for a non-accepted warning/info (errors can't be
        # accepted; an already-accepted finding needs no second pragma).
        if (
            issue.severity in (Severity.WARNING, Severity.INFO)
            and not getattr(issue, "accepted", False)
            and issue.line
            and 1 <= issue.line <= len(lines)
        ):
            src = lines[issue.line - 1]
            edit = {
                "range": _range(issue.line - 1, len(src), len(src)),
                "newText": f'  # barndsl: accept {issue.code} ""',
            }
            actions.append({
                "title": f"Accept {issue.code} (audited deviation)",
                "kind": "quickfix",
                "diagnostics": [lsp_diag],
                "edit": {"changes": {uri: [edit]}},
            })
    return actions


# --- 3.8 rename --------------------------------------------------------------


def prepare_rename(
    text: str, result: CompileResult, line: int, char: int
) -> dict | None:
    """The renamable range for the room id under the cursor, or ``None`` to reject.

    A stamped member (``m.bed``) is rejected with a teaching message (rename it in
    the part file); a non-room identifier is silently rejected. Returns
    ``{range, placeholder}`` for a plain room id.
    """
    src_line = _line_at(text, line)
    word, start, end = word_at(src_line, char)
    plan = result.plan
    if not word or plan is None:
        return None
    if _instance_for_stamped(plan, word) is not None or "." in word:
        raise _RenameRejected(
            "Stamped ids are read-only here — rename the room inside its part file."
        )
    if plan.room(word) is None or word not in result.room_lines:
        return None
    return {"range": _range(line, start, end), "placeholder": word}


class _RenameRejected(Exception):
    """A rename that must be reported to the client as an error (not silently)."""


def rename(
    text: str, result: CompileResult, line: int, char: int, new_name: str, uri: str,
    base_dir: str | None,
) -> dict | None:
    """A ``WorkspaceEdit`` renaming the room id under the cursor to ``new_name``.

    Runs the surgical ``rename_room`` edit (which already rewrites every reference
    — the ``room`` line, every ``door``/``window``/``wall``/``require`` mention)
    and returns a single full-document replacement. ``None`` when the cursor isn't
    on a renamable room id or the edit is refused.
    """
    src_line = _line_at(text, line)
    word, _s, _e = word_at(src_line, char)
    plan = result.plan
    if not word or plan is None or plan.room(word) is None:
        return None
    if _instance_for_stamped(plan, word) is not None or "." in word:
        return None
    edit_result = apply_edit(text, Edit("rename_room", room=word, to=new_name), base_dir=base_dir)
    if not edit_result.ok or not edit_result.changed:
        return None
    lines = _lines(text)
    end_line = len(lines) - 1
    end_char = len(lines[-1]) if lines else 0
    full = {
        "range": {"start": {"line": 0, "character": 0},
                  "end": {"line": end_line, "character": end_char}},
        "newText": edit_result.source,
    }
    return {"changes": {uri: [full]}}


# --- capabilities ------------------------------------------------------------


def server_capabilities(rename_enabled: bool = True) -> dict:
    """The ``ServerCapabilities`` advertised in ``initialize`` — exactly the v1
    set (full-document sync, no workspace folders). ``rename_enabled`` gates the
    rename provider so ``--check`` can report what shipped."""
    caps: dict = {
        "textDocumentSync": {"openClose": True, "change": 1, "save": False},
        "hoverProvider": True,
        "completionProvider": {"triggerCharacters": [" ", '"', ".", "-"]},
        "documentFormattingProvider": True,
        "definitionProvider": True,
        "documentSymbolProvider": True,
        "codeActionProvider": {"codeActionKinds": ["quickfix"]},
    }
    if rename_enabled:
        caps["renameProvider"] = {"prepareProvider": True}
    return caps


#: The method names the server understands (for ``--check`` and the report).
ADVERTISED_METHODS = {
    "lifecycle": ["initialize", "initialized", "shutdown", "exit"],
    "textDocument/didOpen": "diagnostics",
    "textDocument/didChange": "diagnostics (full sync)",
    "textDocument/didClose": "clear diagnostics",
    "textDocument/didSave": "ignored",
    "textDocument/hover": "hover",
    "textDocument/completion": "completion",
    "textDocument/formatting": "formatting",
    "textDocument/definition": "definition",
    "textDocument/documentSymbol": "documentSymbol",
    "textDocument/codeAction": "codeAction",
    "textDocument/rename": "rename",
    "textDocument/prepareRename": "prepareRename",
    "textDocument/publishDiagnostics": "server→client notification",
}


# --- document store ----------------------------------------------------------


class DocumentStore:
    """``uri -> (text, version)`` for the open documents (full-document sync)."""

    def __init__(self) -> None:
        self._docs: dict[str, tuple[str, int]] = {}

    def open(self, uri: str, text: str, version: int) -> None:
        self._docs[uri] = (text, version)

    def update(self, uri: str, text: str, version: int) -> None:
        self._docs[uri] = (text, version)

    def close(self, uri: str) -> None:
        self._docs.pop(uri, None)

    def text(self, uri: str) -> str | None:
        entry = self._docs.get(uri)
        return entry[0] if entry else None

    def version(self, uri: str) -> int | None:
        entry = self._docs.get(uri)
        return entry[1] if entry else None

    def __contains__(self, uri: str) -> bool:
        return uri in self._docs


# --- the stdio server --------------------------------------------------------


class Server:
    """The JSON-RPC-over-stdio protocol shell.

    Owns the :class:`DocumentStore`, a per-version compile cache, and the dispatch
    table. Every capability is delegated to a pure function above; this class only
    frames, dispatches, replies and manages the document lifecycle.

    **Compile coalescing (§2.2).** A ``didChange`` marks the document *dirty* and
    stores the new text without compiling. The compile (and diagnostics publish)
    is *deferred* to the moment the loop is about to block on input, or to the
    next request that needs the plan — whichever comes first. Before blocking we
    peek at input readability with a zero-timeout ``select`` on the reader's file
    descriptor; if another message is already waiting we handle it first, so a
    burst of ``didChange`` for one document collapses to a single compile of the
    newest text. This is coalescing *by draining* with no threads, timers or
    races (design §7): the store always holds the newest text and the last compile
    wins, so a request answer and the published diagnostics are always current. It
    is best-effort — a buffered reader that reads ahead can hide waiting bytes
    from ``select``, in which case we simply compile eagerly (correct, just less
    coalesced) — never wrong.
    """

    def __init__(self, reader: Any, writer: Any) -> None:
        self.reader = reader
        self.writer = writer
        self.store = DocumentStore()
        self.running = True
        self.initialized = False
        self._shutdown_requested = False
        self.root_uri: str | None = None
        #: uri -> version that still needs a compile+publish.
        self._dirty: dict[str, int] = {}
        #: uri -> (version, CompileResult) — the cache a request reuses.
        self._compiled: dict[str, tuple[int, CompileResult]] = {}
        self.rename_enabled = True

    # -- transport --

    def _send(self, obj: dict) -> None:
        try:
            write_message(self.writer, obj)
        except (BrokenPipeError, ValueError, OSError):
            self.running = False

    def _reply(self, req_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _log(self, message: str) -> None:
        print(f"barndsl-lsp: {message}", file=sys.stderr, flush=True)

    # -- compile cache / coalescing --

    def _ensure_compiled(self, uri: str) -> CompileResult | None:
        """The current compile for ``uri`` — compiling (and publishing) first if
        the document is dirty. ``None`` for an unknown document."""
        text = self.store.text(uri)
        if text is None:
            return None
        version = self.store.version(uri) or 0
        cached = self._compiled.get(uri)
        if uri in self._dirty or cached is None or cached[0] != version:
            self._flush(uri)
            cached = self._compiled.get(uri)
        return cached[1] if cached else None

    def _flush(self, uri: str) -> None:
        """Compile ``uri``'s newest text, cache the result and publish diagnostics."""
        text = self.store.text(uri)
        if text is None:
            self._dirty.pop(uri, None)
            return
        version = self.store.version(uri) or 0
        result = compile_document(text, base_dir_for(uri))
        self._compiled[uri] = (version, result)
        self._dirty.pop(uri, None)
        self._publish(uri, result, version)

    def _flush_all_dirty(self) -> None:
        for uri in list(self._dirty):
            self._flush(uri)

    def _publish(self, uri: str, result: CompileResult, version: int) -> None:
        self._notify(
            "textDocument/publishDiagnostics",
            {"uri": uri, "version": version, "diagnostics": diagnostics(result, uri)},
        )

    def _input_ready(self) -> bool:
        """True when another message is *immediately* readable (zero-timeout
        ``select``). Best-effort: an unselectable reader (a test fake, a buffered
        reader with ahead-read bytes) returns ``False`` and we compile eagerly."""
        try:
            fd = self.reader.fileno()
        except (AttributeError, OSError, ValueError):
            return False
        try:
            ready, _, _ = select.select([fd], [], [], 0)
            return bool(ready)
        except (OSError, ValueError):
            return False

    # -- main loop --

    def run(self) -> int:
        """Read, dispatch and reply until ``exit`` (or clean EOF). Returns the
        process exit code (0 for a clean ``shutdown``→``exit``, 1 otherwise)."""
        while self.running:
            # Deferred compile: flush any dirty document before we block, unless
            # more input is already waiting (then coalesce by handling it first).
            if self._dirty and not self._input_ready():
                self._flush_all_dirty()
            try:
                msg = read_message(self.reader)
            except ProtocolError as exc:
                self._log(f"dropping malformed frame: {exc}")
                continue
            if msg is None:
                break  # clean EOF
            try:
                self._dispatch(msg)
            except _RenameRejected:
                raise
            except Exception as exc:  # a feature bug must not kill the server
                self._log(f"internal error handling {msg.get('method')!r}: {exc}")
                if isinstance(msg, dict) and msg.get("id") is not None:
                    self._error(msg["id"], INTERNAL_ERROR, str(exc))
        return 0 if self._shutdown_requested else 1

    def _dispatch(self, msg: dict) -> None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self._log("dropping non-JSON-RPC-2.0 message")
            return
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if method is None:
            return  # a response (we send none that need replies) — ignore
        handler = self._REQUESTS.get(method) if req_id is not None else self._NOTIFICATIONS.get(method)
        if handler is None:
            if req_id is not None:
                self._error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")
            # unknown notification → silently ignored
            return
        handler(self, req_id, params)

    # -- lifecycle --

    def _on_initialize(self, req_id: Any, params: dict) -> None:
        self.root_uri = params.get("rootUri")
        self._reply(req_id, {
            "capabilities": server_capabilities(self.rename_enabled),
            "serverInfo": {"name": "barndsl-lsp", "version": _version()},
        })

    def _on_initialized(self, req_id: Any, params: dict) -> None:
        self.initialized = True

    def _on_shutdown(self, req_id: Any, params: dict) -> None:
        self._shutdown_requested = True
        self._reply(req_id, None)

    def _on_exit(self, req_id: Any, params: dict) -> None:
        self.running = False

    def _on_cancel(self, req_id: Any, params: dict) -> None:
        pass  # everything is fast — cancellation is a no-op

    # -- text sync --

    def _on_did_open(self, req_id: Any, params: dict) -> None:
        doc = params.get("textDocument", {})
        uri = doc.get("uri")
        if uri is None:
            return
        self.store.open(uri, doc.get("text", ""), doc.get("version", 0))
        self._dirty[uri] = doc.get("version", 0)

    def _on_did_change(self, req_id: Any, params: dict) -> None:
        doc = params.get("textDocument", {})
        uri = doc.get("uri")
        changes = params.get("contentChanges") or []
        if uri is None or not changes:
            return
        # Full-document sync: the last change carries the whole new text.
        text = changes[-1].get("text", "")
        version = doc.get("version", (self.store.version(uri) or 0) + 1)
        self.store.update(uri, text, version)
        self._dirty[uri] = version  # deferred compile — see Server docstring

    def _on_did_close(self, req_id: Any, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        if uri is None:
            return
        self.store.close(uri)
        self._dirty.pop(uri, None)
        self._compiled.pop(uri, None)
        self._notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})

    def _on_did_save(self, req_id: Any, params: dict) -> None:
        pass  # nothing to do — we compile on change, not on save

    # -- feature requests --

    def _doc_position(self, params: dict) -> tuple[str, str, int, int, CompileResult] | None:
        uri = params.get("textDocument", {}).get("uri")
        if uri is None:
            return None
        text = self.store.text(uri)
        if text is None:
            return None
        result = self._ensure_compiled(uri)
        if result is None:
            return None
        pos = params.get("position", {})
        return uri, text, pos.get("line", 0), pos.get("character", 0), result

    def _on_hover(self, req_id: Any, params: dict) -> None:
        ctx = self._doc_position(params)
        if ctx is None:
            self._reply(req_id, None)
            return
        _uri, text, line, char, result = ctx
        self._reply(req_id, hover(text, result, line, char))

    def _on_completion(self, req_id: Any, params: dict) -> None:
        ctx = self._doc_position(params)
        if ctx is None:
            self._reply(req_id, [])
            return
        uri, text, line, char, result = ctx
        self._reply(req_id, completions(text, line, char, result, base_dir_for(uri)))

    def _on_formatting(self, req_id: Any, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        text = self.store.text(uri) if uri else None
        self._reply(req_id, formatting(text) if text is not None else [])

    def _on_definition(self, req_id: Any, params: dict) -> None:
        ctx = self._doc_position(params)
        if ctx is None:
            self._reply(req_id, None)
            return
        uri, text, line, char, result = ctx
        self._reply(req_id, definition(text, result, line, char, uri, base_dir_for(uri)))

    def _on_document_symbol(self, req_id: Any, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        result = self._ensure_compiled(uri) if uri else None
        self._reply(req_id, symbols(result) if result else [])

    def _on_code_action(self, req_id: Any, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        if uri is None:
            self._reply(req_id, [])
            return
        text = self.store.text(uri)
        result = self._ensure_compiled(uri)
        if text is None or result is None:
            self._reply(req_id, [])
            return
        self._reply(req_id, code_actions(text, result, uri, params.get("range", {})))

    def _on_prepare_rename(self, req_id: Any, params: dict) -> None:
        ctx = self._doc_position(params)
        if ctx is None:
            self._reply(req_id, None)
            return
        _uri, text, line, char, result = ctx
        try:
            self._reply(req_id, prepare_rename(text, result, line, char))
        except _RenameRejected as exc:
            self._error(req_id, INVALID_REQUEST, str(exc))

    def _on_rename(self, req_id: Any, params: dict) -> None:
        ctx = self._doc_position(params)
        if ctx is None:
            self._reply(req_id, None)
            return
        uri, text, line, char, result = ctx
        new_name = params.get("newName", "")
        self._reply(req_id, rename(text, result, line, char, new_name, uri, base_dir_for(uri)))

    _REQUESTS: dict[str, Callable] = {
        "initialize": _on_initialize,
        "shutdown": _on_shutdown,
        "textDocument/hover": _on_hover,
        "textDocument/completion": _on_completion,
        "textDocument/formatting": _on_formatting,
        "textDocument/definition": _on_definition,
        "textDocument/documentSymbol": _on_document_symbol,
        "textDocument/codeAction": _on_code_action,
        "textDocument/prepareRename": _on_prepare_rename,
        "textDocument/rename": _on_rename,
    }
    _NOTIFICATIONS: dict[str, Callable] = {
        "initialized": _on_initialized,
        "exit": _on_exit,
        "$/cancelRequest": _on_cancel,
        "textDocument/didOpen": _on_did_open,
        "textDocument/didChange": _on_did_change,
        "textDocument/didClose": _on_did_close,
        "textDocument/didSave": _on_did_save,
    }


def _version() -> str:
    from . import __version__

    return __version__


def run_stdio() -> int:
    """Serve on stdin/stdout (the ``barndsl lsp`` subcommand). Binary streams so
    framing byte counts are exact regardless of the ambient text encoding."""
    return Server(sys.stdin.buffer, sys.stdout.buffer).run()


def check() -> int:
    """Print the negotiated capabilities and the advertised methods, then exit 0 —
    a smoke test for editor configs (``barndsl lsp --check``)."""
    caps = server_capabilities()
    print("barndsl lsp — negotiated capabilities")
    print(json.dumps(caps, indent=2))
    print("\nadvertised methods:")
    print("  lifecycle: " + ", ".join(ADVERTISED_METHODS["lifecycle"]))
    for method, role in ADVERTISED_METHODS.items():
        if method == "lifecycle":
            continue
        print(f"  {method} — {role}")
    return 0
