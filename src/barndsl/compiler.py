"""The barndsl compiler: DSL source text → a validated plan + diagnostics.

This is the front-end the AI (or a human) writes against. You hand it source
text in the barndsl architecture language; it lexes, parses, lowers to a
:class:`~barndsl.elements.Barndominium`, runs the building-code checks, and
returns a :class:`CompileResult` whose diagnostics carry line numbers, error
codes, and concrete fix hints — like a compiler's error output. That diagnostic
stream is what guides an author (or an agent) toward a correct design.

The grammar — one statement per line, ``#`` starts a comment — is documented once,
in :data:`DSL_REFERENCE` (the text agents, ``barndsl`` docs and LSP hover all
read); the statements themselves are the keys of :data:`_STATEMENT_PARSERS`,
exported as :data:`STATEMENT_KEYWORDS`.

Any LENGTH field (a size, position, offset, width, setback, ceiling…) accepts a
feet-and-inches literal as well as decimal feet: ``12-6`` (= 12′6″ = 12.5 ft),
``12′6″``, ``12′``, ``12'6``, ``12'`` and ``6″``. These are recognised only as a
single whitespace-free token, so ``12 - 6`` (three tokens), ``door a - b`` and a
plain negative ``-6`` keep their meanings; ``-12-6`` (negative feet + inches) is
rejected, and ASCII ``12'6"`` is unsupported because ``"`` starts a string.
"""

from __future__ import annotations

import difflib
import math
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from .constants import WALK_DEFAULT_WIDTH
from .elements import (
    ALARM_KINDS,
    DEFAULT_BIFOLD_DOOR_WIDTH,
    DEFAULT_DOUBLE_DOOR_WIDTH,
    DOOR_KINDS,
    DOUBLE_LEAF_KINDS,
    DRIVE_SURFACES,
    OVERHEAD_DOOR_HEIGHT,
    OVERHEAD_DOOR_WIDTH,
    LIGHT_KINDS,
    SERVICE_UTILITIES,
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

#: Statements that describe a whole *building*, not a reusable block — illegal
#: inside a part file (fragment mode) → ``PART_HOST_STMT``. A part borrows the
#: host's. ``use`` is legal in a part (Phase 20 — nested composition, depth ≤ 2,
#: guarded by :func:`barndsl.compose.compose_uses`); ``stair`` is legal too (Phase
#: 20 — multi-level parts, a part may carry ``level 1`` rooms + a connecting
#: stair). See the design doc §3.1. Every statement is classified as exactly one
#: of this set or :data:`PART_STATEMENTS` (the tests enforce it), so a new
#: building-wide statement can't be silently dropped from a part.
HOST_ONLY_STATEMENTS = frozenset({
    "plan", "envelope", "wing", "ceiling", "floor", "program", "require", "site",
    "setback", "building", "street", "orientation", "roof", "overhang",
    "finish", "frame", "electrical", "accessible", "climate",
    "drive", "walk", "well", "septic", "service", "grade",
})

#: Statements a part file may carry: its rooms and everything hung on them.
PART_STATEMENTS = frozenset({
    "note", "room", "wall", "door", "open", "entry", "window", "porch", "stair",
    "suite", "zone", "fixture", "outlet", "switch", "light", "alarm", "use", "param",
})

#: Single-letter wall aliases the `fixture` statement accepts (N|S|E|W), plus the
#: full names, mapped to a :class:`~barndsl.elements.Direction`.
_FIXTURE_WALLS = {
    "n": Direction.NORTH, "north": Direction.NORTH,
    "s": Direction.SOUTH, "south": Direction.SOUTH,
    "e": Direction.EAST, "east": Direction.EAST,
    "w": Direction.WEST, "west": Direction.WEST,
}
_TYPES = ", ".join(t.value for t in RoomType)
#: The room-type names as a plain list, for did-you-mean ranking (BAD_TYPE).
_TYPE_VALUES = tuple(t.value for t in RoomType)
_WALLS = "north, south, east, west"
#: Every `fixture <kind>` the parser accepts, interpolated into DSL_REFERENCE the
#: same way _TYPES/_WALLS are — so the one enum the reference used to only sample
#: is now enumerated in full and can never drift from the catalog. Imported
#: locally (not at module top) to mirror the existing FIXTURES import at the point
#: of use and stay clear of any import-order surprise; fixtures.py pulls in only
#: elements/geometry/validation, none of which import compiler, so this is safe.
from .fixtures import FIXTURE_KINDS as _FIXTURE_KINDS  # noqa: E402

_FIXTURES = ", ".join(sorted(_FIXTURE_KINDS))


def _did_you_mean(word: str, options: tuple[str, ...] | list[str]) -> str:
    """A leading ``Did you mean `x` or `y`?`` clause for ``word`` against
    ``options`` (empty when nothing is close). Mirrors the accept-pragma's use of
    :func:`difflib.get_close_matches` so a misspelled keyword/type ranks the near
    hits instead of dumping the whole list."""
    hits = difflib.get_close_matches(word.lower(), list(options), n=3)
    if not hits:
        return ""
    return "Did you mean " + " or ".join(f"`{h}`" for h in hits) + "? "

#: A `use` alias is a plain identifier — no dot (dots namespace stamped ids).
_ALIAS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
#: A `param` name is a plain identifier (Phase 20). A bare name matching this that
#: also names a declared param resolves as a number inside a parametric part.
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

_DOOR_KINDS = frozenset(DOOR_KINDS)
_WINDOW_KIND_SET = frozenset(WINDOW_KINDS)
_LIGHT_KIND_SET = frozenset(LIGHT_KINDS)
_ALARM_KIND_SET = frozenset(ALARM_KINDS)
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
Any length may be written in decimal feet (12, 10.5) OR feet-and-inches as one
token: 12-6, 12'6, 12', 12′6″, 12′ or 6″ (12-6 = 12′6″ = 12.5 ft). The dash form
only reads as a length when there is no space around it, so `12 - 6`, `door a - b`
and a plain `-6` are unaffected; `-12-6` (negative feet + inches) is rejected, and
ASCII 12'6" (with the inch ") is not accepted because " starts a string.

A comment may carry a suppression pragma for a justified deviation:
  # barndsl: accept <CODE> ["reason"]
Trailing a statement it downgrades that CODE on that line to an accepted INFO;
on its own line it applies to the next statement line. Errors cannot be accepted
(ACCEPT_DENIED); an unknown code is ACCEPT_UNKNOWN; a pragma matching no
diagnostic on its line is ACCEPT_UNUSED. Accepted diagnostics stop deducting from
the design score but survive as an audited INFO (packet: "Accepted deviations").

Statements:
  plan "Name"
  envelope <W> x <L>              # primary footprint block (at the origin)
  wing <W> x <L> at <x>,<y>       # optional; add blocks for an L/T/U footprint
  ceiling <H>                     # ceiling height (>= 7; 9-12 typical)
  floor <D>                       # inter-floor assembly depth (ft); floor-to-floor = ceiling + this
  accessible                      # opt-in: run accessibility / aging-in-place nudges
  electrical                      # opt-in: emit the electrical / life-safety checklist reminder
  note "free text" [at <x>,<y> [level <n>]]
                                  # a design note. Bare = free text carried in the
                                  #   packet/notes; with `at <x>,<y>` it becomes a
                                  #   leader-line callout drawn on the plan at that
                                  #   world point (SW origin, +x east, +y north),
                                  #   on floor `level` (default 0). A note anchored
                                  #   outside the footprint is a gentle NOTE_OUTSIDE
                                  #   info, not an error.
  program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>] [storage <sqft>]  # optional intent, checked vs the rooms
                                  #   bed/bath = exact counts; other types = at-least; area = min interior;
                                  #   storage = min closet+pantry+storage-room sq ft
  require adjacent <room_a> <room_b>    # the two rooms must share a wall
  require separate <room_a> <room_b>    # the two rooms must NOT share a wall
  require exterior <room> [<wall>]      # the room needs an exterior wall (optionally that side)
  require area <room> >= <sqft>         # the room's nominal area must be at least sqft
        # declared spatial intent, like `program`: each unmet requirement is a
        # REQUIRE_UNMET warning (never blocking); an unknown room id is an error.
  room <id>: <type> <placement> size <W> x <L> [level <n>] [ceiling <h>] [vaulted] [floor "<finish>"]
        # `ceiling <h>` overrides the plan ceiling for this room (a tray or a
        # taller great room); `vaulted` makes it open to the roof (no flat ceiling).
        # `floor "<finish>"` sets the 3D floor material (e.g. "tile", "polished
        # concrete", "wood plank"); unset picks a default by room type.
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
  door <id_a> - <id_b> [swing|cased|pocket|sliding|bifold|double|french] [width <w>] [offset <o>] [into <room>] [hinge near|far]
        # interior door between two rooms. swing (default) hinges; cased = an open
        # walk-through (no leaf); pocket/sliding slide; bifold folds flat (the
        # reach-in closet door, default 4 ft — centre it on the closet and size it
        # near the closet's width so every foot of rod is reachable); double/french
        # = a pair of half-width leaves (default 5 ft total). offset = ft from the
        # wall's S/W end; `into <room>` + `hinge near|far` set the swing side/hinge.
        # With no `into` the leaf swings east/north, or into the other room when
        # it's too wide for that one; an exterior door always swings inward.
        # The two-room forms `wall`, `door` and `open` accept `to` in place of the
        # `-` separator, so `door a to b` == `door a - b` (mind the spaces — the
        # dashless `a-b` reads as one token, and `a - b` still works too).
  door <id> <wall> exterior [double|french] [width <w>] [offset <o>] [no-egress]  # exterior door, on an exterior wall
  door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]
        # overhead/sectional garage door on a garage/shop's exterior wall. Defaults
        # 9 x 7 (the residential single); `width 16` is a double. Never an egress
        # door and not a building entrance — the plan still needs a people-door.
  open <id_a> - <id_b> [width <w>] [offset <o>]            # shorthand for `door <a> - <b> cased ...`
  entry <id> <wall> [double|french] [width <w>] [offset <o>] [no-egress]
        # shorthand for `door <id> <wall> exterior ...`; double/french = a pair of
        # half-width leaves (egress clear width counts ONE leaf, IRC R311.2)
  window <id> <wall> [casement|slider|fixed|double-hung] [width <w>] [offset <o>] [sill <s>] [head <h>] [fixed] [tempered]
        # window; sill/head are ft above the floor. The kind (default casement)
        # sets the escape-opening math: a casement opens ~its full glazed size, a
        # slider opens ~half its width, a double-hung ~half its height, and FIXED
        # glass never counts for bedroom egress (it still daylights). `fixed` may
        # also trail as a flag; a fixed window counts for daylight but not the
        # openable-area ventilation floor (VENT_AREA). `tempered` declares safety
        # glazing — the IRC R308.4 escape hatch that silences WINDOW_TEMPERED here.
  porch <id> at <x>,<y> size <W> x <L> [covered|open]
  stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]
        # vertical circulation; defaults from 0 to 1. Place its footprint over a
        # room on each level so it links them (and makes the upper floor reachable).
  fixture <kind> in <room> [at <x>,<y>] [wall N|S|E|W [offset <ft>]] [rotate <deg>] [width <w>]
  fixture counter in <room> along N|S|E|W [from <a> to <b>] [depth <d>]
        # place a fixture / furnishing. <kind> is one of: %s.
        # `at <x>,<y>` is ROOM-LOCAL feet,
        # measured from the room's SW corner (unlike every other statement, which
        # is in world coordinates). Omit `at` to auto-place against `wall`, or omit
        # both for the first free spot. `wall ... offset <ft>` pins the piece that
        # many feet along the wall from its S/W start corner (the door/window
        # convention); omit the offset to auto-slot clear of door swings. `offset`
        # needs `wall` and excludes `at`. `rotate` turns it in plan (snapped to a
        # quarter-turn); `width` overrides the run of a resizable piece (a counter).
        # `along <wall>` (COUNTER ONLY) lays a countertop RUN along a wall: the whole
        # wall, or `from <a> to <b>` (room-local ft from the wall's S/W corner), at
        # `depth <d>` into the room (1–4 ft; default the US-standard 25 in = 2-1).
        # It's a wall-backed counter like any other — an L or U is two/three runs
        # meeting at mitred corners, and a sink/range set into a run doesn't clash
        # with it. `along` is exclusive with `at`/`wall`/`width`. Other kinds have a
        # fixed footprint, so `along` on them is an error.
        # Fixtures ADD to a room's auto-seeds; an explicit fixture of a seeded kind
        # (bath toilet/lavatory/tub, kitchen fridge/range/sink, laundry washer/
        # dryer) REPLACES just that seed. Baths, kitchens and laundries auto-seed
        # their fixtures with no `fixture` line at all.
  outlet in <room> wall N|S|E|W offset <ft> [gfci]
        # a receptacle on a room wall, `offset` ft from the wall's S/W start
        # corner. `gfci` marks a ground-fault receptacle (required at kitchens,
        # baths, laundries and outdoors, IRC E3902). Declaring any outlet opts the
        # room into the receptacle-spacing check (no wall point > 6 ft from one,
        # IRC E3901.2 -> OUTLET_SPACING); a wet-room outlet without `gfci` warns
        # (OUTLET_GFCI). The electrical layer is opt-in — draw it or don't.
  switch in <room> wall N|S|E|W offset <ft>
        # a wall switch on a room wall (offset from the S/W start corner). A
        # habitable room with power (outlets/switches) but no `light` gets the
        # ROOM_NO_LIGHT lighting-outlet nudge (IRC E3903).
  light in <room> at <x>,<y> [kind ceiling|pendant|fan|recessed]
        # a ceiling luminaire at ROOM-LOCAL x,y (feet from the room's SW corner,
        # like a `fixture at`). kind defaults to ceiling.
  alarm smoke|co|smoke_co in <room> [at <x>,<y>]
        # a smoke and/or carbon-monoxide alarm, placed at the room (a ceiling
        # device — no wall/offset). `smoke` = smoke alarm (IRC R314), `co` = CO
        # alarm (R315), `smoke_co` = a combination unit satisfying both (the only
        # combo spelling — `combo` is not accepted). Optional `at <x>,<y>` is
        # ROOM-LOCAL feet for the symbol (defaults to the room centre). Declaring
        # any alarm turns on the placement checks: a bedroom without a smoke/combo
        # alarm (ALARM_BEDROOM), a sleeping area with no adjacent-hall alarm
        # (ALARM_HALL), a level with no smoke alarm (ALARM_LEVEL), and — with
        # bedrooms + a garage/shop — no CO/combo alarm (ALARM_CO, info).
  use "<relpath>" as <alias> at <x>,<y> [level <n>] [mirror x|y] [rotate 90|180|270] [with k=v[, k=v…]]
        # cross-file composition: stamp a PART (any `.barn` file with no `plan`
        # header — rooms/openings/windows/fixtures/devices in its own local feet)
        # into this plan. `"<relpath>"` is quoted and RELATIVE to the including
        # file's directory (absolute paths / `..` escapes / no-home-dir sources are
        # USE_UNRESOLVED errors — parts stay under the folder you compile or serve).
        # `as <alias>` is a required, unique identifier: every id inside the part is
        # stamped `<alias>.<id>` (m.bed, m.bath), and host statements reference those
        # namespaced ids like locals (`door great - m.bed`). `at <x>,<y>` places the
        # stamped bounding box's SW corner (ft); `level <n>` lands it on host level n
        # (default 0) — a part's own `level 1` rooms lift by n. `mirror y` flips the
        # part east↔west, `mirror x` north↔south; `rotate 90|180|270` turns it
        # counter-clockwise (rooms are axis-aligned, so only 90° steps). With both,
        # the part is ROTATED FIRST, THEN MIRRORED in its own local frame; the
        # transformed bounding box's SW corner still lands at `at`. `with k=v, …`
        # passes PARAM values to a parametric part (numbers only; keys the part
        # doesn't declare are PARAM_UNDECLARED). Wall directions, wall offsets and
        # fixture rotations all remap so the stamped copy stays code-clean. A part
        # may itself `use` nested parts (depth ≤ 2; a cycle is USE_CYCLE, depth 3 is
        # USE_NESTED) and may carry `level 1` rooms + a `stair`. The part is compiled
        # once per (path, param values) and stamped per use; part-internal
        # diagnostics report once (anchored to the part file), placement-dependent
        # ones per use (anchored to the `use` line).
  param <name> = <number>            # (part files only) declare a part PARAMETER — a
        # number (decimal feet or ft-in) the host may override with `use ... with
        # name=value`. The default is mandatory (every param is optional at use).
        # Inside the part, the bare NAME stands wherever a number stands (sizes,
        # positions, offsets…). Numbers only in v1 — no strings, no arithmetic. A
        # bare name that names no param is PARAM_UNKNOWN; `param` in a plan is
        # PARAM_IN_PLAN.
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
        # fit inside it, that's a SETBACK error. A `setback` with no `site` errors.
  building at <x>,<y>              # optional; place the building on the lot
        # pins the plan origin (the SW envelope corner, world 0,0) at <x>,<y> in
        # lot feet from the lot's SW corner. With it declared the setback check
        # measures each side's real clearance and can name which side is encroached
        # and by how much (ft-in); without it the check is dimensions-only.
  drive at <x>,<y> size <W> x <L> [gravel|concrete|asphalt]
        # a driveway on the lot, in LOT feet (the `building at` frame). Surface
        # defaults to gravel; concrete/asphalt cost more. Needs a `site`.
  walk from <room> to drive [width <ft>]
        # a walkway from <room>'s exterior door to the nearest drive edge (width
        # default 4 ft). Only a `drive` is a valid destination. Needs a `site`.
  well at <x>,<y>                  # a water well (lot feet). Needs a `site`.
  septic at <x>,<y> [field <W> x <L>]
        # a septic tank at <x>,<y> (lot feet), with an optional drain field drawn
        # just north of the tank. Its separation from a `well` is checked (the
        # common 100 ft health-department rule). Needs a `site`.
  service electric|water|gas from N|S|E|W
        # a utility service drop entering from a lot side (drawn as a labelled
        # arrow). Needs a `site`.
  grade <ft>                       # finish-floor height above finished grade
        # a single flat-site value (e.g. `grade 2-8`). When it exceeds 30 in, every
        # porch is a walking surface that needs a 36 in guard (IRC R312.1). Does
        # NOT need a `site` — it's about the building, not the lot.

