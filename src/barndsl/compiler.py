"""The barndsl compiler: DSL source text → a validated plan + diagnostics.

This is the front-end the AI (or a human) writes against. You hand it source
text in the barndsl architecture language; it lexes, parses, lowers to a
:class:`~barndsl.elements.Barndominium`, runs the building-code checks, and
returns a :class:`CompileResult` whose diagnostics carry line numbers, error
codes, and concrete fix hints — like a compiler's error output. That diagnostic
stream is what guides an author (or an agent) toward a correct design.

Grammar (one statement per line; ``#`` starts a comment; ``{`` ``}`` optional)::

    plan "Name"
    envelope <W> x <L>
    wing <W> x <L> at <x>,<y>          # optional — L/T/U footprint extensions
    ceiling <H>
    note "free text"
    room <id>: <type> <placement> size <W> x <L> [level <n>]
    door <id_a> - <id_b> [width <w>]
    open <id_a> - <id_b> [width <w>]    # cased opening / walk-through (no leaf)
    entry <id> <wall> [width <w>] [offset <o>] [no-egress]
    window <id> <wall> [width <w>] [offset <o>]
    porch <id> at <x>,<y> size <W> x <L> [covered|open]
    stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]

``<placement>`` is ``at <x>,<y>`` (absolute), ``east-of|west-of|north-of|
south-of <room>`` (abut an already-defined room), or one of each to pin a corner.

Coordinates are in feet; origin (0,0) is the south-west corner, x→east, y→north.
``<type>`` is a RoomType value (living, kitchen, bedroom, bathroom, hallway,
shop, …); ``<wall>`` is north|south|east|west.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .elements import Barndominium, Direction, RoomType
from .validation import Issue, Severity, ValidationReport, validate

# Statement keywords, for "unknown statement" hints.
_KEYWORDS = (
    "plan", "envelope", "wing", "ceiling", "note", "room", "door", "open", "entry",
    "window", "porch", "stair"
)
_TYPES = ", ".join(t.value for t in RoomType)
_WALLS = "north, south, east, west"

#: Human/LLM-facing language reference, reused in the agent's system prompt so
#: the grammar has a single source of truth.
DSL_REFERENCE = """\
THE barndsl ARCHITECTURE LANGUAGE
One statement per line. '#' begins a comment. Braces { } are optional.
Measurements are in FEET. Origin (0,0) is the south-west corner; x increases
east, y increases north. A room at x,y with size W x L occupies [x, x+W] east-west
and [y, y+L] south-north (south wall=y, north=y+L, west=x, east=x+W).

Statements:
  plan "Name"
  envelope <W> x <L>              # primary footprint block (at the origin)
  wing <W> x <L> at <x>,<y>       # optional; add blocks for an L/T/U footprint
  ceiling <H>                     # ceiling height (>= 7; 9-12 typical)
  note "free text"                # optional design note
  room <id>: <type> <placement> size <W> x <L> [level <n>]
  door <id_a> - <id_b> [width <w>]            # interior door (rooms must share a wall)
  open <id_a> - <id_b> [width <w>]            # cased opening / walk-through, no door leaf
  entry <id> <wall> [width <w>] [offset <o>] [no-egress]   # exterior door, on an exterior wall
  window <id> <wall> [width <w>] [offset <o>]              # window, on an exterior wall
  porch <id> at <x>,<y> size <W> x <L> [covered|open]
  stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]
        # vertical circulation; defaults from 0 to 1. Place its footprint over a
        # room on each level so it links them (and makes the upper floor reachable).

<placement> is one of:
  at <x>,<y>                      # absolute, in feet
  <dir>-of <room> [align near|far|center] [offset <n>]
        # <dir> = east|west|north|south (aliases right|left|above|below). Abut an
        # already-defined room (shares a wall, so a `door` between them resolves).
        # By default the new room aligns to the reference's near corner; `align
        # far|center` slides it along the shared wall, and `offset <n>` shifts it
        # further (+north for east/west anchors, +east for north/south anchors).
  <h-dir>-of <room> <v-dir>-of <room>     # pocket placement: one horizontal anchor
        # (east/west) sets x, one vertical anchor (north/south) sets y, pinning the
        # room into a corner between two rooms. align/offset don't apply here.
<level> defaults to 0 (ground). A loft on level 1 may sit above a ground room
        without overlapping it.
