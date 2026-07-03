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
    require adjacent|separate <a> <b>  # declared spatial intent, checked vs the plan
    require exterior <room> [<wall>]   # (also: require area <room> >= <sqft>)
    room <id>: <type> <placement> size <W> x <L> [level <n>]
    wall <id_a> - <id_b> plumbing|bearing|rated   # attribute(s) of the shared wall
    door <id_a> - <id_b> [width <w>] [offset <o>] [into <room>] [hinge near|far]
    door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]  # sectional garage door
    open <id_a> - <id_b> [width <w>] [offset <o>]   # cased opening / walk-through (no leaf)
    entry <id> <wall> [double|french] [width <w>] [offset <o>] [no-egress]
    window <id> <wall> [casement|slider|fixed|double-hung] [width <w>] [offset <o>] [sill <s>] [head <h>]
    porch <id> at <x>,<y> size <W> x <L> [covered|open]
    stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]
    frame [bay <ft>] [span <ft>] [post <in>] [no-ridge]   # auto post-and-beam frame
    roof gable|shed|monitor [pitch <rise:run>]            # optional roof form (default gable)

``<placement>`` is ``at <x>,<y>`` (absolute), ``east-of|west-of|north-of|
south-of <room>`` (abut an already-defined room), or one of each to pin a corner.

Coordinates are in feet; origin (0,0) is the south-west corner, x→east, y→north.
``<type>`` is a RoomType value (living, kitchen, bedroom, bathroom, hallway,
shop, …); ``<wall>`` is north|south|east|west.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .elements import (
    DEFAULT_DOUBLE_DOOR_WIDTH,
    DOOR_KINDS,
    DOUBLE_LEAF_KINDS,
    OVERHEAD_DOOR_HEIGHT,
    OVERHEAD_DOOR_WIDTH,
    WALL_ATTRIBUTES,
    WINDOW_KINDS,
    Barndominium,
    Direction,
    FrameSpec,
    RoomType,
    inches,
)
from .validation import Issue, Severity, ValidationReport, validate

if TYPE_CHECKING:  # the annotation-only import; runtime resolution is lazy
    from .profiles import Profile

# Statement keywords, for "unknown statement" hints.
_KEYWORDS = (
    "plan", "envelope", "wing", "ceiling", "floor", "note", "program", "require",
    "room", "wall", "door", "open", "entry", "window", "porch", "stair", "frame",
    "roof", "orientation", "finish", "accessible", "site", "setback", "suite",
    "zone", "electrical", "street", "overhang", "climate"
)
_TYPES = ", ".join(t.value for t in RoomType)
_WALLS = "north, south, east, west"

_DOOR_KINDS = frozenset(DOOR_KINDS)
_WINDOW_KIND_SET = frozenset(WINDOW_KINDS)
_WALL_ATTRS = ", ".join(WALL_ATTRIBUTES)
_BED_WORDS = frozenset({"bed", "beds", "bedroom", "bedrooms"})
#: 'bath' is an aggregate (bathroom + half_bath), matching the compile recap.
_BATH_WORDS = frozenset({"bath", "baths", "bathroom", "bathrooms"})