<placement> is one of:
  at <x>,<y>                      # absolute, in feet
  <dir>-of <room> [align near|far|center] [offset <n>]
        # <dir> = east|west|north|south. Each has an equivalent alias, fully
        # interchangeable: right-of = east-of, left-of = west-of, above = north-of,
        # below = south-of. Abut an
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
""" % (_FIXTURES, _TYPES, _WALLS)


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

# --- feet-and-inches length literals ----------------------------------------
#
# A LENGTH field (a size, position, offset, width, setback, ceiling…) accepts a
# US feet-and-inches literal in addition to a decimal-feet number. These are all
# recognised at the NUMBER-PARSING level (in `_Cursor.number`), not by the lexer:
# the tokenizer already splits on whitespace and NOT on `-`/`'`/`′`/`″`, so
# `12-6`, `12'6`, `12′6″` and `12′` each arrive as a SINGLE token, while the
# separator forms keep their meanings — `12 - 6` and `door a - b` tokenize with a
# standalone `-`, and a plain negative like `-6` still parses as a float. Doing
# this in `number()` means the disambiguation is free: a length literal is only a
# length literal when it's one whitespace-free token, exactly the property that
# tells `12-6` (ft-in) apart from `12 - 6` (three tokens) and `a - b`.
#
# Accepted:  12-6  (12′6″ = 12.5 ft; inches 0–11.99, feet non-negative)
#            12′6″  12′6  12′     (Unicode prime/double-prime; inches optional)
#            12'6   12'            (ASCII foot mark; NO inch double-quote)
#            6″                    (Unicode inches alone)
# Rejected:  -12-6  (negative feet WITH an inch part — ambiguous sign; errors)
#            12'6"  and  6"        (ASCII inch double-quote — `"` starts a string
#                                   in the lexer, so it never reaches here intact)
#: <feet>(' | ′)[<inches>][″] — the foot-mark form (ASCII or Unicode prime).
_FT_IN_FOOT = re.compile(r"^(-?\d+(?:\.\d+)?)['′](\d+(?:\.\d+)?)?[″]?$")
#: <feet>-<inches> — the dash form; feet must be non-negative (no leading `-`).
_FT_IN_DASH = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")
#: <inches>″ — inches alone, Unicode double-prime only.
_FT_IN_INCH = re.compile(r"^(-?\d+(?:\.\d+)?)″$")


def _parse_ft_in(text: str) -> float | None:
    """Parse a feet-and-inches length literal to decimal feet, or ``None`` if
    ``text`` isn't one of the accepted forms (see the module note above). Inches
    outside ``[0, 12)`` make it *not* a length literal (returns ``None``), so the
    caller reports a plain malformed-number error rather than silently over-rolling
    the inches into feet."""
    m = _FT_IN_FOOT.match(text)
    if m is not None:
        ft = float(m.group(1))
        inch = float(m.group(2)) if m.group(2) is not None else 0.0
        if not (0.0 <= inch < 12.0):
            return None
        return ft + (-1.0 if ft < 0 else 1.0) * inch / 12.0
    m = _FT_IN_DASH.match(text)
    if m is not None:
        inch = float(m.group(2))
        if not (0.0 <= inch < 12.0):
            return None
        return float(m.group(1)) + inch / 12.0  # feet is non-negative here
    m = _FT_IN_INCH.match(text)
    if m is not None:
        return float(m.group(1)) / 12.0
    return None


#: Words a first-time user reaches for to mean feet/inches, which the DSL never
#: accepts (a length is one token: 12, 12-6 or 12′6″). Recognised only to *teach*.
_FEET_WORDS = {"feet", "ft", "foot"}
_INCH_WORDS = {"inches", "inch", "in", "ins"}
#: `<digits>('|′)<digits?>` — the foot-mark form a user typed before an ASCII `"`.
_FOOTMARK_STEM = re.compile(r"^(\d+(?:\.\d+)?)['′](\d+(?:\.\d+)?)?$")


def _ftin_string_hint(raw: str, quote_col: int) -> str | None:
    """Teach the ``12'6\"`` slip that opens a string literal.

    When an unterminated ``"`` immediately follows a digit — the tell-tale of a
    user writing feet-and-inches with the ASCII inch mark (``12'6"``) — return a
    targeted hint translating it to an accepted form (``12-6`` / ``12′6″``);
    otherwise ``None`` (the generic add-the-quote hint stands). ``quote_col`` is
    the 1-based column of the opening quote."""
    idx = quote_col - 2  # 0-based index of the char just before the quote
    if idx < 0 or idx >= len(raw) or not raw[idx].isdigit():
        return None
    # Grab the bareword run right before the quote (the would-be length token).
    j = idx
    while j >= 0 and raw[j] not in ' \t,:"#{}':
        j -= 1
    stem = raw[j + 1:idx + 1]
    m = _FOOTMARK_STEM.match(stem)
    if m is not None:
        ft, inch = m.group(1), m.group(2)
        dash = f"{ft}-{inch}" if inch else ft
        uni = f"{ft}′{inch}″" if inch else f"{ft}′"
        bad = stem + '"'
        return (
            f"Feet-and-inches uses a dash or unicode marks — write `{dash}` or "
            f"`{uni}`, not `{bad}` (the ASCII `\"` opens a string literal)."
        )
    return (
        "Feet-and-inches uses a dash (`12-6`) or unicode marks (`12′6″`); the "
        "ASCII `\"` opens a string literal, so it can't close a length."
    )


def _units_word_hint(toks: list[_Token], unit_idx: int) -> str | None:
    """Teach ``12 feet 6 inches`` — a length spelled out in words.

    ``toks[unit_idx]`` is a ``feet``/``ft``/``foot`` token; the token before it is
    the feet number, and an optional ``<n> inches`` may follow. Return a hint that
    translates the literal input to the accepted single-token form (``12-6``),
    or ``None`` when the shape isn't the words-for-units slip."""
    if unit_idx <= 0 or unit_idx >= len(toks):
        return None
    if toks[unit_idx].text.lower() not in _FEET_WORDS:
        return None
    feet_tok = toks[unit_idx - 1]
    if feet_tok.quoted:
        return None
    try:
        float(feet_tok.text)
    except ValueError:
        return None
    feet = feet_tok.text
    literal = f"{feet} {toks[unit_idx].text}"
    inch: str | None = None
    # Optional `<inches> inch(es)` right after the feet word.
    if unit_idx + 2 < len(toks) and toks[unit_idx + 2].text.lower() in _INCH_WORDS:
        cand = toks[unit_idx + 1]
        if not cand.quoted:
            try:
                float(cand.text)
                inch = cand.text
                literal += f" {cand.text} {toks[unit_idx + 2].text}"
            except ValueError:
                inch = None
    dash = f"{feet}-{inch}" if inch else feet
    uni = f"{feet}′{inch}″" if inch else f"{feet}′"
    return (
        f"Feet-and-inches is one token — write `{dash}` (or `{uni}`), e.g. "
        f"`size {dash} x <length>`. `{literal}` is several tokens the parser "
        "can't read as one length."
    )


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
    def __init__(self, tokens: list[_Token], param_env: dict[str, float] | None = None):
        self.toks = tokens
        self.i = 0
        #: Parametric-part environment (Phase 20). When compiling a part fragment
        #: with params, a bare identifier standing where a NUMBER is expected
        #: resolves to its param value (use-site override or declared default).
        #: ``None`` outside a parametric part — an identifier is then the usual
        #: ``BAD_NUMBER``. See :meth:`number`.
        self.param_env = param_env

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
            # A LENGTH field also accepts a feet-and-inches literal (12-6, 12′6″,
            # 12′, 12'6, 6″). These aren't valid floats, so try them here — the
            # single-token property is what keeps `12-6` (ft-in) distinct from
            # `12 - 6` / `a - b` (which tokenize with a standalone `-`).
            ft_in = _parse_ft_in(t.text)
            if ft_in is not None:
                return ft_in
            # Parametric parts (Phase 20): inside a part fragment, a bare param
            # NAME stands wherever a number stands — resolve it to the use-site
            # value or its declared default. Numbers only in v1 (no arithmetic),
            # so this is a plain name → value lookup at the token level; the source
            # text is never rewritten. An identifier matching no param is
            # PARAM_UNKNOWN (with a did-you-mean over the declared names).
            if self.param_env is not None and _PARAM_NAME_RE.match(t.text):
                if t.text in self.param_env:
                    return self.param_env[t.text]
                raise _ParseError(
                    "PARAM_UNKNOWN",
                    f"{_did_you_mean(t.text, tuple(self.param_env))}"
                    f"'{t.text}' is not a declared param.",
                    t.col,
                    end_col=t.end_col,
                    hint="Declare it with `param " + t.text + " = <number>`, or use a "
                    "number. Params are numbers only (no arithmetic).",
                )
            raise _ParseError(
                "BAD_NUMBER",
                f"Expected a number for {what}, got '{t.text}'.",
                t.col,
                end_col=t.end_col,
                hint="Use decimal feet (12 or 10.5) or feet-and-inches "
                "(12-6, 12′6″, 12′, 12'6).",
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
            # A first-timer spelling a length in words (`size 12 feet 6 inches x
            # 14`) trips here on the missing `x`; teach the single-token form.
            hint = _units_word_hint(self.toks, self.i - 1)
            raise _ParseError(
                "SYNTAX",
                f"Expected '{expected}', got '{t.text}'.",
                t.col,
                end_col=t.end_col,
                hint=hint,
            )
        return t

    def room_type(self) -> RoomType:
        t = self.take("a room type")
        try:
            return RoomType(t.text.lower())
        except ValueError:
            raise _ParseError(
                "BAD_TYPE",
                f"{_did_you_mean(t.text, _TYPE_VALUES)}Unknown room type '{t.text}'.",
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
            elif _units_word_hint(self.toks, self.i) is not None:
                hint = _units_word_hint(self.toks, self.i)  # type: ignore[assignment]
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

#: Every spelling a relative placement accepts (``east-of``, ``above``, …) — the
#: editors' anchor lists derive from this rather than re-listing it.
PLACEMENT_ANCHORS: tuple[str, ...] = tuple(_PLACEMENT)


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


def _scan_param_defaults(source: str) -> dict[str, float]:
    """Best-effort pre-scan of a part's ``param <name> = <number>`` declarations
    (Phase 20), name → default value, so the resolution environment is ready
    *before* the statement loop — a bare param name may then be referenced on a
    line above its own ``param`` line. Silent and tolerant: a malformed ``param``
    line is skipped here (the authoritative parse in :func:`_parse_statement`
    reports it). Later duplicate declarations overwrite earlier ones; the
    authoritative parse flags the duplicate."""
    defaults: dict[str, float] = {}
    for raw in source.splitlines():
        toks = _tokenize_line(raw, 0)
        if not toks or toks[0].text.lower() != "param":
            continue
        rest = "".join(t.text for t in toks[1:])
        name, sep, val = rest.partition("=")
        if not sep or not _PARAM_NAME_RE.match(name):
            continue
        num = _param_value(val)
        if num is not None:
            defaults[name] = num
    return defaults


def _param_value(text: str) -> float | None:
    """Parse a param default / use-site value literal (Phase 20): a decimal-feet
    number or a feet-and-inches literal, or ``None`` if it isn't a number. Params
    are **numbers only** in v1 — no names, no arithmetic — so this never consults a
    param environment."""
    try:
        v = float(text)
    except ValueError:
        return _parse_ft_in(text)
    return v if math.isfinite(v) else None


# --- statement parsers: site / lot features ---------------------------------


def _parse_site_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`site <W> x <L>` — lot dimensions (east-west x north-south feet)."""
    w = c.number("site width")
    c.keyword("x")
    length = c.number("site length")
    c.expect_end()
    plan.site(w, length)
    ss = plan.site_spec
    assert ss is not None  # .site() just created it
    ss.line, ss.col, ss.end_col = lineno, kw.col, kw.end_col


def _parse_setback_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`setback [front <n>] [side <n>] [rear <n>]` — any subset, any order."""
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


def _parse_building_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`building at <x>,<y>` — plan origin (SW envelope corner) on the lot."""
    c.keyword("at")
    bx = c.number("the building x on the lot")
    by = c.number("the building y on the lot")
    c.expect_end()
    plan.building(bx, by)
    ss = plan.site_spec
    assert ss is not None  # .building() just created it
    ss.building_line, ss.building_col, ss.building_end_col = lineno, kw.col, kw.end_col


def _parse_drive_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`drive at <x>,<y> size <w> x <l> [gravel|concrete|asphalt]`."""
    from .elements import Drive

    c.keyword("at")
    dx = c.number("the drive x on the lot")
    dy = c.number("the drive y on the lot")
    c.keyword("size")
    dw = c.number("the drive width")
    c.keyword("x")
    dl = c.number("the drive length")
    surface = "gravel"
    if (tok := c.peek()) is not None:
        surface = c.take("a drive surface").text.lower()
        if surface not in DRIVE_SURFACES:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown drive surface '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint=f"Use one of: {', '.join(DRIVE_SURFACES)} (default gravel).",
            )
    c.expect_end()
    plan._site().drives.append(
        Drive(dx, dy, dw, dl, surface, line=lineno, col=kw.col, end_col=kw.end_col)
    )


def _parse_walk_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`walk from <room> to drive [width <ft>]`."""
    from .elements import Walk

    c.keyword("from")
    room_tok = c.ident("a room id")
    c.keyword("to")
    c.keyword("drive")
    width = WALK_DEFAULT_WIDTH
    if (tok := c.peek()) is not None:
        if tok.text.lower() != "width":
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown walk option '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint="A walk takes only `width <ft>`, e.g. `walk from mud to drive width 4`.",
            )
        c.keyword("width")
        width = c.number("the walk width")
    c.expect_end()
    plan._site().walks.append(
        Walk(room_tok.text, width, line=lineno, col=kw.col, end_col=kw.end_col)
    )


def _parse_well_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`well at <x>,<y>` — water well point in lot feet."""
    from .elements import Well

    c.keyword("at")
    wx = c.number("the well x on the lot")
    wy = c.number("the well y on the lot")
    c.expect_end()
    plan._site().wells.append(Well(wx, wy, line=lineno, col=kw.col, end_col=kw.end_col))


def _parse_septic_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`septic at <x>,<y> [field <w> x <l>]`."""
    from .elements import Septic

    c.keyword("at")
    px = c.number("the septic x on the lot")
    py = c.number("the septic y on the lot")
    fw = fl = None
    if (tok := c.peek()) is not None:
        if tok.text.lower() != "field":
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown septic option '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint="A septic takes only `field <w> x <l>`, e.g. "
                "`septic at 90,20 field 40 x 60`.",
            )
        c.keyword("field")
        fw = c.number("the drain-field width")
        c.keyword("x")
        fl = c.number("the drain-field length")
    c.expect_end()
    plan._site().septics.append(
        Septic(px, py, fw, fl, line=lineno, col=kw.col, end_col=kw.end_col)
    )


def _parse_service_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`service <electric|water|gas> from <N|S|E|W>` — utility drop."""
    from .elements import Service

    util_tok = c.take("a utility (electric|water|gas)")
    utility = util_tok.text.lower()
    if utility not in SERVICE_UTILITIES:
        raise _ParseError(
            "BAD_OPTION",
            f"Unknown service utility '{util_tok.text}'.",
            util_tok.col,
            end_col=util_tok.end_col,
            hint=f"Use one of: {', '.join(SERVICE_UTILITIES)}.",
        )
    c.keyword("from")
    side_tok = c.take("a lot side (N|S|E|W)")
    svc_side = _FIXTURE_WALLS.get(side_tok.text.lower())
    if svc_side is None:
        raise _ParseError(
            "BAD_WALL",
            f"Unknown lot side '{side_tok.text}'.",
            side_tok.col,
            end_col=side_tok.end_col,
            hint="Use N, S, E or W (the lot edge the service enters from).",
        )
    c.expect_end()
    plan._site().services.append(
        Service(utility, svc_side, line=lineno, col=kw.col, end_col=kw.end_col)
    )


# --- statement parsers: brief contract --------------------------------------


def _parse_program_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """`program <n> bed ...` — intended counts and area/storage targets."""
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
                f"{_did_you_mean(noun.text, _TYPE_VALUES)}"
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
    plan.program(beds, baths, requires=requires, min_area=min_area, min_storage=min_storage)
    assert plan.program_spec is not None  # just set by plan.program(...)
    plan.program_spec.line = lineno
    plan.program_spec.col = kw.col
    plan.program_spec.end_col = kw.end_col


def _parse_require_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    """Parse declared spatial intent (`require ...`) and stamp its source span."""
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
            "`require exterior <room> [<wall>]`, or `require area <room> >= <sqft>`.",
        )
    req = plan.requirements[-1]
    req.line, req.col, req.end_col = lineno, kw.col, kw.end_col


# --- statement parsers: rooms and grouping ----------------------------------


def _parse_room_statement(c: _Cursor, plan: Barndominium, smap: _SourceMap, lineno: int) -> None:
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
    floor_finish = None
    # Optional room suffixes in any order: `level <n>`, `ceiling <h>`,
    # `vaulted`, `floor "<finish>"`.
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
        elif opt == "floor":
            c.keyword("floor")
            floor_finish = c.take("a floor finish (quoted)").text
        else:
            break
    c.expect_end()
    try:
        plan.add_room(
            rid,
            rtype,
            width=w,
            length=length,
            level=level,
            ceiling_height=ceiling_h,
            vaulted=vaulted,
            floor=floor_finish,
            **place_kwargs,
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


def _parse_wall_statement(c: _Cursor, plan: Barndominium, lineno: int) -> None:
    a_tok = c.ident("a room id")
    a = a_tok.text
    sep = c.take("'-' or 'to'")
    if sep.text.lower() not in ("-", "to"):
        raise _ParseError(
            "SYNTAX", f"Expected '-' or 'to', got '{sep.text}'.", sep.col, end_col=sep.end_col,
        )
    b_tok = c.ident("the second room id")
    b = b_tok.text
    attrs: list[str] = []
    while c.peek() is not None:
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


def _parse_suite_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
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


def _parse_zone_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
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


# --- statement parsers: openings -------------------------------------------


def _default_interior_door_width(kind: str) -> float:
    if kind == "cased":
        return 6.0
    if kind in DOUBLE_LEAF_KINDS:
        return DEFAULT_DOUBLE_DOOR_WIDTH
    if kind == "bifold":
        return DEFAULT_BIFOLD_DOOR_WIDTH
    return 32 / 12


def _parse_interior_door_statement(
    c: _Cursor, plan: Barndominium, a_tok: _Token, a: str, lineno: int,
) -> None:
    c.take("'-'")  # consume the separator
    b = c.ident("the second room id").text
    kind = "swing"
    if (tok := c.peek()) is not None and tok.text.lower() in _DOOR_KINDS:
        kind = c.take("a door kind").text.lower()
    width = _default_interior_door_width(kind)
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
                hint="Options: a kind (swing/cased/pocket/sliding/bifold/double/french), "
                "width <n>, offset <n>, into <room>, hinge near|far.",
                end_col=c.toks[c.i - 1].end_col,
            )
    c.expect_end()
    plan.connect(a, b, width=width, kind=kind, offset=offset, swing_into=swing_into, hinge=hinge)
    d = plan.interior_doors[-1]
    d.line, d.col, d.end_col = lineno, a_tok.col, a_tok.end_col


def _parse_overhead_door_options(c: _Cursor) -> tuple[float, float, float]:
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
                "(no-egress is implied — an overhead door never counts as egress).",
                end_col=c.toks[c.i - 1].end_col,
            )
    c.expect_end()
    return width, height, offset