<type> is one of: %s
<wall> is one of: %s
<offset> is feet from the wall's start corner (south or west end) to the opening;
        the opening must fit on the wall (offset + width <= wall length).

Windows and entries must be on an EXTERIOR wall (one lying on the envelope edge)
to count for daylight, bedroom egress, or building access.

Example:
  plan "Cedar Ridge"
  envelope 60 x 40
  ceiling 12
  room great_room: living at 0,0 size 28 x 26
  room kitchen: kitchen east-of great_room size 18 x 26
  room master_bed: bedroom north-of great_room size 16 x 11
  door great_room - kitchen width 8
  entry great_room south width 3 offset 20
  window master_bed north width 5 offset 5
""" % (_TYPES, _WALLS)


# --- Lexer ------------------------------------------------------------------


@dataclass
class _Token:
    text: str
    line: int
    col: int  # 1-based
    #: Explicit source span end (1-based, exclusive). Set for quoted tokens,
    #: whose source length differs from len(text) because of the quote chars
    #: and any escape sequences. None -> derive from len(text).
    end: int | None = None
    #: True if this token came from a "..." literal (so an empty one is still
    #: a real, if invalid, token rather than absent).
    quoted: bool = False
    #: True if a quoted token had no closing '"' before end of line.
    unterminated: bool = False

    @property
    def end_col(self) -> int:
        if self.end is not None:
            return self.end
        return self.col + max(1, len(self.text))


_SEPARATORS = set(" \t,:")
_DROP = set("{}")


def _tokenize_line(line: str, lineno: int) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "#":
            break
        if ch in _SEPARATORS or ch in _DROP:
            i += 1
            continue
        if ch == '"':
            start = i
            i += 1
            buf = ""
            while i < n and line[i] != '"':
                if line[i] == "\\" and i + 1 < n and line[i + 1] in '"\\':
                    buf += line[i + 1]
                    i += 2
                    continue
                buf += line[i]
                i += 1
            terminated = i < n
            if terminated:
                i += 1  # consume closing quote
            # Span covers the quotes (and any escapes): from the opening quote
            # through whatever we consumed, so the caret underlines "...".
            tokens.append(
                _Token(
                    buf, lineno, start + 1, end=i + 1, quoted=True,
                    unterminated=not terminated,
                )
            )
            continue
        start = i
        buf = ""
        while i < n and line[i] not in _SEPARATORS and line[i] not in _DROP and line[i] not in '#"':
            buf += line[i]
            i += 1
        tokens.append(_Token(buf, lineno, start + 1))
    return tokens


# --- Parser -----------------------------------------------------------------


class _ParseError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        col: int,
        hint: str | None = None,
        end_col: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.col = col
        self.end_col = end_col
        self.hint = hint


class _Cursor:
    def __init__(self, tokens: list[_Token]):
        self.toks = tokens
        self.i = 0

    @property
    def eol_col(self) -> int:
        return self.toks[-1].end_col if self.toks else 1

    def peek(self) -> _Token | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, what: str) -> _Token:
        t = self.peek()
        if t is None:
            raise _ParseError(
                "SYNTAX", f"Expected {what}.", self.eol_col, end_col=self.eol_col + 1
            )
        self.i += 1
        return t

    def number(self, what: str) -> float:
        t = self.take(what)
        if t.quoted:
            raise _ParseError(
                "BAD_NUMBER",
                f"Expected a number for {what}, not a quoted value.",
                t.col,
                end_col=t.end_col,
                hint="Write the measurement without quotes, e.g. 12.",
            )
        try:
            value = float(t.text)
        except ValueError:
            raise _ParseError(
                "BAD_NUMBER",
                f"Expected a number for {what}, got '{t.text}'.",
                t.col,
                end_col=t.end_col,
            )
        if not math.isfinite(value):
            raise _ParseError(
                "BAD_NUMBER",
                f"Expected a finite number for {what}, got '{t.text}'.",
                t.col,
                end_col=t.end_col,
                hint="Use a plain measurement in feet, e.g. 12 or 10.5.",
            )
        return value

    def level_value(self) -> int:
        """Take a floor-level token: a whole number >= 0 (0 = ground)."""
        t = self.take("a floor level")
        bad = None
        if t.quoted:
            bad = "a quoted value"
        else:
            try:
                v = float(t.text)
            except ValueError:
                bad = f"'{t.text}'"
            else:
                if not math.isfinite(v) or v != int(v) or v < 0:
                    bad = f"'{t.text}'"
        if bad is not None:
            raise _ParseError(
                "BAD_LEVEL",
                f"Floor level must be a whole number >= 0, got {bad}.",
                t.col,
                end_col=t.end_col,
                hint="0 = ground, 1 = the floor above, etc.",
            )
        return int(float(t.text))

    def ident(self, what: str) -> _Token:
        """Take an identifier/name token, rejecting an empty `\"\"` literal."""
        t = self.take(what)
        if t.text == "":
            raise _ParseError(
                "EMPTY_ID",
                f"Expected {what}, got an empty string.",
                t.col,
                end_col=t.end_col,
                hint="Provide a non-empty name.",
            )
        return t

    def keyword(self, expected: str) -> _Token:
        t = self.take(f"'{expected}'")
        if t.text.lower() != expected:
            raise _ParseError(
                "SYNTAX",
                f"Expected '{expected}', got '{t.text}'.",
                t.col,
                end_col=t.end_col,
            )
        return t

    def room_type(self) -> RoomType:
        t = self.take("a room type")
        try:
            return RoomType(t.text.lower())
        except ValueError:
            raise _ParseError(
                "BAD_TYPE",
                f"Unknown room type '{t.text}'.",
                t.col,
                hint=f"Use one of: {_TYPES}.",
                end_col=t.end_col,
            )

    def wall(self) -> Direction:
        t = self.take("a wall")
        try:
            return Direction(t.text.lower())
        except ValueError:
            raise _ParseError(
                "BAD_WALL",
                f"Unknown wall '{t.text}'.",
                t.col,
                hint=f"Use one of: {_WALLS}.",
                end_col=t.end_col,
            )

    def expect_end(self) -> None:
        t = self.peek()
        if t is not None:
            raise _ParseError(
                "EXTRA_TOKENS",
                f"Unexpected '{t.text}' at end of statement.",
                t.col,
                hint="Remove the extra token(s).",
                end_col=t.end_col,
            )


@dataclass
class _SourceMap:
    room_line: dict[str, int] = field(default_factory=dict)
    # room id -> (col, end_col) of its id token, for column-accurate carets.
    room_col: dict[str, tuple[int, int]] = field(default_factory=dict)


#: Relative-placement keywords -> the add_room anchor kwarg they map to.
_PLACEMENT = {
    "east-of": "east_of", "east_of": "east_of", "right-of": "east_of",
    "west-of": "west_of", "west_of": "west_of", "left-of": "west_of",
    "north-of": "north_of", "north_of": "north_of", "above": "north_of",
    "south-of": "south_of", "south_of": "south_of", "below": "south_of",
}


def _parse_placement(c: "_Cursor") -> tuple[dict, "_Token | None"]:
    """Parse a room's position: ``at <x>,<y>`` or ``<dir> <ref_room>``.

    Returns ``(add_room_kwargs, ref_token)`` — kwargs are either ``{x, y}`` or a
    single anchor like ``{east_of: 'kitchen'}``; ref_token is the reference id
    token (for error locations) or None for absolute placement.
    """
    nxt = c.peek()
    if nxt is not None and nxt.text.lower() == "at":
        c.keyword("at")
        x = c.number("x")
        y = c.number("y")
        return {"x": x, "y": y}, None

    # One or two relative anchors. Two must be on different axes (one of
    # east/west, one of north/south) — that pins a room into a corner/pocket.
    kwargs: dict = {}
    first_ref: _Token | None = None
    while c.peek() is not None and c.peek().text.lower() in _PLACEMENT:
        dir_tok = c.take("a placement")
        rel = _PLACEMENT[dir_tok.text.lower()]
        nxt = c.peek()
        reserved = ("size", "align", "offset")
        if nxt is None or nxt.text.lower() in reserved or nxt.text.lower() in _PLACEMENT:
            raise _ParseError(
                "BAD_PLACEMENT",
                f"Expected a reference room id after '{dir_tok.text}'.",
                dir_tok.col,
                end_col=dir_tok.end_col,
                hint=f"Name the room to abut, e.g. `{dir_tok.text} living`.",
            )
        ref_tok = c.ident("a reference room id")
        if rel in kwargs:
            raise _ParseError(
                "BAD_PLACEMENT",
                f"Repeated '{dir_tok.text}' placement.",
                dir_tok.col,
                end_col=dir_tok.end_col,
                hint="Use at most one east/west and one north/south anchor.",
            )
        kwargs[rel] = ref_tok.text
        if first_ref is None:
            first_ref = ref_tok

    if not kwargs:
        t = c.peek()
        col = t.col if t else c.eol_col
        end = t.end_col if t else c.eol_col + 1
        raise _ParseError(
            "BAD_PLACEMENT",
            "Expected a placement: 'at <x>,<y>' or a relative anchor.",
            col,
            end_col=end,
            hint="e.g. `at 0,0`, `east-of living`, or `east-of a north-of b`.",
        )

    # Optional slide along the shared wall: `align near|far|center` and `offset <n>`.
    while c.peek() is not None and c.peek().text.lower() in ("align", "offset"):
        opt = c.take("an option").text.lower()
        if opt == "align":
            a = c.take("near, far, or center")
            if a.text.lower() not in ("near", "far", "center"):
                raise _ParseError(
                    "BAD_PLACEMENT",
                    f"Unknown alignment '{a.text}'.",
                    a.col,
                    end_col=a.end_col,
                    hint="Use align near, far, or center.",
                )
            kwargs["align"] = a.text.lower()
        else:
            kwargs["offset"] = c.number("offset")
    return kwargs, first_ref


def _parse_statement(
    tokens: list[_Token], plan: Barndominium, smap: _SourceMap, lineno: int
) -> None:
    c = _Cursor(tokens)
    kw = c.take("a statement keyword")
    key = kw.text.lower()

    if key == "plan":
        plan.name = c.ident("a plan name").text
        c.expect_end()
    elif key == "envelope":
        w = c.number("envelope width")
        c.keyword("x")
        length = c.number("envelope length")
        plan.envelope(w, length)
        c.expect_end()
    elif key == "wing":
        w = c.number("wing width")
        c.keyword("x")
        length = c.number("wing length")
        c.keyword("at")
        x = c.number("wing x")
        y = c.number("wing y")
        c.expect_end()
        plan.wing(w, length, x=x, y=y)
    elif key == "ceiling":
        plan.ceiling(c.number("ceiling height"))
        c.expect_end()
    elif key == "note":
        plan.note(c.take("a quoted note").text)
        c.expect_end()
    elif key == "room":
        rid_tok = c.ident("a room id")
        rid = rid_tok.text
        rtype = c.room_type()
        place_kwargs, ref_tok = _parse_placement(c)
        c.keyword("size")
        w = c.number("width")
        c.keyword("x")
        length = c.number("length")
        level = 0
        if c.peek() is not None and c.peek().text.lower() == "level":
            c.keyword("level")
            level = c.level_value()
        c.expect_end()
        try:
            plan.add_room(rid, rtype, width=w, length=length, level=level, **place_kwargs)
        except ValueError as exc:
            col = ref_tok.col if ref_tok else rid_tok.col
            end = ref_tok.end_col if ref_tok else rid_tok.end_col
            raise _ParseError(
                "PLACE_REF",
                str(exc),
                col,
                end_col=end,
                hint="Define the reference room before placing relative to it.",
            )
        smap.room_line[rid] = lineno
        smap.room_col[rid] = (rid_tok.col, rid_tok.end_col)
    elif key == "door":
        a_tok = c.ident("the first room id")
        a = a_tok.text
        sep = c.take("'-' or 'to'")
        if sep.text.lower() not in ("-", "to"):
            raise _ParseError(
                "SYNTAX",
                f"Expected '-' or 'to', got '{sep.text}'.",
                sep.col,
                end_col=sep.end_col,
            )
        b = c.ident("the second room id").text
        width = 32 / 12
        nxt = c.peek()
        if nxt is not None:
            c.keyword("width")
            width = c.number("door width")
        c.expect_end()
        plan.connect(a, b, width=width)
        door = plan.interior_doors[-1]
        door.line, door.col, door.end_col = lineno, a_tok.col, a_tok.end_col
    elif key == "open":
        a_tok = c.ident("the first room id")
        a = a_tok.text
        sep = c.take("'-' or 'to'")
        if sep.text.lower() not in ("-", "to"):
            raise _ParseError(
                "SYNTAX",
                f"Expected '-' or 'to', got '{sep.text}'.",
                sep.col,
                end_col=sep.end_col,
            )
        b = c.ident("the second room id").text
        width = 6.0  # wide cased opening by default; matches DEFAULT_OPENING_WIDTH
        nxt = c.peek()
        if nxt is not None:
            c.keyword("width")
            width = c.number("opening width")
        c.expect_end()
        plan.opening(a, b, width=width)
        door = plan.interior_doors[-1]
        door.line, door.col, door.end_col = lineno, a_tok.col, a_tok.end_col
    elif key == "entry":
        rid_tok = c.ident("a room id")
        rid = rid_tok.text
        wall = c.wall()
        width, offset, egress = 3.0, 1.0, True
        while c.peek() is not None:
            opt = c.take("an option").text.lower()
            if opt == "width":
                width = c.number("door width")
            elif opt == "offset":
                offset = c.number("offset")
            elif opt in ("no-egress", "nonegress"):
                egress = False
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown entry option '{opt}'.",
                    c.toks[c.i - 1].col,
                    hint="Options: width <n>, offset <n>, no-egress.",
                    end_col=c.toks[c.i - 1].end_col,
                )
        plan.entrance(rid, wall, width=width, offset=offset, egress=egress)
        ed = plan.exterior_doors[-1]
        ed.line, ed.col, ed.end_col = lineno, rid_tok.col, rid_tok.end_col
    elif key == "window":
        rid_tok = c.ident("a room id")
        rid = rid_tok.text
        wall = c.wall()
        width, offset = 4.0, 2.0
        while c.peek() is not None:
            opt = c.take("an option").text.lower()
            if opt == "width":
                width = c.number("window width")
            elif opt == "offset":
                offset = c.number("offset")
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown window option '{opt}'.",
                    c.toks[c.i - 1].col,
                    hint="Options: width <n>, offset <n>.",
                    end_col=c.toks[c.i - 1].end_col,
                )
        plan.add_window(rid, wall, width=width, offset=offset)
        win = plan.windows[-1]
        win.line, win.col, win.end_col = lineno, rid_tok.col, rid_tok.end_col
    elif key == "porch":
        pid = c.ident("a porch id").text
        c.keyword("at")
        x = c.number("x")
        y = c.number("y")
        c.keyword("size")
        w = c.number("width")
        c.keyword("x")
        length = c.number("length")
        covered = True
        nxt = c.peek()
        if nxt is not None:
            tag = c.take("'covered' or 'open'").text.lower()
            if tag == "open":
                covered = False
            elif tag != "covered":
                raise _ParseError(
                    "SYNTAX",
                    f"Expected 'covered' or 'open', got '{tag}'.",
                    nxt.col,
                    end_col=nxt.end_col,
                )
        c.expect_end()
        plan.add_porch(pid, x=x, y=y, width=w, length=length, covered=covered)
    elif key == "stair":
        sid_tok = c.ident("a stair id")
        c.keyword("at")
        x = c.number("x")
        y = c.number("y")
        c.keyword("size")
        w = c.number("width")
        c.keyword("x")
        length = c.number("length")
        lo, hi = 0, 1
        while c.peek() is not None and c.peek().text.lower() in ("from", "to"):
            opt = c.take("an option").text.lower()
            if opt == "from":
                lo = c.level_value()
            else:
                hi = c.level_value()
        c.expect_end()
        try:
            plan.add_stair(
                sid_tok.text, x=x, y=y, width=w, length=length,
                from_level=lo, to_level=hi,
            )
        except ValueError as exc:
            raise _ParseError(
                "BAD_LEVEL", str(exc), sid_tok.col, end_col=sid_tok.end_col,
                hint="A stair connects two different levels, e.g. `from 0 to 1`.",
            )
    else:
        raise _ParseError(
            "UNKNOWN_STMT",
            f"Unknown statement '{kw.text}'.",
            kw.col,
            hint=f"Statements start with one of: {', '.join(_KEYWORDS)}.",
            end_col=kw.end_col,
        )


# --- Result -----------------------------------------------------------------


@dataclass
class CompileResult:
    """The output of :func:`compile_source`."""

    plan: Barndominium | None
    diagnostics: list[Issue]
    source: str

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.diagnostics if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.diagnostics if i.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.diagnostics if i.severity is Severity.INFO]

    @property
    def ok(self) -> bool:
        """True when the source compiled and passed all code checks."""
        return self.plan is not None and not self.errors

    def summary(self) -> str:
        e, w, n = len(self.errors), len(self.warnings), len(self.infos)
        if self.plan is None:
            return f"COMPILE FAILED — {e} error(s)"
        status = "OK" if self.ok else "FAILED"
        return f"COMPILE {status} — {e} error(s), {w} warning(s), {n} info(s)"

    def report(self, filename: str = "<plan>") -> str:
        """Compiler-style diagnostic listing with column-accurate carets."""
        src_lines = self.source.splitlines()
        lines = [self.summary()]
        for d in sorted(self.diagnostics, key=lambda i: (i.line or 0, i.col or 0)):
            lines.extend(_format_diagnostic(d, filename, src_lines))
        return "\n".join(lines)


def _format_diagnostic(d: Issue, filename: str, src_lines: list[str]) -> list[str]:
    """Render one diagnostic: header, source snippet, caret underline, hint."""
    if d.line and d.col:
        loc = f"{filename}:{d.line}:{d.col}"
    elif d.line:
        loc = f"{filename}:{d.line}"
    else:
        loc = filename
    where = f" ({d.room})" if d.room is not None else ""
    out = [f"{loc}: {d.severity.value}[{d.code}]{where}: {d.message}"]

    if d.line and 1 <= d.line <= len(src_lines):
        # Expand tabs to single spaces so the (char-based) caret stays aligned.
        snippet = src_lines[d.line - 1].replace("\t", " ")
        out.append(f"    {snippet}")
        if d.col:
            width = max(1, (d.end_col or d.col + 1) - d.col)
            caret = " " * (d.col - 1) + "^" + "~" * (width - 1)
            out.append(f"    {caret.rstrip()}")

    if d.hint:
        out.append(f"    hint: {d.hint}")
    return out


def compile_source(source: str, name: str | None = None) -> CompileResult:
    """Compile DSL ``source`` into a validated plan + diagnostics."""
    diagnostics: list[Issue] = []
    plan = Barndominium(name=name or "Untitled")
    smap = _SourceMap()

    for lineno, raw in enumerate(source.splitlines(), start=1):
        toks = _tokenize_line(raw, lineno)
        unterminated = next((t for t in toks if t.unterminated), None)
        if unterminated is not None:
            diagnostics.append(
                Issue(
                    Severity.ERROR,
                    "UNTERMINATED_STRING",
                    "String literal has no closing '\"'.",
                    line=lineno,
                    col=unterminated.col,
                    end_col=unterminated.end_col,
                    hint='Add the closing quote, e.g. `plan "Name"`.',
                )
            )
            continue
        if not toks:
            # A line of only separators/punctuation (e.g. ":::") tokenizes to
            # nothing; flag it rather than silently dropping a typo'd statement.
            stripped = raw.split("#", 1)[0]
            residue = [ch for ch in stripped if not ch.isspace() and ch not in _DROP]
            if residue:
                col = next(i for i, ch in enumerate(stripped) if not ch.isspace()) + 1
                diagnostics.append(
                    Issue(
                        Severity.ERROR,
                        "SYNTAX",
                        "Line has no statement keyword.",
                        line=lineno,
                        col=col,
                        end_col=col + 1,
                        hint=f"Each line is one statement; start with one of: "
                        f"{', '.join(_KEYWORDS)}.",
                    )
                )
            continue
        try:
            _parse_statement(toks, plan, smap, lineno)
        except _ParseError as err:
            diagnostics.append(
                Issue(
                    Severity.ERROR,
                    err.code,
                    err.message,
                    line=lineno,
                    col=err.col,
                    end_col=err.end_col,
                    hint=err.hint,
                )
            )

    # If parsing failed, stop here — like a compiler that won't typecheck a
    # program that doesn't parse. Fix syntax first.
    if any(d.severity is Severity.ERROR for d in diagnostics):
        return CompileResult(None, diagnostics, source)

    report: ValidationReport = validate(plan)
    for iss in report.issues:
        # Anchor semantic diagnostics to the room's `room ...` line, and point
        # the caret at the room's id token, so quality/code-check issues get the
        # same column-accurate underline as syntax errors.
        if iss.room is not None:
            if iss.line is None:
                iss.line = smap.room_line.get(iss.room)
            if iss.col is None and iss.room in smap.room_col:
                iss.col, iss.end_col = smap.room_col[iss.room]
    diagnostics.extend(report.issues)
    return CompileResult(plan, diagnostics, source)


def compile_file(path: str) -> CompileResult:
    """Compile a ``.barn`` file."""
    with open(path, encoding="utf-8") as fh:
        return compile_source(fh.read())
