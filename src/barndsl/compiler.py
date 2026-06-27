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
    ceiling <H>
    note "free text"
    room <id>: <type> at <x>,<y> size <W> x <L>
    door <id_a> - <id_b> [width <w>]
    entry <id> <wall> [width <w>] [offset <o>] [no-egress]
    window <id> <wall> [width <w>] [offset <o>]
    porch <id> at <x>,<y> size <W> x <L> [covered|open]

Coordinates are in feet; origin (0,0) is the south-west corner, x→east, y→north.
``<type>`` is a RoomType value (living, kitchen, bedroom, bathroom, hallway,
shop, …); ``<wall>`` is north|south|east|west.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .elements import Barndominium, Direction, RoomType
from .validation import Issue, Severity, ValidationReport, validate

# Statement keywords, for "unknown statement" hints.
_KEYWORDS = (
    "plan", "envelope", "ceiling", "note", "room", "door", "entry", "window", "porch"
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
and [y, y+L] south-north (south wall=y, north=y+L, west=x, east=x+L).

Statements:
  plan "Name"
  envelope <W> x <L>              # overall footprint
  ceiling <H>                     # ceiling height (>= 7; 9-12 typical)
  note "free text"                # optional design note
  room <id>: <type> at <x>,<y> size <W> x <L>
  door <id_a> - <id_b> [width <w>]            # interior door (rooms must share a wall)
  entry <id> <wall> [width <w>] [offset <o>] [no-egress]   # exterior door
  window <id> <wall> [width <w>] [offset <o>]              # exterior window
  porch <id> at <x>,<y> size <W> x <L> [covered|open]

<type> is one of: %s
<wall> is one of: %s
<offset> is feet from the wall's start corner (south or west end) to the opening.

Example:
  plan "Cedar Ridge"
  envelope 60 x 40
  ceiling 12
  room great_room: living at 0,0 size 28 x 26
  room kitchen: kitchen at 28,14 size 18 x 12
  room master_bed: bedroom at 0,29 size 16 x 11
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

    @property
    def end_col(self) -> int:
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
                buf += line[i]
                i += 1
            if i < n:
                i += 1  # consume closing quote
            tokens.append(_Token(buf, lineno, start + 1))
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
    def __init__(self, code: str, message: str, col: int, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.col = col
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
            raise _ParseError("SYNTAX", f"Expected {what}.", self.eol_col)
        self.i += 1
        return t

    def number(self, what: str) -> float:
        t = self.take(what)
        try:
            return float(t.text)
        except ValueError:
            raise _ParseError(
                "BAD_NUMBER", f"Expected a number for {what}, got '{t.text}'.", t.col
            )

    def keyword(self, expected: str) -> _Token:
        t = self.take(f"'{expected}'")
        if t.text.lower() != expected:
            raise _ParseError(
                "SYNTAX",
                f"Expected '{expected}', got '{t.text}'.",
                t.col,
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
            )

    def expect_end(self) -> None:
        t = self.peek()
        if t is not None:
            raise _ParseError(
                "EXTRA_TOKENS",
                f"Unexpected '{t.text}' at end of statement.",
                t.col,
                hint="Remove the extra token(s).",
            )


@dataclass
class _SourceMap:
    room_line: dict[str, int] = field(default_factory=dict)


def _parse_statement(
    tokens: list[_Token], plan: Barndominium, smap: _SourceMap, lineno: int
) -> None:
    c = _Cursor(tokens)
    kw = c.take("a statement keyword")
    key = kw.text.lower()

    if key == "plan":
        plan.name = c.take("a plan name").text
        c.expect_end()
    elif key == "envelope":
        w = c.number("envelope width")
        c.keyword("x")
        length = c.number("envelope length")
        plan.envelope(w, length)
        c.expect_end()
    elif key == "ceiling":
        plan.ceiling(c.number("ceiling height"))
        c.expect_end()
    elif key == "note":
        plan.note(c.take("a quoted note").text)
        c.expect_end()
    elif key == "room":
        rid = c.take("a room id").text
        rtype = c.room_type()
        c.keyword("at")
        x = c.number("x")
        y = c.number("y")
        c.keyword("size")
        w = c.number("width")
        c.keyword("x")
        length = c.number("length")
        c.expect_end()
        plan.add_room(rid, rtype, x=x, y=y, width=w, length=length)
        smap.room_line[rid] = lineno
    elif key == "door":
        a = c.take("the first room id").text
        sep = c.take("'-' or 'to'")
        if sep.text.lower() not in ("-", "to"):
            raise _ParseError("SYNTAX", f"Expected '-' or 'to', got '{sep.text}'.", sep.col)
        b = c.take("the second room id").text
        width = 2.67
        nxt = c.peek()
        if nxt is not None:
            c.keyword("width")
            width = c.number("door width")
        c.expect_end()
        plan.connect(a, b, width=width)
    elif key == "entry":
        rid = c.take("a room id").text
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
                )
        plan.entrance(rid, wall, width=width, offset=offset, egress=egress)
    elif key == "window":
        rid = c.take("a room id").text
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
                )
        plan.add_window(rid, wall, width=width, offset=offset)
    elif key == "porch":
        pid = c.take("a porch id").text
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
                raise _ParseError("SYNTAX", f"Expected 'covered' or 'open', got '{tag}'.", nxt.col)
        c.expect_end()
        plan.add_porch(pid, x=x, y=y, width=w, length=length, covered=covered)
    else:
        raise _ParseError(
            "UNKNOWN_STMT",
            f"Unknown statement '{kw.text}'.",
            kw.col,
            hint=f"Statements start with one of: {', '.join(_KEYWORDS)}.",
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
    def ok(self) -> bool:
        """True when the source compiled and passed all code checks."""
        return self.plan is not None and not self.errors

    def summary(self) -> str:
        e, w = len(self.errors), len(self.warnings)
        if self.plan is None:
            return f"COMPILE FAILED — {e} error(s)"
        status = "OK" if self.ok else "FAILED"
        return f"COMPILE {status} — {e} error(s), {w} warning(s)"

    def report(self, filename: str = "<plan>") -> str:
        """Compiler-style diagnostic listing."""
        lines = [self.summary()]
        for d in sorted(self.diagnostics, key=lambda i: (i.line or 0)):
            loc = f"{filename}:{d.line}" if d.line else filename
            where = f" ({d.room})" if d.room else ""
            lines.append(f"{loc}: {d.severity.value}[{d.code}]{where}: {d.message}")
            if d.hint:
                lines.append(f"    hint: {d.hint}")
        return "\n".join(lines)


def compile_source(source: str, name: str | None = None) -> CompileResult:
    """Compile DSL ``source`` into a validated plan + diagnostics."""
    diagnostics: list[Issue] = []
    plan = Barndominium(name=name or "Untitled")
    smap = _SourceMap()

    for lineno, raw in enumerate(source.splitlines(), start=1):
        toks = _tokenize_line(raw, lineno)
        if not toks:
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
                    hint=err.hint,
                )
            )

    # If parsing failed, stop here — like a compiler that won't typecheck a
    # program that doesn't parse. Fix syntax first.
    if any(d.severity is Severity.ERROR for d in diagnostics):
        return CompileResult(None, diagnostics, source)

    report: ValidationReport = validate(plan)
    for iss in report.issues:
        if iss.line is None and iss.room is not None:
            iss.line = smap.room_line.get(iss.room)
    diagnostics.extend(report.issues)
    return CompileResult(plan, diagnostics, source)


def compile_file(path: str) -> CompileResult:
    """Compile a ``.barn`` file."""
    with open(path, encoding="utf-8") as fh:
        return compile_source(fh.read())