def _parse_exterior_door_options(c: _Cursor) -> tuple[float, float, bool, str]:
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
                hint="Options: double|french, width <n>, offset <n>, no-egress.",
                end_col=c.toks[c.i - 1].end_col,
            )
    if ekind in DOUBLE_LEAF_KINDS and not width_given:
        width = DEFAULT_DOUBLE_DOOR_WIDTH
    c.expect_end()
    return width, offset, egress, ekind


def _parse_exterior_door_statement(
    c: _Cursor, plan: Barndominium, a_tok: _Token, a: str, lineno: int,
) -> None:
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
        width, height, offset = _parse_overhead_door_options(c)
        plan.entrance(a, wall, width=width, offset=offset, kind="overhead", height=height)
    else:
        width, offset, egress, ekind = _parse_exterior_door_options(c)
        plan.entrance(a, wall, width=width, offset=offset, egress=egress, kind=ekind)
    ed = plan.exterior_doors[-1]
    ed.line, ed.col, ed.end_col = lineno, a_tok.col, a_tok.end_col


def _parse_door_statement(c: _Cursor, plan: Barndominium, lineno: int) -> None:
    """Unified `door` statement: interior connection or exterior/overhead opening."""
    a_tok = c.ident("a room id")
    a = a_tok.text
    nxt = c.peek()
    if nxt is not None and nxt.text.lower() in ("-", "to"):
        _parse_interior_door_statement(c, plan, a_tok, a, lineno)
    else:
        _parse_exterior_door_statement(c, plan, a_tok, a, lineno)