def _program_noun(text: str) -> "str | RoomType | None":
    """Resolve a `program` noun to 'bed', 'bath', a :class:`RoomType`, or None.

    'bed'/'bath' are exact-count categories; any other room type (singular, or
    with a trailing plural 's') becomes an at-least requirement.
    """
    t = text.lower()
    if t in _BED_WORDS:
        return "bed"
    if t in _BATH_WORDS:
        return "bath"
    for cand in (t, t[:-1] if t.endswith("s") else t):
        try:
            return RoomType(cand)
        except ValueError:
            continue
    return None

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
  floor <D>                       # inter-floor assembly depth (ft); floor-to-floor = ceiling + this
  accessible                      # opt-in: run accessibility / aging-in-place nudges
  electrical                      # opt-in: emit the electrical / life-safety checklist reminder
  note "free text"                # optional design note
  program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>] [storage <sqft>]  # optional intent, checked vs the rooms
                                  #   bed/bath = exact counts; other types = at-least; area = min interior;
                                  #   storage = min closet+pantry sq ft
  require adjacent <room_a> <room_b>    # the two rooms must share a wall
  require separate <room_a> <room_b>    # the two rooms must NOT share a wall
  require exterior <room> [<wall>]      # the room needs an exterior wall (optionally that side)
  require area <room> >= <sqft>         # the room's nominal area must be at least sqft
        # declared spatial intent, like `program`: each unmet requirement is a
        # REQUIRE_UNMET warning (never blocking); an unknown room id is an error.
  room <id>: <type> <placement> size <W> x <L> [level <n>] [ceiling <h>] [vaulted]
        # `ceiling <h>` overrides the plan ceiling for this room (a tray or a
        # taller great room); `vaulted` makes it open to the roof (no flat ceiling).
  wall <id_a> - <id_b> plumbing|bearing|rated   # one or more attributes
        # declared attributes of the SHARED wall between two abutting rooms:
        # plumbing = a 2x6 wet wall (satisfies the wet-room grouping nudge when a
        # wet room backs onto it); bearing = an interior bearing wall the auto
        # `frame` uses as a post line (when it runs along the building's long
        # axis); rated = a fire-separation wall (verifies, and silences, the
        # garage/dwelling separation reminder). The rooms must share a wall.
  suite <id>: <room> ...          # group rooms that read as one unit
        # e.g. `suite primary: master_bed master_bath master_wic`. Members are
        # room ids. Declared membership sharpens the design checks where the
        # geometry backs it up: MASTER_ENSUITE (suite bath reachable via suite
        # doors), BED_SOUND (a suite of exactly the two beds), BED_PRIVACY, and
        # ENTRY_PRIVATE (sole-bedroom suite = primary). An unknown member is a
        # SUITE_REF error; a room in two suites a SUITE_OVERLAP warning.
  zone <id>: <member> ...         # group rooms/suites into a band (private wing, public core)
        # e.g. `zone private: primary bed_2 bed_3 hall_beds`. Members are room
        # ids OR suite ids. An unknown member is a ZONE_REF error; a room in two
        # zones a ZONE_OVERLAP warning; a public room stranded in an otherwise
        # private zone (or vice versa) a ZONE_CROSS info.
  door <id_a> - <id_b> [swing|cased|pocket|sliding|double|french] [width <w>] [offset <o>] [into <room>] [hinge near|far]
        # interior door between two rooms. swing (default) hinges; cased = an open
        # walk-through (no leaf); pocket/sliding slide; double/french = a pair of
        # half-width leaves (default 5 ft total). offset = ft from the wall's
        # S/W end; `into <room>` + `hinge near|far` set the swing side/hinge.
  door <id> <wall> exterior [double|french] [width <w>] [offset <o>] [no-egress]  # exterior door, on an exterior wall
  door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]
        # overhead/sectional garage door on a garage/shop's exterior wall. Defaults
        # 9 x 7 (the residential single); `width 16` is a double. Never an egress
        # door and not a building entrance — the plan still needs a people-door.
  open <id_a> - <id_b> [width <w>] [offset <o>]            # shorthand for `door <a> - <b> cased ...`
  entry <id> <wall> [double|french] [width <w>] [offset <o>] [no-egress]
        # shorthand for `door <id> <wall> exterior ...`; double/french = a pair of
        # half-width leaves (egress clear width counts ONE leaf, IRC R311.2)
  window <id> <wall> [casement|slider|fixed|double-hung] [width <w>] [offset <o>] [sill <s>] [head <h>]
        # window; sill/head are ft above the floor. The kind (default casement)
        # sets the escape-opening math: a casement opens ~its full glazed size, a
        # slider opens ~half its width, a double-hung ~half its height, and FIXED
        # glass never counts for bedroom egress (it still daylights).
  porch <id> at <x>,<y> size <W> x <L> [covered|open]
  stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]
        # vertical circulation; defaults from 0 to 1. Place its footprint over a
        # room on each level so it links them (and makes the upper floor reachable).
  frame [bay <ft>] [span <ft>] [post <in>] [no-ridge]
        # auto-place the post-and-beam structural frame over the footprint: bents
        # spaced <= bay ft along the long axis (default 12), each spanning the short
        # axis, interior support posts where that span exceeds <span> ft (default
        # 40), a ridge member over them (no-ridge omits it). post = nominal section
        # in inches (default 6). A layout aid, not an engineered design.
  overhang <ft>                    # roof eave/rake projection past the walls (0 = flush; 1–2 ft typical)
  climate <zone>                   # IECC climate zone 1-8 -> envelope R-value guidance + WWR check
  roof gable|shed|monitor [pitch <rise:run>]
        # the roof form over the building: gable (default, ridge down the long
        # axis), shed (a single slope), or monitor (a raised centre clerestory
        # aisle). pitch is rise:run (e.g. 0.333 for 4:12).
  orientation <degrees>            # compass azimuth that plan-north (+y) points (0 = true north)
  street <wall>                    # the wall (n|s|e|w) that faces the street/approach → approach nudges
  finish [siding "<name>"] [roof "<name>"]  # exterior material hints (e.g. metal siding, standing-seam)
  site <W> x <L>                   # optional lot dimensions in feet (east-west x north-south)
  setback [front <n>] [side <n>] [rear <n>]  # required yard setbacks (feet); needs a `site`
        # the buildable rectangle is the lot minus its setbacks: front/rear
        # consume the plan's south/north depth, `side` clears BOTH east & west
        # edges. If the building footprint (envelope + wings + porches) doesn't
        # fit inside it, that's a SETBACK error (checked by dimensions only —
        # there is no lot-position statement). A `setback` with no `site` errors.

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

    def count(self, what: str) -> int:
        """Take a count: a whole number >= 0."""
        t = self.take(what)
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
                "BAD_COUNT",
                f"Expected a whole number >= 0 for {what}, got {bad}.",
                t.col,
                end_col=t.end_col,
                hint="Use a plain count, e.g. 3.",
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
            # A common slip: `align`/`offset` (the slide-along-the-wall modifiers)
            # belong on the relative anchor, *before* `size` — not at the end.
            if t.text.lower() in ("align", "offset", "near", "far", "center"):
                hint = (
                    "`align`/`offset` go on the relative anchor, before `size` "
                    "(e.g. `room x: bedroom east-of y align far size 12 x 11`)."
                )
            else:
                hint = "Remove the extra token(s)."
            raise _ParseError(
                "EXTRA_TOKENS",
                f"Unexpected '{t.text}' at end of statement.",
                t.col,
                hint=hint,
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
    while (tok := c.peek()) is not None and tok.text.lower() in _PLACEMENT:
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
    while (tok := c.peek()) is not None and tok.text.lower() in ("align", "offset"):
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
    elif key == "floor":
        plan.floors(c.number("floor assembly depth"))
        c.expect_end()
    elif key == "accessible":
        plan.mark_accessible()
        c.expect_end()
    elif key == "electrical":
        plan.mark_electrical()
        c.expect_end()
    elif key == "street":
        street_wall = c.wall()
        c.expect_end()
        plan.set_street(street_wall)
    elif key == "overhang":
        plan.set_overhang(c.number("the overhang depth in feet"))
        c.expect_end()
    elif key == "climate":
        tok = c.peek()
        czone = c.number("the IECC climate zone (1-8)")
        if czone != int(czone) or not 1 <= int(czone) <= 8:
            raise _ParseError(
                "BAD_OPTION",
                f"climate zone must be an IECC zone 1-8, got {czone:g}.",
                tok.col if tok else 1,
                end_col=tok.end_col if tok else None,
                hint="Use a whole number 1 (warmest) to 8 (coldest).",
            )
        c.expect_end()
        plan.set_climate(int(czone))
    elif key == "orientation":
        # `orientation <degrees>` — azimuth (clockwise from N) that plan-north points.
        plan.orient(c.number("the orientation in degrees"))
        c.expect_end()
    elif key == "finish":
        # `finish [siding "<name>"] [roof "<name>"]` — exterior material hints.
        siding = roofing = None
        while (tok := c.peek()) is not None:
            opt = c.take("'siding' or 'roof'").text.lower()
            if opt == "siding":
                siding = c.take("a siding material (quoted)").text
            elif opt == "roof":
                roofing = c.take("a roof material (quoted)").text
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown finish option '{opt}'.",
                    tok.col,
                    end_col=tok.end_col,
                    hint='Use `finish siding "..." roof "..."`.',
                )
        c.expect_end()
        plan.finish(siding=siding, roof=roofing)
    elif key == "site":
        # `site <W> x <L>` — the lot's east-west x north-south dimensions (feet).
        w = c.number("site width")
        c.keyword("x")
        length = c.number("site length")
        c.expect_end()
        plan.site(w, length)
        ss = plan.site_spec
        assert ss is not None  # .site() just created it
        ss.line, ss.col, ss.end_col = lineno, kw.col, kw.end_col
    elif key == "setback":
        # `setback [front <n>] [side <n>] [rear <n>]` — any subset, in any order.
        front = side = rear = None
        while (tok := c.peek()) is not None:
            opt = c.take("'front', 'side', or 'rear'").text.lower()
            if opt == "front":
                front = c.number("the front setback")
            elif opt == "side":
                side = c.number("the side setback")
            elif opt == "rear":
                rear = c.number("the rear setback")
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown setback edge '{tok.text}'.",
                    tok.col,
                    end_col=tok.end_col,
                    hint="Use `setback front <n> side <n> rear <n>` (any subset; "
                    "`side` applies to both the east and west edges).",
                )
        if front is None and side is None and rear is None:
            raise _ParseError(
                "SYNTAX",
                "A `setback` needs at least one of front/side/rear.",
                c.eol_col,
                end_col=c.eol_col + 1,
                hint="e.g. `setback front 25 side 10 rear 20`.",
            )
        c.expect_end()
        plan.setback(front=front, side=side, rear=rear)
        ss = plan.site_spec
        assert ss is not None  # .setback() just created it
        ss.setback_line, ss.setback_col, ss.setback_end_col = lineno, kw.col, kw.end_col
    elif key == "roof":
        # `roof <style> [pitch <p>]` — style in gable|shed|monitor.
        style_tok = c.take("a roof style (gable|shed|monitor)")
        pitch = None
        if (tok := c.peek()) is not None and tok.text.lower() == "pitch":
            c.keyword("pitch")
            pitch = c.number("the roof pitch (rise:run)")
        c.expect_end()
        try:
            plan.roof(style_tok.text.lower(), pitch=pitch)
        except ValueError as exc:
            raise _ParseError(
                "BAD_OPTION",
                str(exc),
                style_tok.col,
                end_col=style_tok.end_col,
                hint="Use `roof gable`, `roof shed`, or `roof monitor` "
                "(optionally `pitch <rise:run>`).",
            )
    elif key == "note":
        plan.note(c.take("a quoted note").text)
        c.expect_end()
    elif key == "program":
        # `program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]`.
        # The first clause (bed) is mandatory; the rest are any order. bed/bath
        # are exact-count categories; other room types are at-least requirements.
        beds = c.count("the bedroom count")
        unit = c.take("'bed'")
        if _program_noun(unit.text) != "bed":
            raise _ParseError(
                "SYNTAX",
                f"Expected 'bed', got '{unit.text}'.",
                unit.col,
                end_col=unit.end_col,
                hint="The program starts with a bedroom count, e.g. `program 3 bed`.",
            )
        baths = None
        requires: dict[RoomType | str, int] = {}
        min_area = None
        min_storage = None
        while (tok := c.peek()) is not None:
            if tok.text.lower() == "area":
                c.take("area")
                min_area = c.number("the minimum area")
                continue
            if tok.text.lower() == "storage":
                c.take("storage")
                min_storage = c.number("the minimum storage area")
                continue
            n = c.count("a room count")
            noun = c.take("a room type")
            cat = _program_noun(noun.text)
            if cat is None:
                raise _ParseError(
                    "BAD_TYPE",
                    f"Unknown program room type '{noun.text}'.",
                    noun.col,
                    end_col=noun.end_col,
                    hint=f"Use 'bed', 'bath', 'area', 'storage', or a room type: {_TYPES}.",
                )
            if cat == "bed":
                beds = n
            elif cat == "bath":
                baths = n
            else:
                requires[cat] = requires.get(cat, 0) + n
        c.expect_end()
        plan.program(
            beds, baths, requires=requires, min_area=min_area, min_storage=min_storage
        )
        assert plan.program_spec is not None  # just set by plan.program(...)
        plan.program_spec.line = lineno
        plan.program_spec.col = kw.col
        plan.program_spec.end_col = kw.end_col
    elif key == "require":
        # `require adjacent|separate <a> <b>` / `require exterior <room> [<wall>]`
        # / `require area <room> >= <sqft>` — declared spatial intent, checked
        # against the compiled plan by the validator (see REQUIRE_UNMET).
        kind_tok = c.take("a requirement kind (adjacent|separate|exterior|area)")
        kind = kind_tok.text.lower()
        if kind in ("adjacent", "separate"):
            a = c.ident("the first room id").text
            b = c.ident("the second room id").text
            c.expect_end()
            plan.require(kind, a, b)
        elif kind == "exterior":
            rid = c.ident("a room id").text
            wall = c.wall() if c.peek() is not None else None
            c.expect_end()
            plan.require("exterior", rid, wall=wall)
        elif kind == "area":
            rid = c.ident("a room id").text
            c.keyword(">=")
            sqft_tok = c.peek()
            sqft = c.number("the minimum area")
            if sqft < 0:
                assert sqft_tok is not None  # number() consumed a token
                raise _ParseError(
                    "BAD_NUMBER",
                    "require area must be non-negative.",
                    sqft_tok.col,
                    end_col=sqft_tok.end_col,
                    hint="Give the minimum in square feet, e.g. `require area "
                    f"{rid} >= 300`.",
                )
            c.expect_end()
            plan.require("area", rid, min_area=sqft)
        else:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown requirement kind '{kind_tok.text}'.",
                kind_tok.col,
                end_col=kind_tok.end_col,
                hint="Use `require adjacent <a> <b>`, `require separate <a> <b>`, "
                "`require exterior <room> [<wall>]`, or `require area <room> >= "
                "<sqft>`.",
            )
        req = plan.requirements[-1]
        req.line, req.col, req.end_col = lineno, kw.col, kw.end_col
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
        ceiling_h = None
        vaulted = False
        # Optional room suffixes in any order: `level <n>`, `ceiling <h>`, `vaulted`.
        while (tok := c.peek()) is not None:
            opt = tok.text.lower()
            if opt == "level":
                c.keyword("level")
                level = c.level_value()
            elif opt == "ceiling":
                c.keyword("ceiling")
                ceiling_h = c.number("room ceiling height")
            elif opt == "vaulted":
                c.keyword("vaulted")
                vaulted = True
            else:
                break
        c.expect_end()
        try:
            plan.add_room(
                rid, rtype, width=w, length=length, level=level,
                ceiling_height=ceiling_h, vaulted=vaulted, **place_kwargs
            )
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
    elif key == "wall":
        # `wall <a> - <b> plumbing|bearing|rated` — one or more attributes of
        # the shared wall between two abutting rooms. Checked by the validator
        # (WALL_REF / WALL_NOADJ) like every dangling reference.
        a_tok = c.ident("a room id")
        a = a_tok.text
        sep = c.take("'-' or 'to'")
        if sep.text.lower() not in ("-", "to"):
            raise _ParseError(
                "SYNTAX",
                f"Expected '-' or 'to', got '{sep.text}'.",
                sep.col,
                end_col=sep.end_col,
            )
        b_tok = c.ident("the second room id")
        b = b_tok.text
        attrs: list[str] = []
        while (tok := c.peek()) is not None:
            at_tok = c.take("a wall attribute")
            at = at_tok.text.lower()
            if at not in WALL_ATTRIBUTES:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown wall attribute '{at_tok.text}'.",
                    at_tok.col,
                    end_col=at_tok.end_col,
                    hint=f"Use one or more of: {_WALL_ATTRS}.",
                )
            if at not in attrs:
                attrs.append(at)
        if not attrs:
            raise _ParseError(
                "SYNTAX",
                "Expected at least one wall attribute.",
                c.eol_col,
                end_col=c.eol_col + 1,
                hint=f"Name what the shared wall is: {_WALL_ATTRS} "
                f"(e.g. `wall {a} - {b} plumbing`).",
            )
        c.expect_end()
        if a == b:
            raise _ParseError(
                "SYNTAX",
                f"A wall statement names two different rooms, got '{a}' twice.",
                b_tok.col,
                end_col=b_tok.end_col,
                hint="Name the two rooms the wall stands between.",
            )
        plan.wall(a, b, *attrs)
        ws = plan.wall_specs[-1]
        ws.line, ws.col, ws.end_col = lineno, a_tok.col, a_tok.end_col
    elif key == "suite":
        # `suite <id>: <room> ...` — a named group of rooms (members are room
        # ids). The `:` tokenizes away like the `room <id>:` colon. Unknown
        # members are caught by the validator (SUITE_REF), like every reference.
        sid_tok = c.ident("a suite id")
        members: list[str] = []
        while c.peek() is not None:
            members.append(c.ident("a member room id").text)
        if not members:
            raise _ParseError(
                "SYNTAX",
                "A suite needs at least one member room.",
                c.eol_col,
                end_col=c.eol_col + 1,
                hint="List the rooms in the suite, e.g. "
                f"`suite {sid_tok.text}: master_bed master_bath master_wic`.",
            )
        plan.suite(sid_tok.text, *members)
        s = plan.suites[-1]
        s.line, s.col, s.end_col = lineno, kw.col, kw.end_col
    elif key == "zone":
        # `zone <id>: <member> ...` — a named band. Members are room ids OR
        # suite ids (so a zone can group whole suites). Resolution/validation
        # (ZONE_REF / ZONE_OVERLAP / ZONE_CROSS) happens in the validator.
        zid_tok = c.ident("a zone id")
        zmembers: list[str] = []
        while c.peek() is not None:
            zmembers.append(c.ident("a member room or suite id").text)
        if not zmembers:
            raise _ParseError(
                "SYNTAX",
                "A zone needs at least one member.",
                c.eol_col,
                end_col=c.eol_col + 1,
                hint="List the rooms or suites in the zone, e.g. "
                f"`zone {zid_tok.text}: primary bed_2 hall_beds`.",
            )
        plan.zone(zid_tok.text, *zmembers)
        z = plan.zones[-1]
        z.line, z.col, z.end_col = lineno, kw.col, kw.end_col
    elif key == "door":
        # Unified door statement. Two forms, told apart by what follows the id:
        #   interior:  door <a> - <b> [swing|cased|pocket|sliding] [opts]
        #   exterior:  door <id> <wall> exterior [opts]
        a_tok = c.ident("a room id")
        a = a_tok.text
        nxt = c.peek()
        if nxt is not None and nxt.text.lower() in ("-", "to"):
            c.take("'-'")  # consume the separator
            b = c.ident("the second room id").text
            kind = "swing"
            if (tok := c.peek()) is not None and tok.text.lower() in _DOOR_KINDS:
                kind = c.take("a door kind").text.lower()
            if kind == "cased":
                width = 6.0  # cased opens wide
            elif kind in DOUBLE_LEAF_KINDS:
                width = DEFAULT_DOUBLE_DOOR_WIDTH  # the stock 60 in pair
            else:
                width = 32 / 12
            offset, swing_into, hinge = None, None, None
            while c.peek() is not None:
                opt = c.take("an option").text.lower()
                if opt == "width":
                    width = c.number("door width")
                elif opt == "offset":
                    offset = c.number("door offset")
                elif opt == "into":
                    swing_into = c.ident("the room the door swings into").text
                elif opt == "hinge":
                    h = c.take("'near' or 'far'")
                    if h.text.lower() not in ("near", "far"):
                        raise _ParseError(
                            "BAD_OPTION",
                            f"Hinge must be 'near' or 'far', got '{h.text}'.",
                            h.col,
                            hint="Use `hinge near` or `hinge far`.",
                            end_col=h.end_col,
                        )
                    hinge = h.text.lower()
                else:
                    raise _ParseError(
                        "BAD_OPTION",
                        f"Unknown door option '{opt}'.",
                        c.toks[c.i - 1].col,
                        hint="Options: a kind (swing/cased/pocket/sliding/double/"
                        "french), width <n>, offset <n>, into <room>, hinge near|far.",
                        end_col=c.toks[c.i - 1].end_col,
                    )
            c.expect_end()
            plan.connect(
                a, b, width=width, kind=kind, offset=offset,
                swing_into=swing_into, hinge=hinge,
            )
            d = plan.interior_doors[-1]
            d.line, d.col, d.end_col = lineno, a_tok.col, a_tok.end_col
        else:
            # Exterior forms, told apart by the keyword after the wall:
            #   door <id> <wall> exterior [width <w>] [offset <o>] [no-egress]
            #   door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]
            wall = c.wall()
            kind_tok = c.take("'exterior' or 'overhead'")
            if kind_tok.text.lower() not in ("exterior", "overhead"):
                raise _ParseError(
                    "SYNTAX",
                    f"Expected 'exterior' or 'overhead', got '{kind_tok.text}'.",
                    kind_tok.col,
                    end_col=kind_tok.end_col,
                )
            if kind_tok.text.lower() == "overhead":
                # A sectional garage door: 9 x 7 (the residential single) by
                # default, 16 wide for a double. Never an egress door — there is
                # no no-egress option because it's implied.
                width, height, offset = OVERHEAD_DOOR_WIDTH, OVERHEAD_DOOR_HEIGHT, 1.0
                while c.peek() is not None:
                    opt = c.take("an option").text.lower()
                    if opt == "width":
                        width = c.number("door width")
                    elif opt == "height":
                        height = c.number("door height")
                    elif opt == "offset":
                        offset = c.number("offset")
                    else:
                        raise _ParseError(
                            "BAD_OPTION",
                            f"Unknown overhead-door option '{opt}'.",
                            c.toks[c.i - 1].col,
                            hint="Options: width <n>, height <n>, offset <n> "
                            "(no-egress is implied — an overhead door never "
                            "counts as egress).",
                            end_col=c.toks[c.i - 1].end_col,
                        )
                c.expect_end()
                plan.entrance(
                    a, wall, width=width, offset=offset, kind="overhead", height=height
                )
            else:
                width, offset, egress, ekind = 3.0, 1.0, True, "entry"
                width_given = False
                while c.peek() is not None:
                    opt = c.take("an option").text.lower()
                    if opt == "width":
                        width = c.number("door width")
                        width_given = True
                    elif opt == "offset":
                        offset = c.number("offset")
                    elif opt in ("no-egress", "nonegress"):
                        egress = False
                    elif opt in ("double", "french"):
                        ekind = opt
                    else:
                        raise _ParseError(
                            "BAD_OPTION",
                            f"Unknown exterior-door option '{opt}'.",
                            c.toks[c.i - 1].col,
                            hint="Options: double|french, width <n>, offset <n>, "
                            "no-egress.",
                            end_col=c.toks[c.i - 1].end_col,
                        )
                if ekind in DOUBLE_LEAF_KINDS and not width_given:
                    width = DEFAULT_DOUBLE_DOOR_WIDTH  # the stock 60 in pair
                c.expect_end()
                plan.entrance(
                    a, wall, width=width, offset=offset, egress=egress, kind=ekind
                )
            ed = plan.exterior_doors[-1]
            ed.line, ed.col, ed.end_col = lineno, a_tok.col, a_tok.end_col
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
        width, offset = 6.0, None  # wide cased opening by default (DEFAULT_OPENING_WIDTH)
        while c.peek() is not None:
            opt = c.take("an option").text.lower()
            if opt == "width":
                width = c.number("opening width")
            elif opt == "offset":
                offset = c.number("opening offset")
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown open option '{opt}'.",
                    c.toks[c.i - 1].col,
                    hint="Options: width <n>, offset <n>.",
                    end_col=c.toks[c.i - 1].end_col,
                )
        c.expect_end()
        plan.opening(a, b, width=width, offset=offset)
        door = plan.interior_doors[-1]
        door.line, door.col, door.end_col = lineno, a_tok.col, a_tok.end_col
    elif key == "entry":
        rid_tok = c.ident("a room id")
        rid = rid_tok.text
        wall = c.wall()
        width, offset, egress, ekind = 3.0, 1.0, True, "entry"
        width_given = False
        while c.peek() is not None:
            opt = c.take("an option").text.lower()
            if opt == "width":
                width = c.number("door width")
                width_given = True
            elif opt == "offset":
                offset = c.number("offset")
            elif opt in ("no-egress", "nonegress"):
                egress = False
            elif opt in ("double", "french"):
                ekind = opt
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown entry option '{opt}'.",
                    c.toks[c.i - 1].col,
                    hint="Options: double|french, width <n>, offset <n>, no-egress.",
                    end_col=c.toks[c.i - 1].end_col,
                )
        if ekind in DOUBLE_LEAF_KINDS and not width_given:
            width = DEFAULT_DOUBLE_DOOR_WIDTH  # the stock 60 in pair
        plan.entrance(rid, wall, width=width, offset=offset, egress=egress, kind=ekind)
        ed = plan.exterior_doors[-1]
        ed.line, ed.col, ed.end_col = lineno, rid_tok.col, rid_tok.end_col
    elif key == "window":
        rid_tok = c.ident("a room id")
        rid = rid_tok.text
        wall = c.wall()
        kind = "casement"  # the default: full glazed size = clear opening
        if (tok := c.peek()) is not None and tok.text.lower() in _WINDOW_KIND_SET:
            kind = c.take("a window kind").text.lower()
        width, offset = 4.0, 2.0
        sill, head = 3.0, 6.67  # ft above the floor; matches Window's defaults
        while c.peek() is not None:
            opt = c.take("an option").text.lower()
            if opt == "width":
                width = c.number("window width")
            elif opt == "offset":
                offset = c.number("offset")
            elif opt == "sill":
                sill = c.number("sill height")
            elif opt == "head":
                head = c.number("head height")
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown window option '{opt}'.",
                    c.toks[c.i - 1].col,
                    hint="Options: a kind (casement/slider/fixed/double-hung, "
                    "right after the wall), width <n>, offset <n>, sill <n>, "
                    "head <n>.",
                    end_col=c.toks[c.i - 1].end_col,
                )
        plan.add_window(
            rid, wall, width=width, offset=offset, sill_height=sill,
            head_height=head, kind=kind,
        )
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
        while (tok := c.peek()) is not None and tok.text.lower() in ("from", "to"):
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
    elif key == "frame":
        # `frame [bay <ft>] [span <ft>] [post <in>] [ridge|no-ridge]`.
        # Defaults match FrameSpec; placement runs after parse (place_frame).
        bay, span, post, ridge = 12.0, 40.0, inches(6), True
        while c.peek() is not None:
            opt = c.take("an option").text.lower()
            if opt == "bay":
                bay = c.number("bay spacing")
            elif opt == "span":
                span = c.number("max beam span")
            elif opt == "post":
                post = inches(c.number("post size in inches"))
            elif opt == "ridge":
                ridge = True
            elif opt in ("no-ridge", "noridge"):
                ridge = False
            else:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown frame option '{opt}'.",
                    c.toks[c.i - 1].col,
                    hint="Options: bay <ft>, span <ft>, post <in>, no-ridge.",
                    end_col=c.toks[c.i - 1].end_col,
                )
        if bay <= 0 or span <= 0 or post <= 0:
            raise _ParseError(
                "BAD_NUMBER",
                "frame bay/span/post must be positive.",
                kw.col,
                hint="e.g. `frame bay 12 span 40 post 6`.",
                end_col=kw.end_col,
            )
        # Set the spec now; place the structure after the whole file parses (so
        # the envelope/wings are known regardless of statement order).
        plan.frame_spec = FrameSpec(bay, span, post, ridge, lineno, kw.col, kw.end_col)
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
    #: True when parse-error recovery skipped statements: ``plan`` (if any) is
    #: PARTIAL — good enough to score and inspect, not to build or export from.
    recovered: bool = False

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

    def to_dict(self) -> dict:
        """A machine-readable view of the compile, for the agent loop / tooling.

        The same diagnostics as :meth:`report`, but as stable JSON-able data
        (code/severity/line/col/room/message/hint) rather than formatted text —
        so a consumer parses fields instead of scraping the human output.
        """
        return {
            "ok": self.ok,
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "info": len(self.infos),
            },
            "diagnostics": [
                {
                    "code": d.code,
                    "severity": d.severity.value,
                    "line": d.line,
                    "col": d.col,
                    "end_col": d.end_col,
                    "room": d.room,
                    "message": d.message,
                    "hint": d.hint,
                }
                for d in sorted(
                    self.diagnostics, key=lambda i: (i.line or 0, i.col or 0)
                )
            ],
        }


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