# --- statement parsers: simple option blocks --------------------------------


def _parse_climate_statement(c: _Cursor, plan: Barndominium) -> None:
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


def _parse_finish_statement(c: _Cursor, plan: Barndominium) -> None:
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


def _parse_roof_statement(c: _Cursor, plan: Barndominium) -> None:
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


def _parse_note_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    text = c.take("a quoted note").text
    nxt = c.peek()
    if nxt is not None and nxt.text.lower() == "at":
        c.keyword("at")
        x = c.number("the note x")
        y = c.number("the note y")
        level = 0
        if (tok := c.peek()) is not None and tok.text.lower() == "level":
            c.keyword("level")
            level = c.level_value()
        c.expect_end()
        plan.note(text, x=x, y=y, level=level)
        nm = plan.note_marks[-1]
        nm.line, nm.col, nm.end_col = lineno, kw.col, kw.end_col
    else:
        plan.note(text)
        c.expect_end()


# --- statement parsers: secondary openings and physical features ------------


def _parse_open_statement(c: _Cursor, plan: Barndominium, lineno: int) -> None:
    a_tok = c.ident("the first room id")
    a = a_tok.text
    sep = c.take("'-' or 'to'")
    if sep.text.lower() not in ("-", "to"):
        raise _ParseError(
            "SYNTAX", f"Expected '-' or 'to', got '{sep.text}'.", sep.col, end_col=sep.end_col,
        )
    b = c.ident("the second room id").text
    width, offset = 6.0, None
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


def _parse_entry_statement(c: _Cursor, plan: Barndominium, lineno: int) -> None:
    rid_tok = c.ident("a room id")
    rid = rid_tok.text
    wall = c.wall()
    width, offset, egress, ekind = _parse_exterior_door_options(c)
    plan.entrance(rid, wall, width=width, offset=offset, egress=egress, kind=ekind)
    ed = plan.exterior_doors[-1]
    ed.line, ed.col, ed.end_col = lineno, rid_tok.col, rid_tok.end_col


def _parse_window_statement(c: _Cursor, plan: Barndominium, lineno: int) -> None:
    rid_tok = c.ident("a room id")
    rid = rid_tok.text
    wall = c.wall()
    kind = "casement"
    if (tok := c.peek()) is not None and tok.text.lower() in _WINDOW_KIND_SET:
        kind = c.take("a window kind").text.lower()
    width, offset = 4.0, 2.0
    sill, head = 3.0, 6.67
    tempered = False
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
        elif opt == "tempered":
            tempered = True
        elif opt == "fixed":
            kind = "fixed"
        else:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown window option '{opt}'.",
                c.toks[c.i - 1].col,
                hint="Options: a kind (casement/slider/fixed/double-hung, right after the wall), "
                "width <n>, offset <n>, sill <n>, head <n>, fixed, tempered.",
                end_col=c.toks[c.i - 1].end_col,
            )
    plan.add_window(
        rid, wall, width=width, offset=offset, sill_height=sill,
        head_height=head, kind=kind, tempered=tempered,
    )
    win = plan.windows[-1]
    win.line, win.col, win.end_col = lineno, rid_tok.col, rid_tok.end_col


def _parse_porch_statement(c: _Cursor, plan: Barndominium) -> None:
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
                "SYNTAX", f"Expected 'covered' or 'open', got '{tag}'.", nxt.col, end_col=nxt.end_col,
            )
    c.expect_end()
    plan.add_porch(pid, x=x, y=y, width=w, length=length, covered=covered)


def _parse_stair_statement(c: _Cursor, plan: Barndominium, lineno: int) -> None:
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
        plan.add_stair(sid_tok.text, x=x, y=y, width=w, length=length, from_level=lo, to_level=hi)
        st = plan.stairs[-1]
        st.line, st.col, st.end_col = lineno, sid_tok.col, sid_tok.end_col
    except ValueError as exc:
        raise _ParseError(
            "BAD_LEVEL",
            str(exc),
            sid_tok.col,
            end_col=sid_tok.end_col,
            hint="A stair connects two different levels, e.g. `from 0 to 1`.",
        )


def _parse_frame_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
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
    plan.frame_spec = FrameSpec(bay, span, post, ridge, lineno, kw.col, kw.end_col)


def _parse_fixture_wall_token(c: _Cursor) -> Direction:
    wt = c.take("a wall (N|S|E|W)")
    wd = _FIXTURE_WALLS.get(wt.text.lower())
    if wd is None:
        raise _ParseError(
            "BAD_WALL",
            f"Unknown wall '{wt.text}'.",
            wt.col,
            end_col=wt.end_col,
            hint="Use N, S, E or W (or north/south/east/west).",
        )
    return wd


@dataclass
class _FixtureOptions:
    x: float | None = None
    y: float | None = None
    wall: Direction | None = None
    offset: float | None = None
    rotation: float = 0.0
    width: float | None = None
    along: Direction | None = None
    run_from: float | None = None
    run_to: float | None = None
    run_depth: float | None = None


def _apply_fixture_option(c: _Cursor, tok: _Token, opt: str, state: _FixtureOptions) -> None:
    if opt == "at":
        state.x = c.number("the fixture x offset")
        state.y = c.number("the fixture y offset")
    elif opt in ("wall", "along"):
        wd = _parse_fixture_wall_token(c)
        if opt == "along":
            state.along = wd
        else:
            state.wall = wd
    elif opt == "offset":
        state.offset = c.number("the fixture offset")
    elif opt == "from":
        state.run_from = c.number("the counter run start")
    elif opt == "to":
        state.run_to = c.number("the counter run end")
    elif opt == "depth":
        state.run_depth = c.number("the counter depth")
    elif opt in ("rotate", "rotation"):
        state.rotation = c.number("the rotation in degrees")
    elif opt == "width":
        state.width = c.number("the fixture width")
    else:
        raise _ParseError(
            "BAD_OPTION",
            f"Unknown fixture option '{tok.text}'.",
            tok.col,
            end_col=tok.end_col,
            hint="Options: at <x>,<y>, wall N|S|E|W [offset <n>], rotate <deg>, "
            "width <w>, or (counter) along N|S|E|W [from <a> to <b>] [depth <d>].",
        )


def _parse_fixture_options(c: _Cursor) -> _FixtureOptions:
    state = _FixtureOptions()
    while (tok := c.peek()) is not None:
        opt = c.take("an option").text.lower()
        _apply_fixture_option(c, tok, opt, state)
    c.expect_end()
    return state


def _parse_fixture_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    from .fixtures import FIXTURES

    kind_tok = c.ident("a fixture kind")
    kind = kind_tok.text.lower()
    if kind not in FIXTURES:
        raise _ParseError(
            "BAD_OPTION",
            f"Unknown fixture kind '{kind_tok.text}'.",
            kind_tok.col,
            end_col=kind_tok.end_col,
            hint=f"Use one of: {', '.join(FIXTURES)}.",
        )
    c.keyword("in")
    room_tok = c.ident("a room id")
    opts = _parse_fixture_options(c)
    try:
        plan.add_fixture(
            kind,
            room_tok.text,
            x=opts.x,
            y=opts.y,
            wall=opts.wall,
            offset=opts.offset,
            rotation=opts.rotation,
            width=opts.width,
            along=opts.along,
            run_from=opts.run_from,
            run_to=opts.run_to,
            depth=opts.run_depth,
        )
    except ValueError as exc:
        raise _ParseError("BAD_OPTION", str(exc), kind_tok.col, end_col=kind_tok.end_col)
    pf = plan.fixtures[-1]
    pf.line, pf.col, pf.end_col = lineno, kw.col, kw.end_col


def _parse_device_statement(c: _Cursor, plan: Barndominium, key: str, kw: _Token, lineno: int) -> None:
    c.keyword("in")
    room_tok = c.ident("a room id")
    wall = None
    offset = 1.0
    gfci = False
    while (tok := c.peek()) is not None:
        opt = c.take("an option").text.lower()
        if opt == "wall":
            wall = _parse_fixture_wall_token(c)
        elif opt == "offset":
            offset = c.number(f"the {key} offset")
        elif opt == "gfci" and key == "outlet":
            gfci = True
        else:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown {key} option '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint=(
                    "Options: wall N|S|E|W, offset <n>, gfci."
                    if key == "outlet"
                    else "Options: wall N|S|E|W, offset <n>."
                ),
            )
    if wall is None:
        raise _ParseError(
            "BAD_WALL",
            f"A `{key}` needs a wall: `{key} in <room> wall N|S|E|W offset <n>`.",
            kw.col,
            end_col=kw.end_col,
            hint="Name the wall (N|S|E|W) the device sits on.",
        )
    c.expect_end()
    if key == "outlet":
        plan.add_outlet(room_tok.text, wall, offset=offset, gfci=gfci)
        outlet = plan.outlets[-1]
        outlet.line, outlet.col, outlet.end_col = lineno, kw.col, kw.end_col
    else:
        plan.add_switch(room_tok.text, wall, offset=offset)
        switch = plan.switches[-1]
        switch.line, switch.col, switch.end_col = lineno, kw.col, kw.end_col


def _parse_light_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    c.keyword("in")
    room_tok = c.ident("a room id")
    c.keyword("at")
    lx = c.number("the light x offset")
    ly = c.number("the light y offset")
    lkind = "ceiling"
    while (tok := c.peek()) is not None:
        opt = c.take("an option").text.lower()
        if opt == "kind":
            kt = c.take("a light kind")
            lkind = kt.text.lower()
            if lkind not in _LIGHT_KIND_SET:
                raise _ParseError(
                    "BAD_OPTION",
                    f"Unknown light kind '{kt.text}'.",
                    kt.col,
                    end_col=kt.end_col,
                    hint=f"Use one of: {', '.join(LIGHT_KINDS)}.",
                )
        else:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown light option '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint="Options: kind ceiling|pendant|fan|recessed.",
            )
    c.expect_end()
    plan.add_light(room_tok.text, x=lx, y=ly, kind=lkind)
    lm = plan.lights[-1]
    lm.line, lm.col, lm.end_col = lineno, kw.col, kw.end_col


def _parse_alarm_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    kind_tok = c.ident("an alarm kind (smoke|co|smoke_co)")
    akind = kind_tok.text.lower()
    if akind not in _ALARM_KIND_SET:
        raise _ParseError(
            "BAD_OPTION",
            f"Unknown alarm kind '{kind_tok.text}'.",
            kind_tok.col,
            end_col=kind_tok.end_col,
            hint=f"Use one of: {', '.join(ALARM_KINDS)} (smoke_co is the combination unit).",
        )
    c.keyword("in")
    room_tok = c.ident("a room id")
    ax = ay = None
    while (tok := c.peek()) is not None:
        opt = c.take("an option").text.lower()
        if opt == "at":
            ax = c.number("the alarm x offset")
            ay = c.number("the alarm y offset")
        else:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown alarm option '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint="Options: at <x>,<y> (room-local; a ceiling device).",
            )
    c.expect_end()
    plan.add_alarm(room_tok.text, akind, x=ax, y=ay)
    am = plan.alarms[-1]
    am.line, am.col, am.end_col = lineno, kw.col, kw.end_col


# --- statement parsers: composition and params ------------------------------