def compile_source(
    source: str, name: str | None = None, profile: "Profile | None" = None
) -> CompileResult:
    """Compile DSL ``source`` into a validated plan + diagnostics.

    ``profile`` selects the jurisdiction thresholds the code checks compare
    against (see :mod:`barndsl.profiles`); ``None`` uses the IRC baseline
    (:data:`~barndsl.profiles.DEFAULT`), which is byte-identical to the
    pre-profile behaviour.
    """
    diagnostics: list[Issue] = []
    plan = Barndominium(name=name or "Untitled")
    smap = _SourceMap()
    # True once any statement was skipped by parse-error recovery. Tracked at
    # the skip sites themselves (not inferred from ERROR diagnostics later):
    # semantic build errors also record ERRORs but skip nothing, and they must
    # keep the historical unguarded frame/validate behaviour.
    skipped = False

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
            skipped = True
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
                skipped = True
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
            skipped = True

    # Statement-level error recovery (review §1.3): a statement that failed to
    # parse already recorded its diagnostic and was skipped, but the *surviving*
    # statements still built a partial plan. Rather than throw it away (the old
    # behaviour: one typo dropped the whole design gradient an agent hill-climbs
    # on), validate and score the survivors. The result stays FAILED — the parse
    # errors keep ``ok`` False — so nothing downstream treats it as buildable.

    # A source where nothing parsed into a room is genuinely unbuildable — there
    # are no survivors to score — so keep the historical ``plan is None`` (an
    # empty/garbage input scores a flat zero with no misleading semantic cascade;
    # see score.py's plan-None handling).
    if skipped and not plan.rooms:
        return CompileResult(None, diagnostics, source)

    # Derive the structural frame (if requested) before checks, so the validator
    # and renderer see the placed posts/beams. On a partial (statements-skipped)
    # plan the incomplete geometry may defeat the placer or a check, so guard
    # those on the recovery path — a syntax error must never become a crash, but
    # the swallow is *recorded* so incomplete diagnostics can't pass as complete.
    # A clean or semantic-error compile keeps the original, unguarded behaviour,
    # so a real bug still bites.
    def _recovery_limit(what: str) -> Issue:
        return Issue(
            Severity.WARNING,
            "RECOVERY_LIMIT",
            f"{what} could not run on the partial plan; "
            "diagnostics are incomplete.",
            hint="Fix the parse error(s) above to get the full report.",
        )

    if plan.frame_spec is not None:
        from .structure import place_frame

        try:
            place_frame(plan)
        except Exception:
            if not skipped:
                raise
            diagnostics.append(_recovery_limit("Frame placement"))

    try:
        report: ValidationReport | None = validate(plan, profile)
    except Exception:
        if not skipped:
            raise
        report = None
        diagnostics.append(_recovery_limit("Validation"))
    if report is not None:
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
    return CompileResult(plan, diagnostics, source, recovered=skipped)


def compile_file(path: str, profile: "Profile | None" = None) -> CompileResult:
    """Compile a ``.barn`` file (see :func:`compile_source` for ``profile``)."""
    with open(path, encoding="utf-8") as fh:
        return compile_source(fh.read(), profile=profile)