def _parse_use_param_pairs(c: _Cursor, kw: _Token, uparams: dict[str, float]) -> None:
    c.keyword("with")
    seen_pair = False
    while (pt := c.peek()) is not None and "=" in pt.text and not pt.quoted:
        c.take("a param pair")
        seen_pair = True
        name, _, val = pt.text.partition("=")
        if not _PARAM_NAME_RE.match(name):
            raise _ParseError(
                "SYNTAX",
                f"'{name}' is not a valid param name in `with`.",
                pt.col,
                end_col=pt.end_col,
                hint="Write `with width=8, depth=7-6` — a plain name, then `=`, then a number.",
            )
        num = _param_value(val)
        if num is None:
            raise _ParseError(
                "BAD_NUMBER",
                f"`with {name}=` needs a number, got '{val}'.",
                pt.col,
                end_col=pt.end_col,
                hint="Params are numbers only (decimal feet or ft-in, e.g. 8 or 7-6) — "
                "no names or arithmetic in v1.",
            )
        if name in uparams:
            raise _ParseError(
                "PARAM_DUP",
                f"param '{name}' is set twice in this `with` clause.",
                pt.col,
                end_col=pt.end_col,
                hint="Set each param once per `use`.",
            )
        uparams[name] = num
    if not seen_pair:
        nxt = c.peek()
        raise _ParseError(
            "SYNTAX",
            "`with` needs at least one `key=value` param pair.",
            nxt.col if nxt else kw.col,
            end_col=nxt.end_col if nxt else kw.end_col,
            hint="e.g. `with width=8, depth=7-6` (no spaces around `=`).",
        )


@dataclass
class _UseOptions:
    level: int = 0
    mirror: str | None = None
    rotate: int = 0
    params: dict[str, float] = field(default_factory=dict)


def _parse_use_header(c: _Cursor) -> tuple[_Token, _Token, float, float]:
    path_tok = c.take("a quoted part path")
    if not path_tok.quoted:
        raise _ParseError(
            "SYNTAX",
            f"Expected a quoted part path, got '{path_tok.text}'.",
            path_tok.col,
            end_col=path_tok.end_col,
            hint='Quote the relative path, e.g. `use "parts/bath_core.barn" as b at 0,0`.',
        )
    c.keyword("as")
    alias_tok = c.ident("an alias")
    if not _ALIAS_RE.match(alias_tok.text):
        raise _ParseError(
            "SYNTAX",
            f"'{alias_tok.text}' is not a valid alias.",
            alias_tok.col,
            end_col=alias_tok.end_col,
            hint="An alias is a plain identifier (letters, digits, underscore; not starting with a digit), e.g. `as m`.",
        )
    c.keyword("at")
    return path_tok, alias_tok, c.number("the use x"), c.number("the use y")


def _parse_use_mirror(c: _Cursor) -> str:
    c.keyword("mirror")
    axis_tok = c.take("a mirror axis (x or y)")
    axis = axis_tok.text.lower()
    if axis not in ("x", "y"):
        raise _ParseError(
            "BAD_OPTION",
            f"`mirror` takes an axis x or y, got '{axis_tok.text}'.",
            axis_tok.col,
            end_col=axis_tok.end_col,
            hint="`mirror y` flips east↔west; `mirror x` flips north↔south.",
        )
    return axis


def _parse_use_rotation(c: _Cursor) -> int:
    c.keyword("rotate")
    ang_tok = c.take("a rotation of 90, 180 or 270")
    try:
        ang = int(float(ang_tok.text))
    except ValueError:
        ang = -1
    if ang_tok.quoted or ang not in (90, 180, 270):
        raise _ParseError(
            "BAD_OPTION",
            f"`rotate` on `use` takes 90, 180 or 270, got '{ang_tok.text}'.",
            ang_tok.col,
            end_col=ang_tok.end_col,
            hint="Rooms are axis-aligned, so a part turns in 90° steps (90, 180 or 270).",
        )
    return ang


def _parse_use_options(c: _Cursor, kw: _Token) -> _UseOptions:
    opts = _UseOptions()
    while (tok := c.peek()) is not None:
        opt = tok.text.lower()
        if opt == "level":
            c.keyword("level")
            opts.level = c.level_value()
        elif opt == "with":
            _parse_use_param_pairs(c, kw, opts.params)
        elif opt == "mirror":
            opts.mirror = _parse_use_mirror(c)
        elif opt == "rotate":
            opts.rotate = _parse_use_rotation(c)
        else:
            raise _ParseError(
                "BAD_OPTION",
                f"Unknown use option '{tok.text}'.",
                tok.col,
                end_col=tok.end_col,
                hint="Options: level <n>, mirror x|y, rotate 90|180|270, with k=v.",
            )
    return opts


def _parse_use_statement(c: _Cursor, plan: Barndominium, kw: _Token, lineno: int) -> None:
    from .elements import UseSpec

    path_tok, alias_tok, ux, uy = _parse_use_header(c)
    opts = _parse_use_options(c, kw)
    plan.uses.append(
        UseSpec(
            path_tok.text,
            alias_tok.text,
            ux,
            uy,
            opts.level,
            mirror=opts.mirror,
            rotate=opts.rotate,
            params=opts.params,
            line=lineno,
            col=kw.col,
            end_col=kw.end_col,
        )
    )


def _parse_param_statement(tokens: list[_Token], c: _Cursor, plan: Barndominium, kw: _Token) -> None:
    rest = "".join(t.text for t in tokens[1:])
    pname, psep, pval = rest.partition("=")
    if not psep or not _PARAM_NAME_RE.match(pname):
        raise _ParseError(
            "SYNTAX",
            "A param is `param <name> = <number>`.",
            kw.end_col + 1,
            end_col=c.eol_col,
            hint="e.g. `param width = 8` or `param depth = 7-6`.",
        )
    pnum = _param_value(pval)
    if pnum is None:
        raise _ParseError(
            "BAD_NUMBER",
            f"param '{pname}' needs a numeric default, got '{pval}'.",
            kw.end_col + 1,
            end_col=c.eol_col,
            hint="The default is mandatory and a number (decimal feet or ft-in, e.g. 8 or 7-6) — "
            "no names or arithmetic in v1.",
        )
    if pname in plan.params:
        raise _ParseError(
            "PARAM_DUP",
            f"param '{pname}' is declared more than once.",
            kw.end_col + 1,
            end_col=c.eol_col,
            hint="Declare each param once.",
        )
    plan.params[pname] = pnum


# --- statement parsers: simple plan headers ---------------------------------


def _parse_plan_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.name = c.ident("a plan name").text
    c.expect_end()


def _parse_envelope_statement(c: _Cursor, plan: Barndominium) -> None:
    w = c.number("envelope width")
    c.keyword("x")
    length = c.number("envelope length")
    plan.envelope(w, length)
    c.expect_end()


def _parse_wing_statement(c: _Cursor, plan: Barndominium) -> None:
    w = c.number("wing width")
    c.keyword("x")
    length = c.number("wing length")
    c.keyword("at")
    x = c.number("wing x")
    y = c.number("wing y")
    c.expect_end()
    plan.wing(w, length, x=x, y=y)


def _parse_ceiling_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.ceiling(c.number("ceiling height"))
    c.expect_end()


def _parse_floor_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.floors(c.number("floor assembly depth"))
    c.expect_end()


def _parse_accessible_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.mark_accessible()
    c.expect_end()


def _parse_electrical_marker_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.mark_electrical()
    c.expect_end()


def _parse_street_statement(c: _Cursor, plan: Barndominium) -> None:
    street_wall = c.wall()
    c.expect_end()
    plan.set_street(street_wall)


def _parse_overhang_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.set_overhang(c.number("the overhang depth in feet"))
    c.expect_end()


def _parse_orientation_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.orient(c.number("the orientation in degrees"))
    c.expect_end()


def _parse_grade_statement(c: _Cursor, plan: Barndominium) -> None:
    plan.set_grade(c.number("the finish-floor height above grade"))
    c.expect_end()


class _Stmt(NamedTuple):
    """Everything a statement parser in :data:`_STATEMENT_PARSERS` may need."""

    c: _Cursor
    plan: Barndominium
    smap: _SourceMap
    kw: _Token
    key: str
    lineno: int
    tokens: list[_Token]


#: Statement keyword -> its parser. The ONE list of statements: the public
#: :data:`STATEMENT_KEYWORDS` (hints, fmt, playground highlighting, LSP
#: completions, the agent's DSL sniffing) derives from it, so a statement can't be
#: parseable without being known everywhere else, or vice versa.
_STATEMENT_PARSERS: dict[str, Callable[[_Stmt], None]] = {
    "plan": lambda s: _parse_plan_statement(s.c, s.plan),
    "envelope": lambda s: _parse_envelope_statement(s.c, s.plan),
    "wing": lambda s: _parse_wing_statement(s.c, s.plan),
    "ceiling": lambda s: _parse_ceiling_statement(s.c, s.plan),
    "floor": lambda s: _parse_floor_statement(s.c, s.plan),
    "note": lambda s: _parse_note_statement(s.c, s.plan, s.kw, s.lineno),
    "program": lambda s: _parse_program_statement(s.c, s.plan, s.kw, s.lineno),
    "require": lambda s: _parse_require_statement(s.c, s.plan, s.kw, s.lineno),
    "room": lambda s: _parse_room_statement(s.c, s.plan, s.smap, s.lineno),
    "wall": lambda s: _parse_wall_statement(s.c, s.plan, s.lineno),
    "door": lambda s: _parse_door_statement(s.c, s.plan, s.lineno),
    "open": lambda s: _parse_open_statement(s.c, s.plan, s.lineno),
    "entry": lambda s: _parse_entry_statement(s.c, s.plan, s.lineno),
    "window": lambda s: _parse_window_statement(s.c, s.plan, s.lineno),
    "porch": lambda s: _parse_porch_statement(s.c, s.plan),
    "stair": lambda s: _parse_stair_statement(s.c, s.plan, s.lineno),
    "frame": lambda s: _parse_frame_statement(s.c, s.plan, s.kw, s.lineno),
    "roof": lambda s: _parse_roof_statement(s.c, s.plan),
    "orientation": lambda s: _parse_orientation_statement(s.c, s.plan),
    "finish": lambda s: _parse_finish_statement(s.c, s.plan),
    "accessible": lambda s: _parse_accessible_statement(s.c, s.plan),
    "site": lambda s: _parse_site_statement(s.c, s.plan, s.kw, s.lineno),
    "setback": lambda s: _parse_setback_statement(s.c, s.plan, s.kw, s.lineno),
    "building": lambda s: _parse_building_statement(s.c, s.plan, s.kw, s.lineno),
    "suite": lambda s: _parse_suite_statement(s.c, s.plan, s.kw, s.lineno),
    "zone": lambda s: _parse_zone_statement(s.c, s.plan, s.kw, s.lineno),
    "electrical": lambda s: _parse_electrical_marker_statement(s.c, s.plan),
    "street": lambda s: _parse_street_statement(s.c, s.plan),
    "overhang": lambda s: _parse_overhang_statement(s.c, s.plan),
    "climate": lambda s: _parse_climate_statement(s.c, s.plan),
    "fixture": lambda s: _parse_fixture_statement(s.c, s.plan, s.kw, s.lineno),
    "outlet": lambda s: _parse_device_statement(s.c, s.plan, s.key, s.kw, s.lineno),
    "switch": lambda s: _parse_device_statement(s.c, s.plan, s.key, s.kw, s.lineno),
    "light": lambda s: _parse_light_statement(s.c, s.plan, s.kw, s.lineno),
    "alarm": lambda s: _parse_alarm_statement(s.c, s.plan, s.kw, s.lineno),
    "use": lambda s: _parse_use_statement(s.c, s.plan, s.kw, s.lineno),
    "param": lambda s: _parse_param_statement(s.tokens, s.c, s.plan, s.kw),
    "drive": lambda s: _parse_drive_statement(s.c, s.plan, s.kw, s.lineno),
    "walk": lambda s: _parse_walk_statement(s.c, s.plan, s.kw, s.lineno),
    "well": lambda s: _parse_well_statement(s.c, s.plan, s.kw, s.lineno),
    "septic": lambda s: _parse_septic_statement(s.c, s.plan, s.kw, s.lineno),
    "service": lambda s: _parse_service_statement(s.c, s.plan, s.kw, s.lineno),
    "grade": lambda s: _parse_grade_statement(s.c, s.plan),
}

#: Every statement keyword, in reference order (for hints and did-you-mean).
STATEMENT_KEYWORDS: tuple[str, ...] = tuple(_STATEMENT_PARSERS)


def _parse_statement(
    tokens: list[_Token], plan: Barndominium, smap: _SourceMap, lineno: int,
    param_env: dict[str, float] | None = None,
) -> None:
    c = _Cursor(tokens, param_env=param_env)
    kw = c.take("a statement keyword")
    key = kw.text.lower()
    handler = _STATEMENT_PARSERS.get(key)
    if handler is None:
        raise _ParseError(
            "UNKNOWN_STMT",
            f"{_did_you_mean(kw.text, STATEMENT_KEYWORDS)}Unknown statement '{kw.text}'.",
            kw.col,
            hint=f"Statements start with one of: {', '.join(STATEMENT_KEYWORDS)}.",
            end_col=kw.end_col,
        )
    handler(_Stmt(c, plan, smap, kw, key, lineno, tokens))


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
    #: Map of room id -> the 1-based source line of its ``room`` statement, for
    #: tools that rewrite the text surgically (see :mod:`barndsl.edits`). Empty
    #: for a plan built through the Python API rather than compiled from text.
    room_lines: dict[str, int] = field(default_factory=dict)

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
                    "accepted": getattr(d, "accepted", False),
                    "accept_reason": getattr(d, "accept_reason", None),
                    "file": getattr(d, "file", None),
                    "part": getattr(d, "part", None),
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


def _recovery_limit(what: str) -> Issue:
    return Issue(
        Severity.WARNING,
        "RECOVERY_LIMIT",
        f"{what} could not run on the partial plan; diagnostics are incomplete.",
        hint="Fix the parse error(s) above to get the full report.",
    )


def _parse_source_lines(
    source: str,
    plan: Barndominium,
    smap: _SourceMap,
    diagnostics: list[Issue],
    *,
    fragment: bool,
    param_env: dict[str, float] | None,
) -> bool:
    """Parse all source lines into ``plan``; return True if recovery skipped any."""
    skipped = False
    for lineno, raw in enumerate(source.splitlines(), start=1):
        toks = _tokenize_line(raw, lineno)
        unterminated = next((t for t in toks if t.unterminated), None)
        if unterminated is not None:
            ftin_hint = _ftin_string_hint(raw, unterminated.col)
            diagnostics.append(Issue(
                Severity.ERROR,
                "UNTERMINATED_STRING",
                "String literal has no closing '\"'.",
                line=lineno,
                col=unterminated.col,
                end_col=unterminated.end_col,
                hint=ftin_hint or 'Add the closing quote, e.g. `plan "Name"`.',
            ))
            skipped = True
            continue
        if not toks:
            skipped = _record_empty_line_syntax(raw, lineno, diagnostics) or skipped
            continue
        key = toks[0].text.lower()
        if _record_fragment_host_statement(key, toks[0], lineno, diagnostics, fragment):
            skipped = True
            continue
        if _record_param_in_plan(key, toks[0], lineno, diagnostics, fragment):
            skipped = True
            continue
        try:
            _parse_statement(toks, plan, smap, lineno, param_env=param_env)
        except _ParseError as err:
            diagnostics.append(Issue(
                Severity.ERROR,
                err.code,
                err.message,
                line=lineno,
                col=err.col,
                end_col=err.end_col,
                hint=err.hint,
            ))
            skipped = True
    return skipped


def _record_empty_line_syntax(raw: str, lineno: int, diagnostics: list[Issue]) -> bool:
    """Flag punctuation-only lines; return True when a diagnostic was added."""
    stripped = raw.split("#", 1)[0]
    residue = [ch for ch in stripped if not ch.isspace() and ch not in _DROP]
    if not residue:
        return False
    col = next(i for i, ch in enumerate(stripped) if not ch.isspace()) + 1
    diagnostics.append(Issue(
        Severity.ERROR,
        "SYNTAX",
        "Line has no statement keyword.",
        line=lineno,
        col=col,
        end_col=col + 1,
        hint=f"Each line is one statement; start with one of: {', '.join(STATEMENT_KEYWORDS)}.",
    ))
    return True


def _record_fragment_host_statement(
    key: str, tok: _Token, lineno: int, diagnostics: list[Issue], fragment: bool,
) -> bool:
    if not (fragment and key in HOST_ONLY_STATEMENTS):
        return False
    diagnostics.append(Issue(
        Severity.ERROR,
        "PART_HOST_STMT",
        f"`{key}` describes a whole building — a part borrows the host's. "
        "Remove it; size the part by its rooms.",
        line=lineno,
        col=tok.col,
        end_col=tok.end_col,
        hint="A part is any `.barn` file with no `plan` header: rooms, openings, "
        "windows, fixtures, devices — in its own local feet.",
    ))
    return True


def _record_param_in_plan(
    key: str, tok: _Token, lineno: int, diagnostics: list[Issue], fragment: bool,
) -> bool:
    if fragment or key != "param":
        return False
    diagnostics.append(Issue(
        Severity.ERROR,
        "PARAM_IN_PLAN",
        "`param` declares a part parameter — a whole plan can't take one.",
        line=lineno,
        col=tok.col,
        end_col=tok.end_col,
        hint="Move `param` into a part file (a `.barn` with no `plan` header); "
        "the host passes values with `use ... with name=value`.",
    ))
    return True


def _compose_plan_uses(
    plan: Barndominium,
    diagnostics: list[Issue],
    profile: "Profile | None",
    base_dir: str | None,
    compose_ctx: object | None,
    self_path: str | None,
) -> object | None:
    if not plan.uses:
        return None
    from .compose import _ComposeCtx, compose_uses

    ctx = compose_ctx if isinstance(compose_ctx, _ComposeCtx) else _ComposeCtx.top_level(base_dir, self_path)
    return compose_uses(plan, base_dir, diagnostics, profile, ctx=ctx)


def _place_frame_for_compile(plan: Barndominium, diagnostics: list[Issue], skipped: bool) -> None:
    if plan.frame_spec is None:
        return
    from .structure import place_frame

    try:
        place_frame(plan)
    except Exception:
        if not skipped:
            raise
        diagnostics.append(_recovery_limit("Frame placement"))


def _append_validation_issues(
    plan: Barndominium,
    diagnostics: list[Issue],
    smap: _SourceMap,
    profile: "Profile | None",
    composition: object | None,
    skipped: bool,
) -> None:
    try:
        report: ValidationReport | None = validate(plan, profile)
    except Exception:
        if not skipped:
            raise
        diagnostics.append(_recovery_limit("Validation"))
        return
    _append_report_issues(report, diagnostics, smap, composition)


def _anchor_issue_to_room_source(iss: Issue, smap: _SourceMap) -> None:
    if iss.room is None:
        return
    if iss.line is None:
        iss.line = smap.room_line.get(iss.room)
    if iss.col is None and iss.room in smap.room_col:
        iss.col, iss.end_col = smap.room_col[iss.room]


def _append_report_issues(
    report: ValidationReport | None,
    diagnostics: list[Issue],
    smap: _SourceMap,
    composition: object | None,
) -> None:
    if report is None:
        return
    stamped_map = getattr(composition, "stamped_map", {}) if composition is not None else {}
    part_keys = getattr(composition, "part_keys", {}) if composition is not None else {}
    for iss in report.issues:
        if iss.room is not None and iss.room in stamped_map:
            local, inst = stamped_map[iss.room]
            if (iss.code, local) in part_keys.get(inst.part_path, ()):  # part-internal duplicate
                continue
            iss.line, iss.col, iss.end_col = inst.line, inst.col, inst.end_col
            iss.message = f"instance {inst.alias}: {iss.message}"
            diagnostics.append(iss)
            continue
        _anchor_issue_to_room_source(iss, smap)
        diagnostics.append(iss)


def _append_fragment_issue(
    iss: Issue,
    diagnostics: list[Issue],
    smap: _SourceMap,
    stamped_map: dict,
    part_keys: dict,
    local_codes: frozenset[str],
) -> None:
    if iss.code not in local_codes:
        return
    if iss.room is not None and iss.room in stamped_map:
        local, inst = stamped_map[iss.room]
        if (iss.code, local) in part_keys.get(inst.part_path, ()):  # nested part duplicate
            return
        iss.line, iss.col, iss.end_col = inst.line, inst.col, inst.end_col
        iss.message = f"instance {inst.alias}: {iss.message}"
        diagnostics.append(iss)
        return
    _anchor_issue_to_room_source(iss, smap)
    diagnostics.append(iss)


def _append_fragment_validation_issues(
    report: ValidationReport | None,
    diagnostics: list[Issue],
    smap: _SourceMap,
    composition: object | None,
    local_codes: frozenset[str],
) -> None:
    if report is None:
        return
    stamped_map = getattr(composition, "stamped_map", {}) if composition is not None else {}
    part_keys = getattr(composition, "part_keys", {}) if composition is not None else {}
    for iss in report.issues:
        _append_fragment_issue(iss, diagnostics, smap, stamped_map, part_keys, local_codes)


def _finish_fragment(
    plan: Barndominium,
    diagnostics: list[Issue],
    source: str,
    smap: "_SourceMap",
    pragmas: list,
    profile: "Profile | None",
    composition: object | None = None,
) -> CompileResult:
    """Finish a fragment (part) compile: PART_EMPTY / origin normalization /
    local-only validation. See :func:`compile_source` (``fragment=True``).

    ``composition`` (Phase 20 — nested parts) carries the part's own stamped
    nested instances so a local finding on a nested stamped room is deduped
    against the nested part's already-folded part-internal diagnostics, exactly
    as the host does for its stamps."""
    from .compose import PART_LOCAL_CODES, normalize_part_origin
    from .pragma import apply_pragmas

    if not plan.rooms:
        diagnostics.append(Issue(
            Severity.ERROR, "PART_EMPTY",
            "A part declares no rooms — an empty part composes nothing.",
            hint="Add at least one `room`, e.g. `room bath: bathroom at 0,0 size 8 x 8`.",
        ))
        apply_pragmas(diagnostics, pragmas)
        return CompileResult(plan, diagnostics, source, room_lines=dict(smap.room_line))

    if normalize_part_origin(plan) != (0.0, 0.0):
        diagnostics.append(Issue(
            Severity.INFO, "PART_ORIGIN",
            "Part's south-west corner wasn't at 0,0 — normalized to the origin "
            "before stamping.",
            hint="Parts are authored in their own local feet; the `use ... at` "
            "places this corner.",
        ))
    # A synthetic envelope covering the part lets the local checks run without an
    # ENVELOPE error; the composed host validate is the real placement authority.
    plan.envelope_width = max(r.x + r.width for r in plan.rooms)
    plan.envelope_length = max(r.y + r.length for r in plan.rooms)

    try:
        report = validate(plan, profile)
    except Exception:
        report = None
    _append_fragment_validation_issues(report, diagnostics, smap, composition, PART_LOCAL_CODES)
    apply_pragmas(diagnostics, pragmas)
    return CompileResult(plan, diagnostics, source, room_lines=dict(smap.room_line))


def compile_source(
    source: str,
    name: str | None = None,
    profile: "Profile | None" = None,
    *,
    fragment: bool = False,
    base_dir: str | None = None,
    params: dict[str, float] | None = None,
    compose_ctx: object | None = None,
    self_path: str | None = None,
) -> CompileResult:
    """Compile DSL ``source`` into a validated plan + diagnostics.

    ``profile`` selects the jurisdiction thresholds the code checks compare
    against (see :mod:`barndsl.profiles`); ``None`` uses the IRC baseline
    (:data:`~barndsl.profiles.DEFAULT`), which is byte-identical to the
    pre-profile behaviour.

    Cross-file composition (see :mod:`barndsl.compose`):

    * ``base_dir`` is the directory ``use "<relpath>"`` paths resolve against —
      the including file's own directory. ``None`` (a pasted/browser source with
      no home directory) makes any ``use`` a ``USE_UNRESOLVED`` error.
    * ``fragment=True`` compiles a **part** file (a ``.barn`` with no ``plan``
      header): no ``plan``/``envelope`` is required, host-only statements are
      ``PART_HOST_STMT`` errors, at least one ``room`` is required (``PART_EMPTY``),
      the origin is normalized to the SW corner (``PART_ORIGIN`` info), and only
      *local* checks run (whole-building checks are skipped). A part may itself
      ``use`` nested parts (depth ≤ 2) and may carry ``level 1`` rooms + a stair.
      This is what the loader calls once per part; ordinary top-level compiles use
      ``fragment=False``.
    * ``params`` (Phase 20) are the use-site parameter overrides for a parametric
      part — merged over the part's declared ``param`` defaults to build the
      resolution environment a bare param name reads from.
    * ``compose_ctx`` / ``self_path`` are internal recursion state threaded by the
      loader for nested composition (the sandbox root, depth, cycle stack, shared
      memo + instance budget). External callers leave them ``None``.
    """
    from .pragma import apply_pragmas, parse_pragmas

    # Strip a leading UTF-8 BOM for API callers who pass raw file text (the CLI's
    # read helper strips it too; stripping here keeps direct compile_source users
    # from a stray U+FEFF making the first token unlexable).
    source = _strip_bom(source)
    diagnostics: list[Issue] = []
    plan = Barndominium(name=name or "Untitled")
    smap = _SourceMap()
    pragmas = parse_pragmas(source)
    # Parametric parts (Phase 20): pre-scan `param` defaults so a bare param name
    # resolves as a number anywhere in a part, then overlay the use-site values.
    # Only meaningful in a part (fragment); a plan's `param` is a PARAM_IN_PLAN
    # error and never builds an environment.
    param_env: dict[str, float] | None = None
    if fragment:
        param_env = {**_scan_param_defaults(source), **(params or {})}
    # True once any statement was skipped by parse-error recovery. Tracked at
    # the skip sites themselves (not inferred from ERROR diagnostics later):
    # semantic build errors also record ERRORs but skip nothing, and they must
    # keep the historical unguarded frame/validate behaviour.
    skipped = _parse_source_lines(
        source, plan, smap, diagnostics, fragment=fragment, param_env=param_env,
    )

    # Cross-file composition: resolve + stamp every `use` into `plan` BEFORE
    # validation, so overlap/envelope/egress/adjacency run on the composed plan.
    # Resolution errors and (deduped) part-internal diagnostics are appended now;
    # placement-dependent (instance) findings are reclassified after validate.
    # This runs in BOTH modes (Phase 20 — a part may `use` nested parts, depth ≤
    # 2); ``compose_ctx`` carries the recursion state (sandbox root, depth, cycle
    # stack, shared memo + instance budget), built fresh at the top level.
    composition = _compose_plan_uses(plan, diagnostics, profile, base_dir, compose_ctx, self_path)

    # Fragment mode (a part file): no plan/envelope required, ≥1 room, origin
    # normalized, only local checks. The loader (compose.load_part) calls this.
    if fragment:
        return _finish_fragment(
            plan, diagnostics, source, smap, pragmas, profile, composition
        )

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
        apply_pragmas(diagnostics, pragmas)
        return CompileResult(None, diagnostics, source, room_lines=dict(smap.room_line))

    # Derive the structural frame (if requested) before checks, so the validator
    # and renderer see the placed posts/beams. On a partial (statements-skipped)
    # plan the incomplete geometry may defeat the placer or a check, so guard
    # those on the recovery path — a syntax error must never become a crash, but
    # the swallow is *recorded* so incomplete diagnostics can't pass as complete.
    # A clean or semantic-error compile keeps the original, unguarded behaviour,
    # so a real bug still bites.
    _place_frame_for_compile(plan, diagnostics, skipped)
    _append_validation_issues(plan, diagnostics, smap, profile, composition, skipped)
    # Suppression pragmas run last, once every diagnostic carries its resolved
    # line (semantic issues were just anchored to their room's statement line):
    # a pragma downgrades the matched warnings/infos to accepted INFOs and flags
    # any that can't be honoured.
    apply_pragmas(diagnostics, pragmas)
    return CompileResult(
        plan, diagnostics, source, recovered=skipped, room_lines=dict(smap.room_line)
    )


class SourceReadError(Exception):
    """A file the user pointed at could not be read — missing, a directory, no
    permission, or not UTF-8 text. Carries a clean one-line ``<path>: <reason>``
    message so the CLI can print ``error: …`` and exit 2 instead of dumping a
    traceback from deep in the I/O stack."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _strip_bom(text: str) -> str:
    """Drop a leading UTF-8 byte-order mark. Some editors (Notepad, older VS on
    Windows) prepend U+FEFF; left in, it makes the first token unlexable."""
    return text[1:] if text.startswith("﻿") else text


def read_source_file(path: str) -> tuple[str, str | None]:
    """Read DSL source for a file-taking command: ``(text, base_dir)``.

    ``-`` reads standard input (an untitled buffer — ``base_dir`` ``None``, so a
    ``use`` relpath is unresolvable, the same as a pasted source). Otherwise the
    file's UTF-8 text (any leading BOM stripped) with its own directory as the
    ``use`` resolution root. Raises :class:`SourceReadError` — never a raw
    traceback — for a missing file, a directory, a permission error, or bytes
    that are not valid UTF-8."""
    if path == "-":
        return _strip_bom(sys.stdin.read()), None
    # On Windows, opening a directory can raise PermissionError instead of
    # IsADirectoryError. Check first so CLI diagnostics stay stable and helpful.
    if os.path.isdir(path):
        raise SourceReadError(path, "is a directory") from None
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise SourceReadError(path, "no such file") from None
    except IsADirectoryError:
        raise SourceReadError(path, "is a directory") from None
    except PermissionError:
        raise SourceReadError(path, "permission denied") from None
    except UnicodeDecodeError:
        raise SourceReadError(path, "not valid UTF-8 text") from None
    return _strip_bom(text), os.path.dirname(os.path.abspath(path))


def compile_file(path: str, profile: "Profile | None" = None) -> CompileResult:
    """Compile a ``.barn`` file (see :func:`compile_source` for ``profile``).

    The file's own directory is the resolution root for any ``use "<relpath>"``
    (cross-file composition) — parts are found relative to the including file.
    ``path`` may be ``-`` to read standard input. A file that cannot be read
    raises :class:`SourceReadError` (a clean message, no traceback)."""
    text, base_dir = read_source_file(path)
    self_path = None if path == "-" else os.path.realpath(path)
    return compile_source(text, profile=profile, base_dir=base_dir, self_path=self_path)
