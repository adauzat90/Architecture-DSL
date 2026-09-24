"""Central registry of diagnostic codes.

Every code the compiler (``compiler.py``) or validator (``validation.py``) can
emit is catalogued here with its usual severity, a one-line title, a longer
explanation (often citing the IRC clause behind a code check) and a general
hint on how to fix it. The entry also says which other severities the code can
fire at, whether a composed part reports it once for itself, and whether an
``accept`` pragma may waive it. The codes themselves still live as string
literals at each call site (so the emitting code reads naturally); this module
is the *documentation* surface and the source for ``barndsl explain <CODE>``.

Keeping a single table also makes the set of codes auditable: ``barndsl dev
audit`` checks that the registry covers every code the checks emit, at a
severity its entry allows, and that every entry is explained and has a hint.
"""

from __future__ import annotations

from dataclasses import dataclass

from .issues import Severity


@dataclass(frozen=True)
class CodeInfo:
    """A documented diagnostic code: everything the tools know about a code
    lives on its entry, except its category, which :func:`diagnostic_category`
    derives from the explicit sets and prefix rules below."""

    code: str
    #: The severity this code is *usually* emitted at.
    severity: Severity
    title: str
    explanation: str
    #: The general answer to "how do I fix this?", independent of any plan.
    #: Each emitted :class:`~barndsl.issues.Issue` carries its own, specific hint.
    hint: str
    #: Every severity the code can fire at. More than one means it depends on
    #: context (noted in ``explanation``): ``NO_ACCESS`` is an error for most
    #: rooms but a warning for a closet/pantry/loft; ``SHOP_DEPTH`` is a warning
    #: under 12 ft (can't do the job) but an info under 20 (just tight).
    #: ``barndsl dev audit`` checks every emit site against this set.
    severities: frozenset[Severity]
    #: Part-internal: a finding about a used part's own rooms, fixtures,
    #: openings or devices that fires wherever the part is placed. Only these
    #: survive a part's fragment compile, so they are reported once for the part
    #: instead of once per ``use`` (see :mod:`barndsl.compose`). Kept
    #: conservative: a code left out simply surfaces per use instead of once,
    #: never the reverse (which would wrongly dedupe a real placement issue).
    part_local: bool = False
    #: An ``accept`` pragma may not waive it, even though it fires as a warning
    #: or info: it describes a broken *building*, not a jurisdiction judgement
    #: call, so waiving it would let a structurally broken plan score high on a
    #: technicality (see :mod:`barndsl.pragma`). Kept small and deliberate.
    accept_denied: bool = False

    def __post_init__(self) -> None:
        if self.severity not in self.severities:
            raise ValueError(f"{self.code}: its usual severity must be one it allows")

    @property
    def varies(self) -> bool:
        """True when the code's severity depends on context."""
        return len(self.severities) > 1

    @property
    def category(self) -> str:
        """Stable owner/domain bucket used by docs, matrices, and agents."""
        return diagnostic_category(self.code)

    @property
    def owner(self) -> str:
        """Human-facing subsystem owner for the diagnostic category."""
        return diagnostic_owner(self.category)


CATEGORY_OWNERS: dict[str, str] = {
    "pragma": "diagnostic suppression / audit trail",
    "syntax": "compiler parser and recovery",
    "composition": "part composition and stamped ids",
    "program": "program/require/spec matching",
    "geometry": "plan geometry and room placement",
    "opening": "doors, windows, openings, and thresholds",
    "access_egress": "accessibility, egress, light, and ventilation",
    "circulation": "room access, halls, privacy, and adjacency flow",
    "fixtures": "fixtures, appliances, clearances, and wet groups",
    "electrical": "electrical and life-safety devices",
    "structure": "frame, roof, stair, loft, porch, and load path",
    "site": "site, drive, solar, grade, well, and septic",
    "quality": "design-quality coaching and soft livability rules",
    "finish": "finishes and material notes",
    "export": "downstream export/build feedback",
}

_SYNTAX_CODES = frozenset({
    "UNTERMINATED_STRING", "SYNTAX", "BAD_NUMBER", "BAD_LEVEL", "BAD_COUNT",
    "EMPTY_ID", "BAD_TYPE", "BAD_WALL", "EXTRA_TOKENS", "BAD_PLACEMENT",
    "PLACE_REF", "BAD_OPTION", "UNKNOWN_STMT", "EMPTY", "TRUNCATED",
    "RECOVERY_LIMIT", "DIM_IMPLAUSIBLE", "DUP_ID", "CEILING",
})
_SITE_PREFIXES = ("SITE", "SETBACK", "DRIVE", "APPROACH", "SOLAR", "WELL", "SEPTIC")
_FIXTURE_PREFIXES = ("FIXTURE", "RANGE", "KITCHEN", "LAUNDRY", "BATH_CLEARANCE", "DRYER", "SINK", "PLUMBING", "WATER_HEATER", "WET_GROUP", "COUNTER")
_OPENING_PREFIXES = ("DOOR", "WINDOW", "OPENING", "OPEN_", "OVERHEAD", "ENTRY_INTERIOR")
_STRUCTURE_PREFIXES = ("FRAME", "BAY", "ROOF", "POST", "LOAD", "STAIR", "LOFT", "PORCH", "WING")
_ELECTRICAL_PREFIXES = ("ELECTRICAL", "OUTLET", "RECEPTACLE", "ROOM_NO_LIGHT", "ALARM", "DEVICE")
_COMPOSITION_PREFIXES = ("USE", "PARAM", "PART", "SUITE", "ZONE")
_ACCESS_EGRESS_CODES = frozenset({
    "BEDROOM_EGRESS", "EGRESS_SIZE", "EGRESS_DOOR", "NAT_LIGHT", "VENT_AREA",
    "BATH_VENT", "ACCESS_ENTRY", "ACCESS_DOOR", "ACCESS_BATH", "ACCESS_SINGLE_FLOOR",
    "WINDOW_FALL", "WINDOW_TEMPERED", "WINDOW_SILL",
})
_CIRCULATION_PREFIXES = ("HALL", "GARAGE", "PANTRY", "MUDROOM")
_CIRCULATION_CODES = frozenset({
    "NO_ENTRY", "NO_ACCESS", "NO_BACK_DOOR", "ENTRY_PRIVATE", "PRIVATE_PASSTHROUGH",
    "CLOSET_ACCESS", "CLOSET_DOOR_SWING", "MASTER_ENSUITE", "BATH_DISTANCE",
    "BED_PRIVACY", "BED_SOUND", "GARAGE_BEDROOM", "GARAGE_PASSTHROUGH",
    "FOYER_FLOW", "STORAGE_ACCESS", "MECH_ACCESS", "MECH_BEDROOM", "SAFE_ROOM_ACCESS",
})
_PROGRAM_CODES = frozenset({"NO_PROGRAM", "PROGRAM_MISMATCH", "PROGRAM_AREA_OVERRUN", "BRIEF_ACCEPTANCE", "REQUIRE_REF", "REQUIRE_UNMET", "NO_BATH"})
_GEOMETRY_PREFIXES = ("AREA", "ROOM", "FOOTPRINT", "OUT_OF_BOUNDS", "OVERLAP", "ENVELOPE")
_GEOMETRY_CODES = frozenset({
    "WALL_REF", "WALL_NOADJ", "NOTE_OUTSIDE", "ROOM_SIZE", "ROOM_HABITABLE", "ROOM_CLEAR",
})
_OPENING_CODES = frozenset({"ENTRY_INTERIOR", "DOOR_THRESHOLD", "SELF_DOOR"})
_STRUCTURE_CODES = frozenset({"WALL_BEARING_AXIS"})
_FIXTURE_CODES = frozenset({"WALL_UNUSED"})
_QUALITY_CODES = frozenset({
    "DESIGN", "LOW_STORAGE", "NO_CLOSET", "CLOSET_DEPTH", "CLOSET_SHAPE", "CLOSET_WINDOW",
    "BATH_OVERSIZE", "BEDROOM_AREA", "BEDROOM_DIM", "BED_CLEARANCE", "DINING_CLEARANCE",
    "OFFICE_CLEARANCE", "SHOP_DEPTH", "SHOP_DOOR_HEIGHT",
    "ROOM_PROPORTION", "ROOM_TIGHT", "KITCHEN_FLOW", "ENERGY_ENVELOPE",
    "FLEX_FUTURE_BED", "FOYER_SHAPE", "GREAT_ROOM_FLOW", "GREAT_ROOM_SCALE",
    "REC_ROOM_NOISE", "REC_ROOM_SCALE", "STORAGE_SHAPE", "MECH_CLEARANCE",
    "SAFE_ROOM_SIZE", "SAFE_ROOM_WINDOW", "SAFE_ROOM_EXTERIOR",
})

#: Explicitly listed codes -> category. Checked BEFORE the prefix rules, so an
#: explicit listing always wins (``ROOM_TIGHT`` is quality coaching even though
#: ``ROOM`` prefixes geometry). The sets are disjoint (tested).
_EXPLICIT_CATEGORIES: dict[str, str] = {
    **dict.fromkeys(_SYNTAX_CODES, "syntax"),
    **dict.fromkeys(_PROGRAM_CODES, "program"),
    **dict.fromkeys(_ACCESS_EGRESS_CODES, "access_egress"),
    **dict.fromkeys(_CIRCULATION_CODES, "circulation"),
    **dict.fromkeys(_GEOMETRY_CODES, "geometry"),
    **dict.fromkeys(_OPENING_CODES, "opening"),
    **dict.fromkeys(_STRUCTURE_CODES, "structure"),
    **dict.fromkeys(_FIXTURE_CODES, "fixtures"),
    **dict.fromkeys(_QUALITY_CODES, "quality"),
    "FLOOR_FINISH": "finish",
}

#: Prefix rules, in precedence order, for codes not listed explicitly.
_PREFIX_CATEGORIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ACCEPT_",), "pragma"),
    (_COMPOSITION_PREFIXES, "composition"),
    (_SITE_PREFIXES, "site"),
    (_ELECTRICAL_PREFIXES, "electrical"),
    (_FIXTURE_PREFIXES, "fixtures"),
    (_OPENING_PREFIXES, "opening"),
    (_STRUCTURE_PREFIXES, "structure"),
    (_CIRCULATION_PREFIXES, "circulation"),
    (_GEOMETRY_PREFIXES, "geometry"),
    (("REVIT_",), "export"),
)

#: The bucket for a code no rule classifies (``unclassified_codes`` lists them).
_DEFAULT_CATEGORY = "quality"


def _classified_category(code: str) -> str | None:
    c = code.strip().upper()
    if c in _EXPLICIT_CATEGORIES:
        return _EXPLICIT_CATEGORIES[c]
    for prefixes, category in _PREFIX_CATEGORIES:
        if c.startswith(prefixes):
            return category
    return None


def diagnostic_category(code: str) -> str:
    """Return the stable domain bucket for a diagnostic code."""
    return _classified_category(code) or _DEFAULT_CATEGORY


def unclassified_codes() -> list[str]:
    """Registered codes that only reach the default bucket by falling through.

    Every registered code should be listed explicitly or match a prefix rule, so
    a new rule's owner is a decision rather than an accident; the audit gate
    (``barndsl dev audit``) fails on any code this returns."""
    return sorted(code for code in REGISTRY if _classified_category(code) is None)


def diagnostic_owner(category: str) -> str:
    """Return a human-facing owner label for a diagnostic category."""
    return CATEGORY_OWNERS.get(category, "general validation")


def _c(
    code: str,
    severity: Severity,
    title: str,
    explanation: str,
    *,
    hint: str,
    also: tuple[Severity, ...] = (),
    part_local: bool = False,
    accept_denied: bool = False,
) -> tuple[str, CodeInfo]:
    """One registry entry. ``also`` lists the other severities a
    context-dependent code can fire at."""
    return code, CodeInfo(
        code, severity, title, explanation, hint,
        frozenset({severity, *also}), part_local, accept_denied,
    )


E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO

#: code -> CodeInfo. Grouped by emitting phase for readability.
REGISTRY: dict[str, CodeInfo] = dict(
    [
        # --- suppression pragmas (the `# barndsl: accept CODE` escape hatch) --
        _c("ACCEPT_DENIED", W, "Diagnostic can't be accepted",
           "An `# barndsl: accept <CODE>` pragma named a code that can't be waived: "
           "either it fired as an ERROR (an unbuildable-plan problem), or it is a "
           "structural design-flaw code on the accept-denylist (e.g. "
           "GARAGE_PASSTHROUGH — a broken *building*, not a jurisdiction judgement "
           "call). Both must be fixed, never waived. `accept` only downgrades "
           "ordinary warnings and infos. Resolve the underlying problem.",
           hint="Fix the underlying diagnostic and remove the pragma; `accept` only downgrades "
           "ordinary warnings and infos, never errors or structural design flaws such as "
           "GARAGE_PASSTHROUGH."),
        _c("ACCEPT_UNKNOWN", W, "Accept pragma names an unknown code",
           "An `# barndsl: accept <CODE>` pragma named a code the registry doesn't "
           "know (a typo, or an old name). The pragma suppresses nothing. Use a "
           "real diagnostic code — the message lists did-you-mean candidates, and "
           "`barndsl explain` / the registry has the exact spellings.",
           hint="Correct the pragma to a real diagnostic code (the message suggests near "
           "matches; `barndsl explain` lists them all), or remove it."),
        _c("ACCEPT_UNUSED", I, "Accept pragma matched nothing",
           "An `# barndsl: accept <CODE>` pragma fired on nothing — the code never "
           "appeared on the line it targets (a trailing pragma's own line, or the "
           "statement following a standalone one), or the standalone pragma had no "
           "following statement. A stale pragma outlives the diagnostic it once "
           "waived; remove it so the audit trail stays honest.",
           hint="Remove the stale pragma, or move it onto (or directly before) the statement "
           "whose diagnostic it is meant to accept."),
        # --- lexer / parser (always errors) ---------------------------------
        _c("UNTERMINATED_STRING", E, "Unterminated string literal",
           "A quoted value has no closing '\"' before the end of the line.",
           hint="Close the quoted value on the same line, e.g. `plan \"Name\"`; write "
           "feet-and-inches as `12-6` or `12'6`, because `\"` starts a string."),
        _c("SYNTAX", E, "Syntax error",
           "A required keyword or token is missing or out of place. The caret "
           "points at where the parser expected something else.",
           hint="Match the statement's grammar in the DSL reference; the caret marks where a "
           "keyword or value was expected, e.g. `setback front 25 side 10 rear 20` or "
           "`program 3 bed`."),
        _c("BAD_NUMBER", E, "Malformed number",
           "A field that expects a plain number in feet got a non-number, a "
           "quoted value, or a non-finite value (nan/inf). Write measurements "
           "as bare feet, e.g. `10.5`.",
           hint="Write each measurement as an unquoted, finite number of feet, e.g. `12`, "
           "`10.5` or `12-6`; `param` defaults must be numbers and `frame` values "
           "positive."),
        _c("BAD_LEVEL", E, "Invalid floor level",
           "`level`/`from`/`to` must be a whole number >= 0 (0 = ground).",
           hint="Use a whole number of 0 or more for `level`, `from` and `to` (0 = ground); a "
           "stair joins two different levels, e.g. `from 0 to 1`."),
        _c("BAD_COUNT", E, "Invalid count",
           "A `program` bed/bath count must be a whole number >= 0.",
           hint="Write each `program` bed/bath count as a whole number of 0 or more, e.g. "
           "`program 3 bed 2 bath`."),
        _c("EMPTY_ID", E, "Empty identifier",
           "An id or name slot received an empty string. Give it a non-empty name.",
           hint="Replace the empty `\"\"` with a non-empty id or name, e.g. "
           "`plan \"Cedar Ridge\"`."),
        _c("BAD_TYPE", E, "Unknown room type",
           "The room type isn't one of the known kinds (living, kitchen, "
           "bedroom, bathroom, …).",
           hint="Use a known room type (living, kitchen, bedroom, bathroom, … as listed in the "
           "DSL reference); in a `program`, `bed`, `bath`, `area` and `storage` are also "
           "accepted."),
        _c("BAD_WALL", E, "Unknown wall",
           "A wall must be one of north / south / east / west.",
           hint="Name the wall in full (north, south, east or west) on a `window`, `entry`, "
           "exterior `door` or `street`, e.g. `window <room> north width 4`; a `fixture`, "
           "`outlet` or `switch` wall and a `service` side also take N, S, E or W."),
        _c("EXTRA_TOKENS", E, "Unexpected trailing tokens",
           "The statement parsed fully but extra tokens remain on the line.",
           hint="Remove the extra token(s) at the end of the line; put `align`/`offset` on the "
           "relative anchor before `size`, and write lengths as one token (`12-6`, not "
           "`12 feet 6 inches`)."),
        _c("BAD_PLACEMENT", E, "Invalid placement",
           "A room needs `at <x>,<y>` or a relative anchor (east-of/west-of/"
           "north-of/south-of). At most one anchor per axis.",
           hint="Place the room with `at <x>,<y>` or a relative anchor such as "
           "`east-of <room>`, using at most one east/west and one north/south anchor; "
           "`align` takes near, far or center."),
        _c("PLACE_REF", E, "Bad placement reference",
           "A relative anchor points at a room that isn't defined yet, or a room "
           "is placed relative to itself. Define the reference room first.",
           hint="Define the reference room on an earlier line than any room anchored to it "
           "(e.g. `east-of <room>`), and never anchor a room to itself."),
        _c("BAD_OPTION", E, "Unknown option",
           "An option keyword isn't valid for this statement (e.g. an unknown "
           "`entry`/`window` option).",
           hint="Use an option or value the statement accepts, as listed in the DSL reference; "
           "the message names the valid choices, e.g. `alarm smoke|co|smoke_co` or "
           "`climate` 1-8."),
        _c("UNKNOWN_STMT", E, "Unknown statement",
           "The line doesn't start with a known statement keyword.",
           hint="Start each line with a statement keyword such as `room`, `door`, `window` or "
           "`envelope` (or `#` for a comment), and check its spelling."),
        # --- cross-file composition (the `use` statement + part files) ------
        _c("USE_UNRESOLVED", E, "Part path can't be resolved",
           "A `use \"<relpath>\"` names a part file that can't be resolved: the "
           "path is missing, absolute, escapes the including file's directory "
           "(`..`/symlink), exceeds the size limit, or the source has no home "
           "directory to resolve against (a pasted or browser-opened buffer). "
           "Paths are always relative to the including file — compile the file, "
           "or serve its folder, and keep parts under it.",
           hint="Write the path relative to the including file and keep the part under that "
           "folder, e.g. `use \"parts/<part>.barn\" as <alias> at <x>,<y>`; compile from "
           "disk, not a pasted buffer."),
        _c("USE_ALIAS_DUP", E, "Duplicate use alias",
           "Two `use` statements share an `as <alias>`. Every id inside a part is "
           "stamped `<alias>.<id>`, so aliases must be unique across the plan — "
           "give each instance its own (`m`, `m2`, `bath_1`, ...).",
           hint="Give each `use` its own unique `as <alias>`, e.g. `m`, `m2`, `bath_1`."),
        _c("USE_NESTED", E, "Use nested deeper than 2",
           "A part used by a part tries to `use` a third part. Composition is "
           "depth-2 (host → part → part); a `use` reaching depth 3 is refused. "
           "Flatten the deepest level, or `use` it one level up.",
           hint="Keep composition at depth 2 (host, part, part): flatten the deepest part into "
           "its parent, or `use` it one level up."),
        _c("USE_CYCLE", E, "Part cycle",
           "A part `use`s itself, or two parts `use` each other (a → b → a). A part "
           "library is a tree, not a ring — break the loop. The cycle path is named "
           "in the message; the loader stops cleanly instead of recursing forever.",
           hint="Break the loop so no part `use`s itself or an ancestor; a part library is a "
           "tree, not a ring."),
        _c("USE_PART_INVALID", E, "Part fails to compile",
           "A used part file doesn't compile cleanly on its own (in fragment mode): "
           "it has one or more errors of its own. The part-internal diagnostics are "
           "reported once, anchored to the part file — fix the part, then re-use it.",
           hint="Fix the errors reported against the part file until it compiles on its own, "
           "then re-use it."),
        _c("PART_HOST_STMT", E, "Host-only statement in a part",
           "A part file uses a statement that describes a whole building, not a "
           "reusable block — `envelope`, `plan`, `wing`, `ceiling`, `program`, "
           "`require`, `site`, `setback`, `building`, `street`, `orientation`, "
           "`roof`, `overhang`, `finish`, `frame`, or `electrical`. A part borrows "
           "the host's; size it by its rooms and drop the statement. (`use` and "
           "`stair` ARE allowed in a part — Phase 20 nested + multi-level parts.)",
           hint="Remove the whole-building statement from the part and size the part by its "
           "rooms; the host plan supplies the envelope, roof, program and similar "
           "settings."),
        _c("PARAM_UNKNOWN", E, "Unknown param name",
           "A bare name stands where a number is expected inside a part, but it "
           "isn't a declared `param`. Declare it (`param <name> = <number>`), or use "
           "a number. Params are numbers only in v1 — no arithmetic.",
           hint="Declare the name in the part with `param <name> = <number>`, or write a "
           "number; params are plain numbers, with no arithmetic."),
        _c("PARAM_UNDECLARED", E, "Use sets an undeclared param",
           "A `use ... with <name>=<value>` names a param the part doesn't declare. "
           "Add `param <name> = <default>` to the part, or drop the pair — the "
           "part's declared params are listed in the message.",
           hint="Add `param <name> = <number>` to the part, or drop the pair from the "
           "`use ... with` clause; the message lists the part's declared params."),
        _c("PARAM_DUP", E, "Param declared or set twice",
           "A `param <name>` is declared more than once in a part, or a `with` "
           "clause sets the same param twice. Declare/set each param once.",
           hint="Declare each `param` once per part, and set each param name only once in a "
           "`use ... with` clause."),
        _c("PARAM_IN_PLAN", E, "param in a whole plan",
           "`param` declares a *part* parameter — a whole plan (a `.barn` with a "
           "`plan` header) has no use-site to pass values from. Move `param` into a "
           "part file; the host passes values with `use ... with name=value`.",
           hint="Move `param` into a part file (a `.barn` with no `plan` header); the host "
           "passes values with `use ... with <name>=<value>`."),
        _c("PART_ORIGIN", I, "Part origin normalized",
           "A part's south-west-most corner wasn't at 0,0, so the loader shifted "
           "the whole part to the origin before stamping (the `at` on the `use` "
           "line then places that corner). Harmless — parts are authored in their "
           "own local feet and needn't start at 0,0.",
           hint="Nothing to fix: parts use their own local feet and `use ... at` places the "
           "south-west corner. Start the part at 0,0 to silence it."),
        _c("PART_EMPTY", E, "Part declares no rooms",
           "A part file (any `.barn` file with no `plan` header) must declare at "
           "least one `room`. An empty part composes nothing.",
           hint="Add at least one `room` to the part, e.g. "
           "`room <id>: <type> at 0,0 size <W> x <L>`."),
        # --- envelope / wings / top level -----------------------------------
        _c("ENVELOPE", E, "Bad envelope",
           "The envelope must have positive width and length, e.g. `envelope 60 x 40`.",
           hint="Declare the footprint with a positive width and length, e.g. "
           "`envelope 60 x 40`."),
        _c("DIM_IMPLAUSIBLE", E, "Implausible dimension",
           "An envelope, room, or wing side is non-finite or larger than the "
           "1000 ft plausibility limit — no barndominium runs that far, and such a "
           "value overflows the area takeoff to `inf`. Almost always a typo (a "
           "stray digit, or feet entered as inches × something). Use a realistic "
           "measurement in feet; the value is clamped so the rest of the report "
           "still reads, but the plan stays unbuildable until it is fixed.",
           hint="Use a realistic, finite size in feet for the envelope, wing or room (each "
           "side at most 1000 ft); a stray digit is the usual cause."),
        _c("WING_SIZE", E, "Bad wing size",
           "A `wing` block must have positive dimensions.",
           hint="Give the wing positive dimensions in feet, e.g. `wing 20 x 24 at <x>,<y>`."),
        _c("FOOTPRINT_SPLIT", E, "Disconnected footprint",
           "A wing doesn't share a wall with the rest of the building; a corner "
           "touch isn't enough. The footprint must be one connected shape.",
           hint="Move the `wing` so it shares an edge with the envelope or another wing; a "
           "corner touch doesn't connect them."),
        _c("CEILING", E, "Ceiling too low",
           "Habitable space needs a ceiling of at least 7 ft (9–12 is typical).",
           hint="Set the plan `ceiling` to at least the profile's minimum (7 ft under the IRC; "
           "9-12 is typical); for a room override, raise its `ceiling` or drop it to "
           "inherit the plan's."),
        _c("EMPTY", E, "Empty plan",
           "The plan has no rooms.",
           hint="Add at least one `room` statement, e.g. "
           "`room <id>: living at 0,0 size 20 x 16`."),
        _c("DUP_ID", E, "Duplicate room id",
           "Two rooms share an id; ids must be unique.",
           hint="Rename one of the rooms so every room id is unique, and update any statements "
           "meant to refer to the renamed room.",
           part_local=True),
        _c("NO_BATH", W, "No bathroom",
           "The plan has no bathroom or half-bath.",
           hint="Add a bathroom or half-bath, e.g. "
           "`room <id>: bathroom at <x>,<y> size 8 x 8`."),
        _c("NOTE_OUTSIDE", I, "Positioned note outside the footprint",
           "A `note \"...\" at <x>,<y>` is anchored outside the building footprint "
           "(envelope + wings). Often intentional — annotating the site, a setback, "
           "or a future addition — so it's only a gentle nudge, never an error: if "
           "the callout means to sit on the plan, move its `at` point inside the "
           "walls.",
           hint="If the callout belongs on the plan, move its `at` point inside the walls; a "
           "note that annotates the site on purpose can stay as it is."),
        _c("FLOOR_FINISH", W, "Unrecognised floor finish",
           "A room's `floor \"...\"` hint matched no material in the 3D palette, "
           "so it falls back to the default finish for its room type. Not "
           "blocking — pick a recognised name (tile, concrete, wood/plank, "
           "carpet, ...) to control the 3D floor material.",
           hint="Use a recognised name in `floor \"<finish>\"` (tile, concrete, wood plank, "
           "carpet, ...), or drop it to get the room type's default."),
        # --- site / setbacks (the `site` / `setback` statements) -------------
        _c("SETBACK", E, "Footprint violates the setbacks",
           "The building footprint (envelope + wings + porches) doesn't fit "
           "inside the buildable rectangle — the lot (`site`) minus its yard "
           "setbacks. `front` and `rear` consume the plan's north-south depth "
           "(front along the south/entry edge); `side` clears both the east and "
           "west edges. Without `building at <x>,<y>` it is a dimensions-only "
           "check: the footprint's bounding box against the buildable width and "
           "length. With it, each yard is measured against its own setback. "
           "Shrink the footprint, enlarge the lot, reduce the setbacks, or move "
           "the building. Not a substitute for a survey/site plan.",
           hint="Shrink the footprint, enlarge the `site`, or reduce the `setback` so building "
           "plus porches fit the buildable area; with `building at <x>,<y>`, also try "
           "moving the building."),
        _c("SITE", E, "Invalid site declaration",
           "The `site` lot dimensions are zero/negative/non-finite, or a "
           "`setback` value is negative. Declare a real lot: positive "
           "dimensions, non-negative setbacks.",
           hint="Declare `site <W> x <L>` with positive, finite lot dimensions, and give "
           "`setback` non-negative values, e.g. `setback front 25 side 10 rear 20`."),
        _c("SETBACK_NO_SITE", E, "Setback without a site",
           "A `setback` statement declares yard setbacks but no `site <W> x <L>` "
           "gives the lot dimensions to measure them against. Add a `site` line, "
           "or drop the setbacks.",
           hint="Add the lot dimensions with `site <W> x <L>` (feet), or remove the `setback` "
           "line."),
        _c("SITE_REQUIRED", E, "Site feature without a site",
           "A `drive`/`walk`/`well`/`septic`/`service` places itself in lot feet, "
           "so it needs a `site <W> x <L>` to sit on. Declare the lot dimensions, "
           "or remove the site feature. (`grade` is the exception — it describes "
           "the building's height above grade and needs no lot.)",
           hint="Add the lot dimensions with `site <W> x <L>` (feet), or remove the `drive`, "
           "`walk`, `well`, `septic` or `service` line."),
        _c("SITE_REF", E, "Walk names an unknown room",
           "A `walk from <room> to drive` names a room that doesn't exist. The walk "
           "starts at that room's exterior door, so it must be a real room with an "
           "exterior door. Name an existing entry room.",
           hint="Name an existing room that has an exterior door, e.g. "
           "`walk from <room> to drive`."),
        _c("WELL_SEPTIC_CLEAR", W, "Well too close to the septic",
           "A private well sits closer to the septic tank/drain field than the "
           "common 100 ft health-department separation. This is a public-health "
           "rule (not IRC) and varies by jurisdiction (50-100 ft is typical). Move "
           "the well or septic apart, and confirm the figure with the county health "
           "department.",
           hint="Move the `well` or `septic` apart to the required separation (commonly 100 "
           "ft), and confirm the figure with the county health department."),
        _c("DRIVE_DOOR", I, "Drive with no path to a door",
           "The plan has a driveway but neither a `walk` nor a drive edge comes "
           "within a few feet of any exterior door — guests park and have no path "
           "to an entry. Add `walk from <room> to drive`, or extend the drive to a "
           "door.",
           hint="Add `walk from <room> to drive` from a room with an exterior door, or extend "
           "the drive to within a few feet of a door."),
        _c("SEPTIC_SETBACK", I, "Septic inside a setback",
           "A septic tank or its drain field falls inside a required yard setback "
           "band. Septic components are usually held out of the setbacks too; "
           "confirm the allowed septic setback with the county health department.",
           hint="Move the `septic` and its drain field clear of the setback bands, or confirm "
           "the allowed septic setback with the county health department."),
        _c("SITE_OVERLAP", W, "Site features of the same kind overlap",
           "Two declared driveways overlap on the lot. The cost takeoff sums each "
           "drive's area independently, so an overlap double-counts the shared "
           "paving in the estimate (and the drawing paints it twice). Only drives "
           "are checked: a `walk` is auto-routed to terminate at a drive, so a "
           "walk-drive overlap is by design and a walk-walk overlap of two thin "
           "auto-routed paths isn't a meaningful double-count. Merge or separate "
           "the overlapping drives so each patch of paving is declared once.",
           hint="Merge the overlapping `drive` rectangles into one, or move them apart so each "
           "patch of paving is declared once."),
        _c("PORCH_GUARD", W, "Porch needs a guard (R312.1)",
           "The declared `grade` puts the finish floor more than 30 in above "
           "finished grade, so every porch is a walking surface that needs a 36 in "
           "guard (IRC R312.1) with balusters blocking a 4 in sphere. Note the "
           "guard on the drawings. Silent when no `grade` is declared or the floor "
           "sits <= 30 in above grade.",
           hint="Add a 36 in guard along the porch's open edges, with balusters blocking a 4 "
           "in sphere, and note it on the drawings (IRC R312.1)."),
        _c("RECOVERY_LIMIT", W, "Partial-plan checks incomplete",
           "Parse-error recovery kept a partial plan, but frame placement or "
           "validation crashed on it and was skipped - the diagnostics listed "
           "are incomplete. Fix the parse error(s) to get the full report.",
           hint="Fix the parse errors reported with it; once every statement parses, frame "
           "placement and validation run on the full plan and the report is complete."),
        # --- geometry -------------------------------------------------------
        _c("ROOM_GEOMETRY", E, "Non-finite room geometry",
           "A room has nan/inf coordinates or size.",
           hint="Give the room finite coordinates and size in feet, e.g. "
           "`at 0,0 size 12 x 10`; nan and inf aren't valid measurements.",
           part_local=True),
        _c("ROOM_SIZE", E, "Non-positive room size",
           "A room's width or length is <= 0.",
           hint="Give the room a positive width and length in feet, e.g. `size 12 x 10`.",
           part_local=True),
        _c("OUT_OF_BOUNDS", E, "Room outside the footprint",
           "A room extends past the envelope (or, with wings, outside the "
           "footprint union).",
           hint="Move or shrink the room so it sits inside the `envelope` or a wing, or add a "
           "`wing` that covers the area it extends into."),
        _c("OVERLAP", E, "Rooms overlap",
           "Two rooms on the same level intersect. Reposition so they only abut.",
           hint="Move or resize one room so the two only abut, e.g. re-anchor it with "
           "`east-of <room>` or pin it with `at <x>,<y>`; otherwise shrink a room or "
           "enlarge the envelope.",
           part_local=True),
        _c("AREA_OVERFLOW", W, "Assigned area exceeds footprint",
           "The level-0 room area sums to more than the footprint — rooms likely "
           "overlap or the envelope is too small.",
           hint="Check for overlapping rooms, then shrink rooms or enlarge the `envelope` "
           "until the ground-floor room area fits within the footprint."),
        _c("AREA_UNUSED", I, "Footprint under-used",
           "A large share of the footprint isn't assigned to any room.",
           hint="Enlarge rooms or add spaces so more of the footprint is assigned to a room."),
        _c("AREA_VOID", E, "Concentrated unassigned void",
           "One connected, room-sized patch of the footprint is assigned to no "
           "room — a real hole in the plan (an unfinished space, a mis-sized "
           "neighbour, a gap the tiling left). A house has no void areas: an "
           "enclosed pocket no one can enter is framed, roofed and "
           "foundation-poured dead space, so a room-sized void is an ERROR (a "
           "smaller pocket, under the error bar but over ~20 sq ft, is an INFO "
           "nudge). Unlike AREA_UNUSED — which sums "
           "diffuse slack and only speaks below 85% coverage — this fires on the "
           "largest single gap regardless of overall coverage, so a dead pocket on "
           "an otherwise well-covered footprint stays visible. Fill it with a room, "
           "grow a neighbour over it, or trim the envelope.",
           hint="Extend an adjacent room over the empty patch, declare a room there (storage, "
           "closet, pantry, utility), or shrink the `envelope` so no dead pocket remains.",
           also=(I,)),
        # --- room programs --------------------------------------------------
        _c("BEDROOM_AREA", E, "Bedroom too small",
           "A bedroom is below the ~70 sq ft IRC minimum habitable area (R304).",
           hint="Enlarge the bedroom's `size` to at least the profile's minimum habitable area "
           "(about 70 sq ft under IRC R304).",
           part_local=True),
        _c("BEDROOM_DIM", E, "Bedroom too narrow",
           "A bedroom's smallest dimension is below the 7 ft minimum (R304).",
           hint="Widen the bedroom so both dimensions meet the profile's minimum (7 ft under "
           "IRC R304).",
           part_local=True),
        _c("ROOM_HABITABLE", W, "Habitable room below the R304 minimum",
           "A habitable room (living, dining, office/den, loft) is below the IRC "
           "R304 minimum — 70 sq ft of floor area (R304.1) and 7 ft in every "
           "horizontal dimension (R304.2). Bedrooms carry the same rule as a hard "
           "error via BEDROOM_AREA/BEDROOM_DIM (not repeated here); a kitchen is "
           "exempt from both (R304.2). The thresholds follow the active profile's "
           "habitable-room minimums.",
           hint="Enlarge the room to the active profile's R304 minimums (by default 70 sq ft "
           "of floor area and 7 ft in every horizontal dimension).",
           part_local=True),
        _c("HALL_WIDTH", E, "Hallway too narrow",
           "A hallway is below the 3 ft (36 in) minimum width (R311.6).",
           hint="Widen the hallway to at least the profile's minimum hallway width (3 ft by "
           "default, IRC R311.6)."),
        _c("ROOM_TIGHT", I, "Room below a workable size",
           "A room is smaller than the usable floor its function needs — by area "
           "(kitchen ~70, full bath ~48, half bath ~30 sq ft) or by shortest side "
           "(full bath >= 6 ft, half bath >= 5 ft, so the fixtures fit across it). "
           "Bedrooms are covered by BEDROOM_AREA.",
           hint="Enlarge the room to a workable size (kitchen ~70, full bath ~48, half bath "
           "~30 sq ft) and widen a bath's short side to 6 ft (half bath 5 ft).",
           part_local=True),
        _c("BATH_CLEARANCE", W, "Bathroom can't fit its fixtures",
           "A bathroom's clear (finish-face) interior can't hold its fixtures with "
           "code clearances — a water closet needs 15 in from its centreline to any "
           "wall/fixture and 21 in of clear floor in front (IRC R307.1), and a full "
           "bath needs a 5 ft wall for the tub. Enlarge the room so toilet, lavatory "
           "and tub/shower fit.",
           hint="Enlarge the bathroom so the toilet (15 in each side of centre, 21 in clear in "
           "front), lavatory and, in a full bath, a 5 ft tub wall fit.",
           part_local=True),
        _c("KITCHEN_FIT", I, "Kitchen tight for its appliances",
           "A kitchen's clear interior is too small to hold a sink, range and "
           "refrigerator along the counters with a comfortable ~40 in working aisle. "
           "Enlarge it or lengthen the counter run.",
           hint="Enlarge the kitchen or lengthen its counter run so a sink, range and "
           "refrigerator fit with a ~40 in working aisle.",
           part_local=True),
        _c("BED_CLEARANCE", I, "Bedroom too tight to furnish",
           "A bedroom clears its area and 7 ft nominal dimension but its clear "
           "(finish-face) shape still can't hold a queen bed (5×6.67) against a wall "
           "with a ~24 in walk-around — needs about 7×6.67 ft clear. Catches the "
           "narrow room that passes the area/dimension checks but not the layout; "
           "the livability companion to the wet-room fixture checks.",
           hint="Widen or reshape the bedroom to about 7 × 6.67 ft clear so a queen bed backs "
           "to a wall with a ~24 in path beside it.",
           part_local=True),
        _c("DINING_CLEARANCE", I, "Dining room too tight to furnish",
           "A dining room's clear interior is too small to seat a 4-person table "
           "(~3 ft) with ~30 in of chair-pull and circulation all round (about 8 ft "
           "clear each way). Enlarge it.",
           hint="Enlarge the dining room to about 8 ft clear each way so a 4-seat table has "
           "~30 in of chair-pull and circulation all round.",
           part_local=True),
        _c("OFFICE_CLEARANCE", I, "Office too tight for a desk",
           "An office's clear (finish-face) interior, minus the door-swing "
           "keepouts, can't hold a desk (4×2 ft) against a wall with a 3 ft "
           "chair-pull behind it to roll the chair back — it needs about a 4×5 ft "
           "clear box on some wall, clear of the door's arc. The office analog of "
           "BED_CLEARANCE / DINING_CLEARANCE (clear box + keepouts). A ~8×10 ft "
           "office is the comfortable floor. Enlarge or reshape it, or move the "
           "door off the desk wall. INFO — livability guidance, not a gate.",
           hint="Enlarge or reshape the office (~8 x 10 ft is comfortable), or move its door "
           "off the desk wall, so a desk fits against a wall with 3 ft of chair-pull."),
        _c("ACCESS_ENTRY", I, "No-step entrance (accessible target)",
           "An accessible plan needs at least one no-step entrance (threshold ≤ ½ in) "
           "with a level landing (ANSI A117.1). Thresholds aren't in the geometry, so "
           "this is a reminder. Only emitted when the plan opts in via `accessible`.",
           hint="Make at least one entrance no-step (threshold ≤ ½ in) with a level landing, "
           "e.g. a slab-on-grade entry with no stoop step."),
        _c("ACCESS_DOOR", I, "Door too narrow for an accessible route",
           "A door/opening on the living route is below the ~32 in clear width an "
           "accessible route needs (a ~34 in leaf; a 36 in exterior door) — ANSI "
           "A117.1 §404. Only emitted when the plan opts in via `accessible`.",
           hint="Widen doors on the accessible route to a ~34 in leaf (32 in clear), and use a "
           "36 in exterior door, `width 3`, at the accessible entrance."),
        _c("ACCESS_BATH", I, "Bath lacks a wheelchair turning space",
           "A ground-floor bath's clear short side is under the 60 in wheelchair "
           "turning circle (ANSI A117.1 §304); plan a roll-in shower and grab-bar "
           "blocking too. Only emitted when the plan opts in via `accessible`.",
           hint="Widen the ground-floor bath so its clear short side is at least 60 in for a "
           "wheelchair turning circle, and plan a roll-in shower and grab-bar blocking."),
        _c("ACCESS_SINGLE_FLOOR", I, "No single-floor living",
           "Accessible / aging-in-place living wants a bedroom and a full bath on the "
           "one no-stair entry level; the entry level is missing one. Only emitted "
           "when the plan opts in via `accessible`.",
           hint="Place a bedroom and a full bath on the no-stair entry level so daily living "
           "works on one floor."),
        _c("ROOM_CLEAR", I, "Clear dimension falls short once walls are built",
           "A room meets a code minimum on its nominal (centreline) rectangle but "
           "falls below it once the bounding walls' thickness is subtracted. IRC "
           "habitability minimums (R304 area/width, R311.6 hall width) are measured "
           "between finished surfaces, and Revit's room schedule reports that same "
           "clear area — so a plan can compile clean yet build short. Grow the room "
           "by roughly a wall thickness so the clear dimension still meets the "
           "minimum.",
           hint="Grow the room by roughly a wall thickness on the short side (or in area) so "
           "its clear, finish-face size still meets the minimum.",
           part_local=True),
        # --- fixtures & furnishings (the `fixture` statement) ----------------
        _c("DEVICE_ROOM", E, "Device references unknown room",
           "An `outlet`, `switch`, `light` or `alarm` statement places a device "
           "`in` a room id that doesn't exist — a mistyped id would otherwise draw "
           "nothing and silently skip the receptacle, lighting and alarm checks for "
           "the room you meant. Reference a room that's defined.",
           hint="Place the `outlet`, `switch`, `light` or `alarm` `in` a room id that is "
           "defined, correcting any typo in the id.",
           part_local=True),
        _c("FIXTURE_ROOM", E, "Fixture references unknown room",
           "A `fixture` statement places a fixture `in` a room id that doesn't "
           "exist — a mistyped id would otherwise place nothing. Reference a room "
           "that's defined.",
           hint="Place the fixture `in` a room id defined by a `room` statement; check the id "
           "for a typo.",
           part_local=True),
        _c("FIXTURE_OOB", W, "Fixture outside the room",
           "An author-placed fixture's footprint extends past its room's clear "
           "(finish-face) interior. Its `at` is room-local feet from the SW corner; "
           "move it inward or grow the room. Auto-seeded fixtures are fitted "
           "automatically, so only explicit `fixture` placements are checked.",
           hint="Move the fixture inward (its `at` is room-local feet from the room's SW "
           "corner), or grow the room.",
           part_local=True),
        _c("FIXTURE_OVERLAP", W, "Fixtures overlap",
           "Two fixtures occupy the same floor — an author-placed one overlaps "
           "another fixture (a seed or another placement). Slide one along its "
           "wall, or back it to a different wall.",
           hint="Slide one fixture along its wall, or back it to a different wall with "
           "`wall N|S|E|W`.",
           part_local=True),
        _c("FIXTURE_DOOR", W, "Fixture blocks a door swing",
           "An author-placed fixture sits in the clear floor a hinged door swings "
           "through, so the door can't fully open — its leaf hits the fixture. "
           "The auto-placer keeps its own seeds and auto-slotted pieces clear of "
           "every swing, so only an explicit `at x,y` can land here. Keep the "
           "swing clear — slide the fixture off the door approach, or swing the "
           "door the other way (or make it a pocket/sliding door).",
           hint="Slide the fixture clear of the door's swing, swing the door the other way "
           "with `into <room>`, or make it a `pocket` or `sliding` door.",
           part_local=True),
        _c("FIXTURE_OPENING", W, "Fixture blocks a doorway/opening",
           "A wall-backed fixture or appliance sits across a door, cased opening, "
           "entry or overhead door. Appliances need wall backing and utility "
           "hookups, and openings must stay clear for traffic.",
           hint="Slide the fixture along a solid wall with backing and services, or move the "
           "door or opening so it stays clear."),
        _c("FIXTURE_TOILET_CLEARANCE", W, "Toilet clearance below IRC R307.1",
           "An author-placed toilet has less than 15 in from its centreline to the "
           "nearest side wall or fixture, or less than 21 in of clear floor in "
           "front (IRC R307.1). Give the water closet a 30 in bay (15 in each side "
           "of centre) and 21 in in front — slide it along the wall or widen the "
           "room. Only explicit `fixture toilet` placements are checked; the "
           "auto-seed is fitted for you.",
           hint="Give the toilet a 30 in bay (15 in each side of centre) and 21 in of clear "
           "floor in front; slide it along the wall or widen the room.",
           part_local=True),
        _c("FIXTURE_FRONT", I, "Fixture's clear-floor strip is blocked",
           "An author-placed fixture's approach — the clear floor it needs in front "
           "of its face (the `front` clearance in the catalog) — is cut off by a "
           "wall or overlapped by another fixture, so you can't use or stand at it. "
           "A free-standing piece (table, island) instead wants ~2 ft of walkway on "
           "at least one long side. Slide it clear, or move it toward the centre.",
           hint="Slide the fixture to where nothing crowds the clear floor in front of it; "
           "give a free-standing table or island ~2 ft of walkway on one long side.",
           part_local=True),
        _c("FIXTURE_ROOM_TYPE", I, "Fixture in an unusual room type",
           "An author-placed fixture sits in a room type it isn't usually found in "
           "— a toilet/tub/shower/lavatory outside a bath or laundry, a "
           "range/refrigerator/island outside a kitchen, a bed outside a bedroom or "
           "loft. Often a typo in the room id or type; if it's deliberate, ignore "
           "the note. Storage/utility placements (a water heater in a utility, a "
           "washer in a mudroom, a desk anywhere) are never flagged.",
           hint="Check the room id and type for a typo; if the placement is deliberate, keep "
           "it and record why with `# barndsl: accept FIXTURE_ROOM_TYPE \"reason\"`.",
           part_local=True),
        _c("FIXTURE_BACKING", I, "Wall-backed fixture floats mid-floor",
           "An author-placed piece that normally backs to a wall (toilet, vanity, "
           "tub, shower, sink, range, fridge, counter, wardrobe, dresser, water "
           "heater, washer, dryer) sits more than ~0.5 ft off every wall of its "
           "room. Back it to a wall with `at`, or drop the coordinates and give it "
           "`wall N|S|E|W` to auto-place against a wall.",
           hint="Back the fixture to a wall: adjust its `at` so it sits against one, or drop "
           "`at` and use `wall N|S|E|W` to auto-place it.",
           part_local=True),
        _c("FIXTURE_EGRESS", W, "Fixture blocks a bedroom escape window",
           "A tall author-placed piece (refrigerator, wardrobe, water heater) parks "
           "over a bedroom's emergency-escape window, so no one could get out "
           "through it (IRC R310). Keep the egress window clear — move the fixture "
           "to another wall.",
           hint="Move the refrigerator, wardrobe or water heater to another wall so the "
           "bedroom's escape window stays clear.",
           part_local=True),
        _c("COUNTER_DOOR", W, "Counter run crosses a doorway",
           "A `fixture counter ... along <wall>` run spans a doorway, opening or "
           "entry on that wall — you can't build countertop across an opening. Stop "
           "the run short of the opening with `from <a> to <b>` (room-local feet "
           "along the wall), or move the run to another wall. A full-wall run over a "
           "door is the usual cause; a broken L/U leg clears it.",
           hint="Stop the run short of the opening with "
           "`fixture counter in <room> along <wall> from <a> to <b>`, or move the run to "
           "another wall."),
        _c("COUNTER_ROOM", I, "Counter in an unusual room type",
           "A counter run sits in a room type where a run of casework reads as odd "
           "(a bedroom, closet, hallway or loft). Counters belong to a kitchen, "
           "pantry, bath, laundry, mudroom or shop; if this is a deliberate bar or "
           "work ledge, ignore the note.",
           hint="Move the counter to a kitchen, pantry, bath, laundry, mudroom or shop; for a "
           "deliberate bar or work ledge, keep it and record why with "
           "`# barndsl: accept COUNTER_ROOM \"reason\"`."),
        _c("SINK_NO_COUNTER", I, "Kitchen sink not set into a counter",
           "A kitchen sink isn't set into any counter run — a sink wants countertop "
           "to each side to work at. Extend a `fixture counter ... along <wall>` run "
           "past the sink so it drops into the countertop. Only fires once the "
           "kitchen has counters placed to compare against; a bath lavatory (its own "
           "vanity) is never judged.",
           hint="Extend a `fixture counter in <room> along <wall>` run past the sink so it "
           "drops into the countertop with counter to each side."),
        _c("RANGE_WINDOW", W, "Range under an operable window",
           "A range/cooktop sits directly under a window that opens. A draft "
           "through the sash can blow out a burner, and a curtain hangs over the "
           "flame — common code and NKBA practice keep a cooktop out from under an "
           "openable window. Slide the range along the wall, clear of the sash "
           "(a fixed, non-opening window overhead is fine).",
           hint="Slide the range along the wall so it clears the operable window's sash; a "
           "`fixed` window over the cooktop is fine."),
        _c("RANGE_LANDING", I, "No landing surface beside the range",
           "A range has no counter, sink, island or refrigerator within 1 ft to "
           "either side to set a hot pan down (NKBA wants a landing surface flanking "
           "the cooktop). Add a `fixture counter` next to the range. Only fires when "
           "the kitchen already has other casework placed to compare against.",
           hint="Put a `fixture counter` beside the range so a hot pan can be set down within "
           "1 ft. Or keep it and record why with "
           "`# barndsl: accept RANGE_LANDING \"reason\"`.",
           part_local=True),
        _c("KITCHEN_TRIANGLE", I, "Kitchen work triangle too spread out",
           "The sink–range–refrigerator work triangle (centre to centre) sums to "
           "more than ~26 ft, so the cook walks marathons between the three "
           "stations (NKBA keeps each leg ~4–9 ft, the perimeter ~13–26 ft). Draw "
           "the three appliances closer together. A compact, efficient galley is "
           "not flagged; a genuinely cramped kitchen is caught by KITCHEN_FIT.",
           hint="Place the sink, range and refrigerator closer together, keeping each leg ~4–9 "
           "ft and the triangle under ~26 ft.",
           part_local=True),
        _c("DRYER_VENT", I, "Dryer far from an exterior wall",
           "A dryer is more than ~10 ft from any exterior wall of its room (or its "
           "room has none), so the exhaust duct runs long and bendy — lint collects "
           "and airflow drops, a fire risk and an efficiency loss. Put the laundry "
           "on an exterior wall, or keep the dryer near one.",
           hint="Put the laundry on an exterior wall, or place the dryer within about 10 ft of "
           "one so its vent duct stays short and straight.",
           part_local=True),
        _c("WATER_HEATER_PLACEMENT", I, "Water heater placement needs protection",
           "A `water_heater` fixture sits somewhere its installation needs extra "
           "protection the DSL can't draw. In a garage or shop, a fuel-fired or "
           "electric water heater's ignition source must be elevated 18 in above "
           "the floor (or be a listed flammable-vapour-ignition-resistant unit), "
           "IRC M1307.3. On an upper floor (level 1+) over habitable space, it "
           "needs a drain pan piped to an approved drain so a leak doesn't soak the "
           "ceiling below, IRC P2801.6. A one-per-heater INFO naming which case "
           "applies — carry the detail onto the plumbing/mechanical documents.",
           hint="Carry the protection onto the plumbing/mechanical documents: elevate the "
           "ignition source 18 in in a garage or shop, or add a piped drain pan on an "
           "upper floor."),
        _c("FIXTURE_STAIR", W, "Fixture on a stair footprint",
           "A fixture's footprint overlaps a stair's run or landing on the same "
           "level, so it fouls the flight. Keep the stair and its landing clear — "
           "slide the fixture off the footprint, or run the stair along a wall.",
           hint="Slide the fixture off the stair's run and landing, or move the stair along a "
           "wall."),
        # --- doors ----------------------------------------------------------
        _c("SELF_DOOR", E, "Door to self",
           "An interior door connects a room to itself.",
           hint="Name two different rooms in the door, e.g. `door <room_a> - <room_b>`; a door "
           "always joins two rooms."),
        _c("DOOR_REF", E, "Door references unknown room",
           "A door/entry names a room id that doesn't exist.",
           hint="Reference a room id that exists, correcting any typo, or declare the missing "
           "room with a `room` statement."),
        _c("DOOR_NOADJ", E, "Door between non-adjacent rooms",
           "A door joins two rooms that don't share a wall (they may only touch "
           "at a corner), or a cross-level door whose footprints don't overlap.",
           hint="Move the rooms to share a wall, not just a corner, or route through a room "
           "between them; across levels, place the upper room over part of the lower one."),
        _c("DOOR_FIT", W, "Door wider than the wall",
           "A door is wider than the shared wall it sits on.",
           hint="Reduce the door's `width` so it fits within the wall the two rooms share."),
        _c("DOOR_OOB", E, "Door runs off the shared wall",
           "A positioned interior door's offset+width exceeds the shared wall it "
           "sits on. Keep offset >= 0 and offset + width <= the shared length.",
           hint="Keep `offset` >= 0 and `offset` + `width` within the shared wall's length, or "
           "drop `offset` to centre the door."),
        _c("DOOR_SWING", W, "Door swing problem",
           "Error: a door's `into` names a room it doesn't connect. Warning: the "
           "leaf can't fully open because the room it swings into is shallower "
           "than the door is wide — swing it the other way or narrow it.",
           hint="Set `into` to one of the two rooms the door joins; if the leaf can't fully "
           "open, swing it into the other room, narrow the door, or deepen the room.",
           also=(E,)),
        _c("DOOR_BLOCKS_HALL", I, "Door swings into a hallway",
           "A door swings into a hallway and, open, its leaf leaves under a 3 ft "
           "passage beside it — it blocks circulation. Swing it into the room.",
           hint="Swing the door into the room instead, with `into <room>`, so the hallway "
           "keeps a clear passage."),
        _c("DOOR_HITS_FIXTURE", W, "Door swing crowds out a fixture",
           "A door's swing arc eats the wall run a wet room or kitchen needs to "
           "place a fixture (toilet, tub, lavatory, sink, range, refrigerator) that "
           "the room otherwise has the floor area for — keeping the leaf's path "
           "clear drops it. This is the gap BATH_CLEARANCE (capacity only, "
           "door-blind) can't see. Move the door along the wall, swing it the other "
           "way (`into <room>` / `hinge near|far`), make it a pocket/sliding door, "
           "or enlarge the room.",
           hint="Move the door along the wall, swing it the other way (`into <room>` / "
           "`hinge near|far`), make it a `pocket` or `sliding` door, or enlarge the room.",
           part_local=True),
        _c("DOOR_SWING_INWARD", W, "Exterior door can't clear inward",
           "An exterior door swings inward (the residential default), but the room "
           "is too shallow for its leaf to fully open. Deepen the room, narrow the "
           "door, or use an out-swing or sliding door.",
           hint="Deepen the room or narrow the door. If it will be built as an out-swing or "
           "sliding door, keep it and record why with "
           "`# barndsl: accept DOOR_SWING_INWARD \"reason\"`."),
        _c("DOOR_SWING_PRIVACY", I, "Private-room door swings out",
           "A bedroom or bathroom door is set to swing out into circulation. It "
           "should open into the private room so the leaf screens the view on entry "
           "and folds flat against a wall rather than sweeping the corridor.",
           hint="Swing the door into the bedroom or bathroom with `into <room>` on the `door` "
           "statement, so the leaf screens the view and folds against a wall."),
        _c("DOOR_SWING_UNSET", I, "Private-room door swing not pinned",
           "A bedroom or bathroom door has no `into` direction and its default "
           "geometric swing opens out of the room. Pin it with `into <room>` so it "
           "reliably opens into the private space (and stays that way if the layout "
           "shifts).",
           hint="Pin the swing with `into <room>` on the `door` statement, naming the bedroom "
           "or bathroom, so it opens into the private room."),
        _c("DOOR_NARROW", W, "Door too narrow",
           "A swinging interior door is below the 30 in minimum clear width.",
           hint="Give the swinging interior door a `width` of at least 2.5 ft (30 in)."),
        _c("DOOR_SIZE", I, "Non-standard door width",
           "A swing door's width isn't a manufactured leaf size (interior "
           "24/28/30/32/36 in; exterior 30/32/36, doubles 60/72), a declared "
           "double/french pair isn't a stock pair width (48/60/64/72 in total), "
           "a bifold isn't a stock opening (24/30/32/36 in singles, 48/60/72 in "
           "pairs, 96 in for two units side by side), or an overhead door isn't "
           "a stock sectional size (widths 8/9/10/12/16 ft; heights 7/8 ft "
           "residential and 10/12/14 ft commercial, the tall panels a shop bay "
           "wants for lift/RV clearance). Snap it to the nearest so it's "
           "orderable off-the-shelf.",
           hint="Snap `width` (and an overhead door's `height`) to the nearest stock size, "
           "e.g. `width 3`, or keep a custom size and record why with "
           "`# barndsl: accept DOOR_SIZE \"reason\"`."),
        _c("DOOR_WIDE_SWING", W, "Oversized single swing door",
           "A very wide interior opening was written as one swing leaf. Use an "
           "open/cased passage for open-plan public rooms, or declare a double/"
           "french pair so the opening is represented as two leaves.",
           hint="Make an open-plan passage a cased opening (`open <room_a> - <room_b>`), "
           "declare a `double` or `french` pair, or reduce the single leaf to a normal "
           "2.5–3 ft width."),
        _c("OVERHEAD_ROOM", I, "Overhead door in a living space",
           "An overhead (sectional garage) door is on a room that isn't a garage "
           "or shop — unusual for a living space. Either the room should be a "
           "garage/shop bay, or the door should be a people-door (`entry`).",
           hint="Retype the room as a `garage` or `shop`, or use `entry <room> <wall>` for a "
           "people-door. Or keep it and record why with "
           "`# barndsl: accept OVERHEAD_ROOM \"reason\"`."),
        _c("SHOP_DOOR_HEIGHT", I, "Shop overhead door too short for clearance",
           "An overhead door on a SHOP bay is 8 ft tall or shorter — a "
           "residential-height panel. The whole point of a barndominium shop is "
           "clearance: a 12 ft+ bay exists to swallow a lift, an RV, a dually with "
           "a topper, and a 7-8 ft door throttles the opening to car height and "
           "wastes that headroom. Order a taller sectional (`height 10`, or 12/14) "
           "so the opening matches the bay. A GARAGE is exempt (a car clears a 7 ft "
           "door fine) — only a SHOP door is nudged. INFO: the short door still "
           "opens; it's the bay's clearance that's squandered.",
           hint="Give the shop's overhead door `height 10` or more (12/14 for a lift or RV), "
           "or keep it and record why with `# barndsl: accept SHOP_DOOR_HEIGHT \"reason\"`."),
        _c("OVERHEAD_HEADER", I, "Wide overhead opening needs an engineered header",
           "An overhead door wider than 10 ft (a 12 or 16 ft double) spans more "
           "than a stock header carries — the header and the jamb posts over the "
           "opening must be engineered with the building frame.",
           hint="Have the header and jamb posts over the opening engineered with the building "
           "frame, or split it into two single overhead doors."),
        _c("OPEN_BATH", W, "Bathroom has no door",
           "A bathroom is joined by an `open` passage; baths need a door for privacy.",
           hint="Replace the `open` passage with a real door, `door <bath> - <room>`, so the "
           "bathroom has privacy.",
           part_local=True),
        # --- openings -------------------------------------------------------
        _c("WINDOW_REF", E, "Window references unknown room",
           "A window names a room id that doesn't exist.",
           hint="Reference an existing room id in the `window` statement (check for typos), or "
           "declare the room."),
        _c("OPENING_SIZE", E, "Non-positive opening width",
           "A window, interior door, or exterior door (including an overhead "
           "door) was declared with `width <= 0`. A zero- or negative-width "
           "opening isn't a buildable opening, and it misprices in the estimate "
           "(an overhead line vanishes; an entry still bills a full leaf). Give "
           "it a positive width, e.g. `width 3`.",
           hint="Give the opening a positive width, e.g. `width 3` for a door or window, or "
           "`width 9` for an overhead door.",
           part_local=True),
        _c("OPENING_OOB", E, "Opening runs off the wall",
           "A window/entry's offset+width exceeds the wall it sits on.",
           hint="Keep the opening on its wall: `offset` at least 0 and `offset` + `width` no "
           "more than the wall's length. Narrow it or reduce its offset."),
        _c("OPENING_CLASH", E, "Openings overlap",
           "Two openings (window/entry) overlap on the same wall span — they "
           "can't both physically occupy that run of wall.",
           hint="Move one opening along the wall or narrow it so their spans don't overlap; "
           "for two doors between the same rooms, offset them apart or use a single door.",
           part_local=True),
        _c("WINDOW_INTERIOR", W, "Window on an interior wall",
           "A window is on a wall that isn't on the building envelope, so it "
           "provides no daylight or egress.",
           hint="Move the window to a wall that lies on the building envelope, or rework the "
           "layout so the room reaches an exterior wall."),
        _c("WINDOW_SILL", W, "Window head at or below its sill",
           "A window's head height is not above its sill height, so it encloses "
           "no glazed area. Set `head` above `sill` (both are ft above the floor).",
           hint="Set `head` above `sill` (both in feet above the floor), e.g. "
           "`sill 3 head 6.5`."),
        _c("WINDOW_TEMPERED", W, "Window needs safety glazing",
           "A window sits in an IRC R308.4 hazard location — within 24 in of a "
           "door in the same wall plane (R308.4.1), within 60 in of a tub/shower "
           "in a wet room (R308.4.5), within 36 in of a stair flight "
           "(R308.4.6/.7, simplified), or a large glazing panel over 9 sq ft whose "
           "bottom edge is below 18 in and top edge above 36 in above the floor, "
           "anywhere (R308.4.3) — where human impact is likely, so its glass must "
           "be tempered/safety glazing. The rule is derived from geometry, honours "
           "a declared `tempered` attribute (the R308.4 escape hatch), and also "
           "fills the window schedule's Glazing column. Specify tempered glass on "
           "the schedule.",
           hint="Add `tempered` to the `window` line so the schedule specifies safety glazing, "
           "as IRC R308.4 requires in hazard locations."),
        _c("WINDOW_FALL", W, "Operable window needs fall protection",
           "An operable window has a sill below 24 in on an upper storey. IRC "
           "R312.2 requires window fall protection (an opening-control device or "
           "fall guard, ASTM F2090) where an operable sash sits below 24 in and "
           "more than 72 in above the grade below. The model carries no grade "
           "elevation, so an upper level (>= 1) is the proxy for 'well above "
           "grade'. Fit an opening-control device that limits the sash to a 4 in "
           "clear opening yet still releases for escape — do not raise the sill, "
           "which would fight the R310 egress-window rule. A fixed sash is exempt.",
           hint="Fit a window opening-control device or fall guard (ASTM F2090) that limits "
           "the sash to a 4 in opening; don't raise the sill, which fights the egress "
           "rule."),
        _c("ENTRY_INTERIOR", E, "Entry on an interior wall",
           "An exterior door is on a wall that doesn't face outside.",
           hint="Put the `entry` (or exterior `door`) on a wall of the room that lies on the "
           "building envelope, not one shared with another room."),
        _c("DOOR_THRESHOLD", I, "Threshold-to-landing drop at the egress door",
           "A reminder-class info (the model has no vertical threshold data, so it "
           "teaches rather than measures): at the required egress door the exterior "
           "landing may be no more than 1.5 in below the top of the threshold — "
           "7.75 in only where the door does not swing out over the landing (IRC "
           "R311.3.1). Nudged once, on the primary entry, and only before a "
           "porch/landing is modelled (mirroring DOOR_NO_LANDING's single info); "
           "once landings are drawn, the CD set carries the detail. Confirm the "
           "landing-to-threshold drop on the construction documents.",
           hint="Confirm on the construction documents that the exterior landing sits no more "
           "than 1.5 in below the egress door's threshold (7.75 in where the door doesn't "
           "swing out over it)."),
        _c("DOOR_NO_LANDING", W, "Exterior door has no landing",
           "An exterior people-door (`entry`) opens onto no landing — IRC R311.3 "
           "requires a floor/landing on each side of an exterior door, at least as "
           "wide as the door and 36 in deep, so you don't step out into space. A "
           "covered-or-open `porch` whose footprint spans the door's exterior face "
           "for the door's full width satisfies it. Only entries are checked (an "
           "overhead garage door needs no landing). Severity varies: with porches "
           "modelled anywhere it's a WARNING on each uncovered entry; on a plan with "
           "NO porches at all it's a single INFO nudge on the primary entry (the "
           "plan simply hasn't drawn porches yet — don't spam every door). Add a "
           "`porch` at the door, or note the landing on the construction documents.",
           hint="Add a `porch` at the door that spans its width and is at least 3 ft deep, or "
           "note the landing on the construction documents.",
           also=(I,)),
        # --- stairs ---------------------------------------------------------
        _c("STAIR_GEOMETRY", E, "Non-finite stair geometry",
           "A stair has nan/inf coordinates or size.",
           hint="Give the stair finite coordinates and size in feet (no nan/inf), e.g. "
           "`stair <id> at <x>,<y> size 4 x 10`."),
        _c("STAIR_SIZE", E, "Non-positive stair size",
           "A stair's width or length is <= 0.",
           hint="Give the stair positive dimensions in feet, e.g. `size 4 x 10`."),
        _c("STAIR_LEVELS", E, "Bad stair levels",
           "A stair must connect two different levels >= 0, e.g. `from 0 to 1`.",
           hint="Connect two different levels, both >= 0, e.g. "
           "`stair <id> at <x>,<y> size 4 x 10 from 0 to 1`."),
        _c("STAIR_OOB", E, "Stair outside the envelope",
           "A stair's footprint extends past the envelope.",
           hint="Move or resize the stair so its whole footprint lies inside the `envelope` or "
           "a `wing`."),
        _c("STAIR_FLOAT", W, "Stair lands in no room",
           "A stair doesn't overlap a room on one of the levels it connects.",
           hint="Position the stair so its footprint overlaps a room on each level it connects "
           "(its `from` and `to` levels)."),
        _c("STAIR_RUN", W, "Stair footprint too small",
           "A stair's footprint is too short to physically hold the run the "
           "ceiling height requires (R311.7: ~7.75 in max riser, 10 in min "
           "tread). Lengthen its footprint or model a switchback.",
           hint="Lengthen the stair's footprint to hold the run its rise needs, or widen it to "
           "about twice the minimum stair width to fit a switchback."),
        _c("STAIR_HANDRAIL", I, "Stair flight needs a handrail",
           "A stair flight of four or more risers requires at least one handrail, "
           "34–38 in above the tread nosings and graspable the full length (IRC "
           "R311.7.8). The DSL doesn't model railings, so this is a one-per-plan "
           "checklist reminder on the first qualifying stair — carry the handrail "
           "onto the construction documents. Fewer than four risers is exempt.",
           hint="Carry a handrail 34-38 in above the nosings, on at least one side of the "
           "flight, onto the construction documents (IRC R311.7.8); the DSL doesn't draw "
           "rails."),
        _c("STAIR_HEADROOM", W, "Stair headroom can't develop",
           "A stair's footprint is too short for a floor opening (stairwell) that "
           "keeps 6 ft 8 in of headroom under the upper floor (IRC R311.7.2). "
           "Lengthen the run/opening or reduce the floor-to-floor height.",
           hint="Lengthen the stair's run and stairwell opening until 6 ft 8 in of headroom "
           "develops, or lower the floor-to-floor height (`ceiling` plus `floor`) so "
           "fewer risers are needed."),
        _c("STAIR_LANDING", I, "Flight too tall — needs an intermediate landing",
           "A single straight flight climbs more than 12 ft 7 in (151 in) of "
           "vertical rise. IRC R311.7.3 limits a flight to that rise between floor "
           "levels or landings, so a taller run needs an intermediate landing (a "
           "switchback or L-turn). Risers are computed the same way as STAIR_RUN "
           "(rise / max riser), so a gentler profile riser is reflected in the "
           "quoted count. A normal one-storey flight stays well under, so this only "
           "speaks up on a tall or multi-level run. INFO — the DSL models one "
           "straight flight, so note the mid-run landing on the construction "
           "documents.",
           hint="Break the flight with an intermediate landing (a switchback or L-turn) and "
           "note it on the construction documents; the DSL models one straight flight."),
        # --- guards & life safety -------------------------------------------
        _c("LOFT_GUARD", I, "Open loft edge needs a guard",
           "An upper-level room only partially covers a room below, so it "
           "overlooks a double-height void. Its open edge is a walking surface "
           "more than 30 in up and needs a 36 in guard with balusters that block a "
           "4 in sphere (IRC R312).",
           hint="Add a 36 in guard along the loft's open edge, with balusters spaced so a 4 in "
           "sphere can't pass (IRC R312)."),
        _c("LOFT_CEILING", W, "Loft ceiling below the habitable minimum",
           "A loft is habitable and often sleeps people, so it needs the IRC "
           "R305.1 7 ft habitable-room minimum ceiling. Its EFFECTIVE ceiling — "
           "the room's `ceiling` override, else the plan ceiling — is under 7 ft, "
           "so it can't be lived or slept in. This is a flat-ceiling check: "
           "barndsl carries no roof-slope geometry, so a sloped-ceiling loft's "
           "'>= 7 ft over at least half the floor' (R305.1.1) can't be measured "
           "here — the hint notes it. A `vaulted` loft is open to the ridge (its "
           "usable height rises well past 7 ft) and is exempt.",
           hint="Raise the loft's ceiling to 7 ft or more via its `ceiling <h>` override or "
           "the plan `ceiling`; under a sloped roof, keep 7 ft over at least half the "
           "floor."),
        _c("ALARM_CO", I, "Smoke/CO alarms",
           "Two roles, both info. On a plan that has bedrooms but declares NO "
           "`alarm`, a teaching reminder to place smoke/CO alarms (IRC R314/R315). "
           "Once alarms ARE declared, the carbon-monoxide check: bedrooms coexist "
           "with an attached garage/shop but no `co`/`smoke_co` alarm sits outside "
           "the sleeping areas (IRC R315). INFO — fuel-fired appliances aren't "
           "modelled, so the attached garage/shop is the only trigger.",
           hint="Declare the alarms so placement can be checked, e.g. "
           "`alarm smoke in <bedroom>`; with an attached garage/shop, add "
           "`alarm co in <hall>` (or `smoke_co`) outside the sleeping areas."),
        _c("ALARM_BEDROOM", W, "Bedroom has no smoke alarm",
           "A bedroom carries no `smoke` (or `smoke_co`) alarm. IRC R314.3 "
           "requires a smoke alarm in each sleeping room. Runs only once a plan "
           "declares any `alarm`. Add `alarm smoke in <bed>`.",
           hint="Add `alarm smoke in <bedroom>` (or `alarm smoke_co in <bedroom>` for a "
           "combination unit) to every sleeping room."),
        _c("ALARM_HALL", W, "No smoke alarm outside a sleeping area",
           "No room adjacent to a bedroom carries a smoke alarm. IRC R314.3 wants "
           "a smoke alarm outside each sleeping area; this approximates 'outside' "
           "as a room sharing a door with the bedroom (a hallway if present, else "
           "any adjacent room). Add `alarm smoke in <hall>`.",
           hint="Add `alarm smoke in <hall>` to the hallway (or other room) that shares a door "
           "with the bedroom, outside the sleeping area."),
        _c("ALARM_LEVEL", W, "Level has no smoke alarm",
           "A storey of the dwelling carries no smoke alarm. IRC R314.3(3) "
           "requires at least one on every level, including basements. Add "
           "`alarm smoke in <room on that level>`.",
           hint="Add `alarm smoke in <room>` for a room on each level that lacks one, "
           "basements included."),
        _c("ELECTRICAL_PLAN", I, "Electrical / life-safety checklist",
           "An opt-in reminder (the `electrical` directive) for code requirements "
           "the DSL can't place from geometry: receptacle spacing (no wall point "
           ">6 ft from an outlet, IRC E3901.2) with GFCI/AFCI protection (E3902), "
           "switched lighting outlets at habitable rooms/halls/entries (R303.7 / "
           "E3903), stair lighting, and a level landing at each exterior door "
           "(R311.3). Carry these onto the construction documents. Once a room "
           "actually draws outlets, the sharper per-room checks (OUTLET_SPACING / "
           "OUTLET_GFCI / ROOM_NO_LIGHT) take over from this reminder.",
           hint="Carry receptacle spacing, GFCI/AFCI protection, switched lighting and "
           "exterior-door landings onto the construction documents, or draw `outlet`, "
           "`switch` and `light` statements so the per-room checks take over."),
        _c("OUTLET_SPACING", W, "Receptacle spacing too wide",
           "A habitable room has drawn receptacles, but there is a point along its "
           "walls more than 6 ft from the nearest one — a lamp/appliance cord "
           "would have to cross a doorway to reach power. IRC E3901.2 requires "
           "receptacles so that no point along a wall line is more than 6 ft from "
           "one (i.e. one at least every 12 ft of wall run, measured around "
           "corners). Add an `outlet` in the worst gap. Only rooms that declare an "
           "outlet are checked (drawing the electrical layer is opt-in).",
           hint="Add an `outlet` in the widest gap so no point along the room's walls is more "
           "than 6 ft from a receptacle (IRC E3901.2).",
           part_local=True),
        _c("RECEPTACLE_COUNTER", W, "Kitchen counter needs a small-appliance receptacle",
           "A kitchen counter run (a `fixture counter ... along` run at least 12 in "
           "wide) leaves a point on the counter wall more than 24 in from a "
           "receptacle. IRC E3901.4 requires small-appliance receptacles spaced so "
           "no point along a counter is more than 24 in from one (receptacles at "
           "most 48 in apart, and one on every counter >= 12 in wide). Add an "
           "`outlet` on the counter wall in the gap. Only checked once a plan draws "
           "its electrical layer (an outlet/switch/light), like OUTLET_SPACING.",
           hint="Add an `outlet` on the counter wall in the gap, e.g. "
           "`outlet in <room> wall <wall> offset <ft>`, so no counter point is over 24 in "
           "from a receptacle."),
        _c("OUTLET_GFCI", W, "Receptacle needs GFCI protection",
           "A receptacle in a kitchen, bathroom, laundry or utility (a wet/damp "
           "location) isn't marked `gfci`. IRC E3902 requires ground-fault "
           "circuit-interrupter protection for receptacles in those rooms (and "
           "outdoors). Add `gfci` to the `outlet`, or protect the circuit at the "
           "panel and note it.",
           hint="Add `gfci` to the receptacle's `outlet` line, or protect the circuit at the "
           "panel and note it on the electrical plan.",
           part_local=True),
        _c("ROOM_NO_LIGHT", I, "Habitable room has power but no light",
           "A habitable room draws receptacles or switches but no lighting outlet. "
           "IRC E3903 requires at least one wall-switch-controlled lighting outlet "
           "in every habitable room (a switched receptacle counts). Add a `light` "
           "(or note a switched receptacle). Advisory — the switch is often there, "
           "just not drawn.",
           hint="Add a wall-switch-controlled light, e.g. `light in <room> at <x>,<y>`, or "
           "note a switched receptacle.",
           part_local=True),
        # --- access ---------------------------------------------------------
        _c("NO_ENTRY", E, "No exterior door",
           "The plan has no exterior people-door — no way to enter the building. "
           "An overhead garage door doesn't count (vehicle access, not an "
           "entrance), so a plan whose only exterior door is overhead still "
           "needs an `entry`.",
           hint="Add a people-door on an exterior wall, e.g. "
           "`entry <room> <wall> width 3 offset <o>`; an overhead garage door doesn't "
           "count as an entrance."),
        _c("NO_ACCESS", E, "Room unreachable",
           "A room can't be reached from any entrance through interior doors "
           "(or, across levels, a `stair` landing in it). A warning (not error) "
           "for a closet/pantry/loft, which may be open to the room beside it.",
           hint="Connect the room with `door <room> - <neighbour>` to a reachable room it "
           "shares a wall with, or give it its own `entry <room> <wall>`; an upper-level "
           "room needs a `stair` landing in it from a reachable room below.",
           also=(W,)),
        # --- egress & light -------------------------------------------------
        _c("BEDROOM_EGRESS", E, "Bedroom has no escape opening",
           "Every bedroom needs an emergency escape opening — a window that "
           "opens, or its own exterior door — on an exterior wall (IRC R310). "
           "A `fixed` window is glass that doesn't open: it daylights but is "
           "never an escape opening.",
           hint="Add an operable window (casement, slider or double-hung, not `fixed`) on an "
           "exterior wall, e.g. `window <room> <wall> width 4`, or give the bedroom its "
           "own `entry <room> <wall>`; a bedroom with no exterior wall must move to the "
           "perimeter."),
        _c("EGRESS_SIZE", W, "Egress opening too small",
           "A bedroom's escape opening is below the IRC R310 minimums: ~5.7 sq "
           "ft net clear opening (5.0 at grade), >= 20 in clear width, >= 24 in "
           "clear height, sill <= 44 in above the floor. The clear opening "
           "follows the window kind: a casement clears ~its full glazed size, a "
           "slider ~half its glazed width, a double-hung ~half its glazed "
           "height; fixed glass never counts.",
           hint="Enlarge the escape window or lower its `sill` to meet the profile's R310 "
           "minimums (by default 5.7 sq ft net clear, 5.0 at grade; 20 in wide, 24 in "
           "high; sill at most 44 in); a `casement` clears its full size, a `slider` or "
           "`double-hung` about half."),
        _c("EGRESS_DOOR", W, "No wide egress door",
           "No exterior egress door is at least 32 in clear wide (R311.2). A "
           "double/french pair provides its required clear width through ONE "
           "leaf, so it counts half its total width.",
           hint="Make at least one `entry` at least 32 in wide (`width 2-8` or more); a "
           "`double` or `french` pair counts one leaf, so it needs twice that."),
        _c("NAT_LIGHT", W, "Insufficient natural light",
           "A habitable room's glazing on exterior walls is below 8% of floor "
           "area (R303.1).",
           hint="Add window area on the room's exterior walls "
           "(`window <room> <wall> width <w>`) until its glazing reaches the profile's "
           "daylight ratio (8% of floor area under the IRC); move a room with no exterior "
           "wall to the perimeter."),
        _c("VENT_AREA", W, "Insufficient natural ventilation",
           "A habitable room's OPENABLE window area on exterior walls is below 4% "
           "of its floor area (IRC R303.1's natural-ventilation floor, half the 8% "
           "glazing floor). A `fixed` window daylights but opens nothing, so it "
           "counts for NAT_LIGHT but not here. Make a window operable "
           "(casement/slider/double-hung), widen one, or confirm mechanical "
           "ventilation on the construction documents.",
           hint="Make a window operable (`casement`, `slider` or `double-hung` rather than "
           "`fixed`) or widen one on an exterior wall, or confirm mechanical ventilation "
           "on the construction documents."),
        # --- solar orientation (advisory; needs a declared `orientation`) ---
        _c("SOLAR_WEST_GAIN", I, "Overheating west glazing",
           "A habitable room has a lot of west-facing glass. The low afternoon sun "
           "on a west wall is hard to shade and overheats the room. Shade it with a "
           "deep overhang/porch or awning, cut it back, or move it to the south "
           "face. Runs only when the plan declares an `orientation` (northern "
           "hemisphere).",
           hint="Shade the west glass with a deep `overhang`, covered porch or awning, cut it "
           "back, or move it to the south face."),
        _c("SOLAR_NORTH_ONLY", I, "Room lit only from the north",
           "A living/dining/bedroom/kitchen is glazed only to the north — little "
           "direct sun, so it feels dim and cold in winter — while it has a sunnier "
           "(south/east/west) exterior wall to spare. Add a window on that wall. "
           "Offices are exempt (even north light is a valid studio choice). Runs "
           "only when the plan declares an `orientation`.",
           hint="Add a `window <room> <wall>` on a south, east or west exterior wall for "
           "winter sun, or keep it and record why with "
           "`# barndsl: accept SOLAR_NORTH_ONLY \"reason\"`."),
        _c("SOLAR_SOUTH_UNUSED", I, "South wall left unglazed",
           "The plan has a substantial south-facing exterior wall but almost no "
           "south glazing — the best passive-solar face is nearly blank. South "
           "glass gives free low-angle winter sun that a summer-blocking overhang "
           "can shade. Runs only when the plan declares an `orientation`.",
           hint="Add south-facing windows for free winter sun, shaded by an `overhang` or "
           "covered porch against the high summer sun."),
        _c("SOLAR_SOUTH_NO_OVERHANG", I, "Unshaded south glazing",
           "The plan has a lot of south glazing but no roof `overhang` (and no "
           "covered porch over it) to shade it — the high summer sun overheats "
           "those rooms. A ~2 ft eave blocks the summer sun while still admitting "
           "the low winter sun. Runs only when the plan declares an `orientation`.",
           hint="Add a ~2 ft eave with `overhang 2`, or a covered `porch` over the south "
           "glass, to block the high summer sun while admitting winter sun."),
        # --- thermal envelope (advisory; needs a declared `climate` zone) ---
        _c("ENERGY_ENVELOPE", I, "Envelope R-value guidance",
           "The prescriptive envelope targets (ceiling/wall/floor/slab R-values and "
           "window U-factor) for the plan's declared IECC `climate` zone, plus the "
           "steel-frame thermal-bridge note: insulate a metal shell with continuous "
           "exterior insulation, since steel studs short-circuit cavity insulation. "
           "Guidance, not the code of record — confirm with the adopted energy code.",
           hint="Confirm the R-values and window U-factor against the adopted energy code, "
           "ideally with a rater; on a steel frame, use continuous exterior insulation."),
        _c("WINDOW_HEAVY", I, "High window-to-wall ratio",
           "Glazing exceeds ~28% of the gross exterior wall area — a high "
           "window-to-wall ratio that drives the heating/cooling load. The daylight "
           "floor (NAT_LIGHT, 8%) is the minimum; this is the practical ceiling. "
           "Concentrate glass on the south (winter gain) and shade it. Runs only "
           "when the plan declares a `climate` zone.",
           hint="Trim glazing toward ~28% of the exterior wall area, or concentrate it on the "
           "south and shade it with an `overhang`."),
        _c("APPROACH_ENTRY", I, "Front door doesn't face the street",
           "No people-door is on the wall the `street` directive names as facing "
           "the approach — the front door is around the side or back. Runs only "
           "when the plan declares a `street`.",
           hint="Put a people-door on the street-facing wall, e.g. "
           "`entry <room> <wall> width 3`, or keep it and record why with "
           "`# barndsl: accept APPROACH_ENTRY \"reason\"`."),
        _c("APPROACH_GARAGE", I, "Garage faces away from the street",
           "An overhead/garage door is on the wall opposite the `street` side, so a "
           "vehicle would have to drive around the house to reach it. Face it toward "
           "the approach or a side wall. Runs only when the plan declares a "
           "`street`.",
           hint="Move the overhead door to the street-facing wall or a side wall, or keep it "
           "and record why with `# barndsl: accept APPROACH_GARAGE \"reason\"`."),
        # --- design quality (advisory) --------------------------------------
        _c("KITCHEN_FLOW", I, "Kitchen not open to living/dining",
           "An idiomatic barndo opens the kitchen to a dining or living area.",
           hint="Open the kitchen to a dining or living area, e.g. `open <kitchen> - <room>`, "
           "or keep it closed and record why with "
           "`# barndsl: accept KITCHEN_FLOW \"reason\"`."),
        _c("KITCHEN_PASSTHROUGH", W, "Kitchen used as public circulation",
           "The kitchen is the only route between dining and a living/great/rec "
           "room, so everyday traffic crosses the work zone. Open those public "
           "rooms directly to each other or route circulation around the kitchen.",
           hint="Open the living or great room directly to dining, or add a hall path around "
           "the kitchen so everyday traffic bypasses the work zone."),
        _c("BED_PRIVACY", I, "Bedroom opens onto a public room",
           "A bedroom opening straight onto living/kitchen/dining lacks privacy; "
           "buffer it with a hallway.",
           hint="Open the bedroom off a `hallway` rather than the living, kitchen or dining "
           "area, or keep it and record why with "
           "`# barndsl: accept BED_PRIVACY \"reason\"`."),
        _c("BATH_DISTANCE", I, "Bedroom far from a bath",
           "A bedroom is more than two doors from any bathroom.",
           hint="Site a bath next to the bedrooms, ideally off the same hallway, or keep it "
           "and record why with `# barndsl: accept BATH_DISTANCE \"reason\"`.",
           part_local=True),
        _c("BATH_VENT", I, "Windowless bathroom",
           "A bathroom has no exterior window, so it needs mechanical ventilation "
           "(IRC R303.3). The DSL can't model fans — confirm an exhaust fan.",
           hint="Add an exterior window to the bathroom; if an exhaust fan vented outside is "
           "planned instead, keep it and record why with "
           "`# barndsl: accept BATH_VENT \"reason\"`."),
        _c("HALL_DEADEND", I, "Hallway dead end",
           "A hallway opens onto at most one room (a 1-room foyer is exempt), or it "
           "runs well past its last doorway into a blank wall — a dead-end stub. "
           "Trim it back to its last door, or put a room at the dead end.",
           hint="Trim the hall back to its last doorway, cap its end with a room's door, or "
           "drop a one-room hall and open that room off a larger space."),
        _c("HALL_TIGHT", I, "Hallway tight",
           "A hallway meets the 3 ft code minimum but is under the 4 ft that's "
           "comfortable for two people or moving furniture.",
           hint="Widen the hallway to the profile's comfortable width (4 ft by default), or "
           "keep it and record why with `# barndsl: accept HALL_TIGHT \"reason\"`."),
        _c("NO_BACK_DOOR", I, "Only one exterior door",
           "A home wants a front *and* a back door — a second exterior door (off "
           "the kitchen, mudroom or laundry, on another wall) for daily flow and a "
           "second way out. Garage/porch doors and overhead doors don't count.",
           hint="Add a second exterior door on another wall, off the kitchen, mudroom or "
           "laundry: `entry <room> <wall>`. Or keep it and record why with "
           "`# barndsl: accept NO_BACK_DOOR \"reason\"`."),
        _c("BATH_OVERSIZE", I, "Ensuite larger than its bedroom",
           "A private (ensuite) bath is larger than the bedroom it serves, a sign "
           "the suite is mis-proportioned. A bath should be the same size or smaller.",
           hint="Shrink the ensuite or enlarge its bedroom so the bath is no larger than the "
           "room it serves, or keep it and record why with "
           "`# barndsl: accept BATH_OVERSIZE \"reason\"`.",
           part_local=True),
        _c("STAIR_BLOCKS_DOOR", W, "Stair blocks a doorway",
           "A stair's footprint intrudes on the clear floor in front of a door, so "
           "you'd step off the stair straight into the doorway. Place the stair "
           "along a wall, clear of door approaches.",
           hint="Shift the stair along a wall clear of the doorway, or move the door so its "
           "approach is clear."),
        _c("STAIR_WALL", I, "Stair floats free of any wall",
           "A stair sits in the middle of a room rather than along an exterior or "
           "partition wall, where it would need railings all round and chops up the "
           "floor. (Mid-flight landings/turns aren't modelled.)",
           hint="Move the stair so a long side runs against an exterior or partition wall, or "
           "keep it and record why with `# barndsl: accept STAIR_WALL \"reason\"`."),
        _c("DOOR_CENTERED", I, "Door floats mid-wall",
           "A swing door is centred on a wall with usable wall on both flanks; "
           "backing it to a corner leaves one unbroken run to line with furniture. "
           "Only un-positioned swing leaves are flagged.",
           hint="Back the door toward a corner with `offset` (~0.5 ft for trim) to leave one "
           "full wall run, or keep it and record why with "
           "`# barndsl: accept DOOR_CENTERED \"reason\"`."),
        _c("WINDOW_PARTITION", I, "Window butts an interior wall",
           "A window sits against an interior partition where it meets the exterior "
           "wall — no room for framing/trim, and it reads off-balance. Pull it "
           "toward the wall centre or a true building corner; space windows evenly.",
           hint="Adjust the window's `offset` to pull it toward the wall's centre or a "
           "building corner, leaving clear wall on both sides; space multiple windows "
           "evenly."),
        _c("DOOR_SWING_CLASH", I, "Door swings overlap",
           "Two door leaves sweep into the same space and would foul each other. "
           "Move one along its wall, narrow it, swing it the other way (`into` / "
           "`hinge`), or make one a pocket/sliding door.",
           hint="Move one door along its wall, swing it the other way (`into <room>` / "
           "`hinge near|far`), or make one a `pocket` or `sliding` door.",
           part_local=True),
        _c("ENVELOPE_MODULE", I, "Exterior dimension off the build module",
           "An exterior (envelope or wing) measurement isn't a whole multiple of the "
           "3 ft build module. Rounding exterior dimensions to the module cuts sheet "
           "goods and framing with less waste.",
           hint="Round envelope and wing dimensions to whole multiples of the 3 ft build "
           "module, or keep them and record why with "
           "`# barndsl: accept ENVELOPE_MODULE \"reason\"`."),
        _c("PRIVATE_PASSTHROUGH", W, "Routed through a private room",
           "A room is reachable only by passing through a bathroom or someone "
           "else's bedroom — a circulation defect.",
           hint="Route the room off a hallway or living space instead of through the bathroom "
           "or bedroom, e.g. `door <room> - <hall>`."),
        _c("ENTRY_PRIVATE", W, "Entry into a private room",
           "An exterior entry opens into a bathroom (warning) or a bedroom "
           "(info — it might be a patio door).",
           hint="Land the entry in a mudroom, hall or living space, not a bath or bedroom; for "
           "a deliberate bedroom patio door, keep it and record why with "
           "`# barndsl: accept ENTRY_PRIVATE \"reason\"`.",
           also=(I,)),
        _c("WET_GROUP", I, "Scattered plumbing",
           "Three or more wet rooms (bath/kitchen/laundry/utility) share no "
           "walls, spreading plumbing runs out. A declared plumbing wall — "
           "`wall <bath> - <neighbour> plumbing` — that a wet room really backs "
           "onto also satisfies this: the wet wall exists, just shared with a "
           "dry room.",
           hint="Group wet rooms back-to-back on a shared wall, or declare the wall their "
           "fixtures back onto: `wall <bath> - <neighbour> plumbing`."),
        _c("PLUMBING_STACK", I, "Upper wet room not stacked",
           "An upper-floor wet room (bath/kitchen/laundry) sits over no wet room "
           "on the level below, so its waste stack can't drop straight down and "
           "must jog horizontally through the floor assembly and down through a "
           "dry room. Stack it over a wet room below (the cross-floor analogue of "
           "WET_GROUP).",
           hint="Stack the upper wet room over a bath, kitchen or laundry below, aligning "
           "their wet walls. Or keep it and record why with "
           "`# barndsl: accept PLUMBING_STACK \"reason\"`."),
        _c("CLOSET_SHAPE", I, "Long, skinny closet",
           "A closet has the floor area for a walk-in but is shaped as a narrow "
           "strip (>= 4:1). A more square footprint (under ~3:1, >= 4 ft deep) is "
           "a usable walk-in. Small reach-ins and wide/shallow closets are exempt.",
           hint="Reshape it into a squarer walk-in (under ~3:1, at least 4 ft deep), or make "
           "it a shallow reach-in behind a near-full-width `bifold` door.",
           part_local=True),
        _c("CLOSET_ACCESS", W, "Reach-in closet with a blind rod",
           "A closet under 4 ft deep is a REACH-IN — nobody can step inside, so "
           "everything past arm's reach (~2 ft) of the door jambs is dead "
           "storage. Its door should be a bifold centred on the closet and "
           "nearly as wide as it, so every foot of rod is reachable. A person-"
           "door parked at one end of a wide reach-in strands the rest of the "
           "closet. Walk-ins (4 ft and deeper) take an ordinary door and are "
           "exempt, as are walk-through closets with two openings.",
           hint="Use a `bifold` door centred on the reach-in and nearly as wide as it, e.g. "
           "`door <room> - <closet> bifold width <w>`, so every foot of rod is reachable."),
        _c("PANTRY_ACCESS", W, "Reach-in pantry with unreachable shelves",
           "A pantry under 4 ft deep is a REACH-IN — nobody can step inside, so "
           "the shelves past arm's reach (~2 ft) of the door jambs are beyond "
           "reach: the groceries at the back can't be got at. Its single door "
           "should be a bifold centred on the pantry and nearly as wide as it, so "
           "every shelf is reachable. A person-door parked at one end of a wide "
           "reach-in strands the far shelves. The closet analog (CLOSET_ACCESS) — "
           "walk-in pantries (4 ft and deeper) take an ordinary door and are "
           "exempt, as are walk-through pantries with two openings.",
           hint="Give a reach-in pantry (under 4 ft deep) one `bifold` door centred on it and "
           "nearly as wide: `door <pantry> - <room> bifold width <w> offset <o>`."),
        _c("CLOSET_DEPTH", W, "Bedroom closet too shallow to hang clothes",
           "A closet serving a bedroom is under 2 ft in its short dimension — "
           "hanging clothes are 2 ft deep (24 in hangers), so nothing hangs in "
           "it and the bedroom effectively has no clothes closet. 2 - 2.5 ft is "
           "the reach-in standard. A shallow closet off a hall is exempt (a "
           "linen/broom cabinet is legitimate shelf-only storage).",
           hint="Deepen the closet to at least 2 ft (2–2.5 ft is the reach-in standard); "
           "anything shallower is shelf-only storage, not a clothes closet."),
        _c("CLOSET_WINDOW", I, "Window in a closet",
           "A closet has a window: sunlight fades clothes, the glass eats the "
           "wall the rod wants, and the stretch of exterior wall (and its "
           "daylight) would serve a habitable room better. Bury closets on "
           "interior walls.",
           hint="Move the closet onto interior walls and give that exterior wall and its "
           "window to a habitable room, or keep it and record why with "
           "`# barndsl: accept CLOSET_WINDOW \"reason\"`."),
        _c("BED_SOUND", I, "Bedrooms share a party wall",
           "Two bedrooms share a wall directly, so sound carries between them. "
           "Stack each bedroom's closet on the shared wall (back-to-back) to buffer "
           "the sleeping rooms, or put a hall/closet between them.",
           hint="Stack a closet on each side of the shared wall or put a hall or closet "
           "between the bedrooms, or keep it and record why with "
           "`# barndsl: accept BED_SOUND \"reason\"`."),
        _c("NO_CLOSET", I, "Bedroom has no usable closet",
           "A bedroom has no closet reached by a door from it — either none "
           "abuts it, or one abuts but with no door into it (e.g. a neighbour's "
           "closet).",
           hint="Give the bedroom its own closet with a door into it, e.g. "
           "`room <closet>: closet east-of <bedroom> size 6 x 3` and "
           "`door <bedroom> - <closet>`."),
        _c("LOW_STORAGE", I, "Storage-poor plan",
           "Dedicated storage (closets + pantry + storage rooms) is below a small fraction of the "
           "conditioned floor area — the whole-house storage the review flagged as "
           "invisible, now visible. Conservative floor (below the worked gallery), "
           "so it only catches a home with almost no closets. Declare a specific "
           "target with `program ... storage <sqft>`.",
           hint="Add closets, a pantry or a storage room (a linen closet by the baths, a coat "
           "closet at the entry), or declare a target with `program ... storage <sqft>`."),
        _c("SAFE_ROOM_WINDOW", W, "Safe room has a window",
           "A safe/storm room should not have ordinary exterior glazing: windborne "
           "debris and pressure failure defeat the protected-room intent.",
           hint="Move the safe room to an interior location with no window, or document a "
           "rated storm shutter or window assembly outside the DSL."),
        _c("SAFE_ROOM_EXTERIOR", W, "Safe room on exterior wall",
           "A protected room works best buried in the interior. Exterior walls take "
           "the storm/debris load unless specifically hardened.",
           hint="Move the safe room to the interior, surrounded by other spaces; if it must "
           "touch the shell, harden that exterior wall assembly."),
        _c("SAFE_ROOM_SIZE", W, "Safe room too small or skinny",
           "A safe/storm room needs enough clear floor for occupants and a usable "
           "door swing; a long 4-ft strip reads as a closet/corridor label, not a shelter.",
           hint="Make the safe room a compact shelter with enough clear floor for its "
           "occupants and a usable door swing, not a long narrow strip."),
        _c("SAFE_ROOM_ACCESS", W, "Safe room access problem",
           "A safe/storm room should have a real door and should not be a "
           "pass-through corridor to other rooms.",
           hint="Give the safe room a real door leaf, e.g. `door <safe_room> - <hall> swing`, "
           "and put it off a hall or core rather than on the route between rooms."),
        _c("MECH_CLEARANCE", W, "Mechanical room too small",
           "A mechanical room needs working clearance for equipment service — too "
           "small and it becomes an inaccessible utility closet.",
           hint="Enlarge the mechanical room so its equipment has service clearance, or fold "
           "it into a larger utility room."),
        _c("MECH_ACCESS", W, "Mechanical room access problem",
           "Mechanical equipment needs a real service door and should not sit on the "
           "only circulation route between other rooms.",
           hint="Give the mechanical room a service door from a hall, utility, mudroom or "
           "garage (`door <mech> - <room>`), and route circulation around it, not through "
           "it."),
        _c("MECH_BEDROOM", W, "Mechanical room opens to bedroom",
           "Avoid a direct mechanical-room door into a sleeping room: noise, service "
           "traffic and combustion/equipment risk belong off hall/utility space.",
           hint="Move the mechanical room's door off the bedroom and onto a hall, utility, "
           "mudroom or garage."),
        _c("FOYER_FLOW", W, "Foyer entry flow problem",
           "A foyer should be the public arrival point: it wants an exterior entry, "
           "standing room, and a connection to the public core rather than only "
           "private rooms. A foyer that connects only to private/service rooms is a "
           "warning; a missing entry door or a tight foyer is an info.",
           hint="Give the foyer an exterior `entry`, enough standing room, and a door or "
           "opening to the public core or a hall, not only to private or service rooms.",
           also=(I,)),
        _c("FOYER_SHAPE", I, "Foyer shaped like a corridor",
           "A foyer/entry should be a compact arrival space, not a long hallway "
           "running the length of the house.",
           hint="Make the foyer a compact arrival room, or type it `hallway` if it is meant to "
           "be a circulation spine."),
        _c("STORAGE_SHAPE", I, "Storage room awkwardly shaped",
           "A storage room with walk-in area but a long skinny footprint wastes "
           "floor as aisle; make it compact or relabel it as hallway/cabinetry.",
           hint="Make the walk-in storage more compact, or split it into closets along "
           "circulation, or keep it and record why with "
           "`# barndsl: accept STORAGE_SHAPE \"reason\"`."),
        _c("STORAGE_ACCESS", W, "Storage room access problem",
           "Storage should have a usable door/opening. A shallow reach-in needs a "
           "wide, centred door so stored goods aren't stranded beyond arm's reach. "
           "A storage room with no opening is a warning; one used as a pass-through "
           "between other rooms is an info.",
           hint="Give the storage room its own usable opening off a hall or core, e.g. "
           "`door <room> - <hall>`, instead of routing circulation through it; or relabel "
           "a dead pocket.",
           also=(I,)),
        _c("GREAT_ROOM_SCALE", I, "Great room lacks great-room scale",
           "A great room should be larger and taller/opener than an ordinary living "
           "room; otherwise the label overstates the space.",
           hint="Enlarge the great room and give it a taller `ceiling <h>` or mark it "
           "`vaulted`, or retype it as `living` if it is a modest room."),
        _c("GREAT_ROOM_FLOW", I, "Great room not tied to public core",
           "A great room should anchor the shared living core, connected/open to "
           "kitchen or dining rather than isolated like a separate room.",
           hint="Tie the great room to the kitchen or dining with a wide cased opening, e.g. "
           "`open <room> - <kitchen_or_dining>`."),
        _c("FLEX_FUTURE_BED", I, "Flex room not bedroom-ready",
           "A flex room often becomes a guest room or bedroom later; egress and a "
           "closet make that conversion practical.",
           hint="To keep it bedroom-ready, give the flex room an exterior wall with an "
           "escape-capable window and a closet, or keep it and record why with "
           "`# barndsl: accept FLEX_FUTURE_BED \"reason\"`."),
        _c("REC_ROOM_SCALE", I, "Rec room too small",
           "A recreation/game room needs more floor area than a small office or den "
           "to hold activity, furniture and circulation.",
           hint="Grow the rec room, or retype a small multipurpose room as `flex` or `office`. "
           "Or keep it and record why with `# barndsl: accept REC_ROOM_SCALE \"reason\"`."),
        _c("REC_ROOM_NOISE", I, "Rec room shares bedroom wall",
           "A recreation room is a noisy public space; a direct party wall with a "
           "bedroom needs a buffer such as hall, closet or storage.",
           hint="Put a hall, closet, storage room or bath between the rec room and the "
           "bedroom. Or keep it and record why with "
           "`# barndsl: accept REC_ROOM_NOISE \"reason\"`."),
        _c("MASTER_ENSUITE", I, "No private ensuite",
           "On a floor with two or more full bathrooms, no bedroom has a private "
           "(ensuite) bath — every bath is shared. The primary bedroom should get "
           "its own.",
           hint="Give the primary bedroom its own bath: `door <bedroom> - <bath>`, with that "
           "bath connected to nothing else."),
        _c("ROOM_PROPORTION", I, "Awkwardly elongated room",
           "A habitable room is more than ~3:1 long-to-short and hard to furnish.",
           hint="Bring the room under about 3:1 by widening the short side or splitting the "
           "space. Or keep it and record why with "
           "`# barndsl: accept ROOM_PROPORTION \"reason\"`.",
           part_local=True),
        _c("ROOM_SKINNY", W, "Room extremely long and skinny",
           "A non-circulation room is extremely elongated, so it reads as leftover "
           "corridor space rather than a usable room. Split it, widen it, or relabel "
           "it as circulation/storage if that is the intent.",
           hint="Widen the short side or split the room into smaller rooms; if it is meant as "
           "a linear strip, retype it as `hallway` or `storage`."),
        _c("LAUNDRY_FIT", W, "Laundry can't hold its washer/dryer",
           "The laundry's clear interior can't fit a washer and dryer (2.25 ft "
           "deep) with a 3 ft working aisle to load them — about 5.5 ft of clear "
           "depth. Widen the room, or fold laundry into a bigger mudroom/utility.",
           hint="Give the laundry ~5.5 ft of clear depth (washer/dryer plus a 3 ft aisle), or "
           "fold it into a larger mudroom or utility room.",
           part_local=True),
        _c("MUDROOM_SHAPE", W, "Mudroom too narrow or too elongated",
           "A mudroom under ~5 ft wide CANNOT do its job — a bench and hooks "
           "(~1.5 ft) plus a 3 ft walkway physically don't fit — so that is a "
           "WARNING (it's a hallway wearing a mudroom label). One that is wide "
           "enough but past ~2.5:1 reads as a corridor and stays an INFO nudge. "
           "Aim near a compact 6 x 8; give surplus length to the shop, laundry "
           "or pantry.",
           hint="Make the mudroom at least ~5 ft wide, near 6 x 8; give surplus length to the "
           "shop, laundry or pantry, or relabel a narrow strip as a `hallway`.",
           also=(I,),
           part_local=True),
        _c("SHOP_DEPTH", W, "Shop bay too narrow to work in",
           "A shop's shortest side is too small for the bay's job — the shop "
           "analog of MUDROOM_SHAPE. Under 12 ft it CANNOT hold a vehicle or a "
           "workbench wall plus a working aisle, so it is storage mislabeled as a "
           "shop — a WARNING (the geometry proves it). Between 12 and 20 ft it "
           "works but is tight for a full-size truck (8.5 ft wide plus door swing) "
           "with a work zone along a wall — an INFO comfort nudge. A GARAGE is "
           "exempt (garages are sized to cars, not equipment); only a SHOP bay is "
           "judged. Widen it, or relabel a genuine storage strip as storage/utility.",
           hint="Widen the shop's shortest side to at least 12 ft (20 ft for a comfortable "
           "truck bay), or relabel a narrow strip as `storage` or `utility`.",
           also=(I,)),
        _c("GARAGE_BEDROOM", W, "Garage/shop opens into a bedroom",
           "A garage or shop must not open directly into a sleeping room (IRC "
           "R302.5.1). A barndominium shop bay is treated as a garage.",
           hint="Remove the garage-to-bedroom door and connect the garage through a mudroom or "
           "hall instead, e.g. `door <garage> - <mudroom>`."),
        _c("GARAGE_PASSTHROUGH", W, "Bedrooms reached only through the garage/shop",
           "The only interior route from the public core (living/kitchen/dining) to "
           "one or more bedrooms passes through a garage or shop — the vehicle bay "
           "is a corridor, so you must cross it (fumes, cold, no fire separation on "
           "the path) to reach the sleeping rooms. Subtler and more dangerous than "
           "GARAGE_BEDROOM (a direct garage↔bedroom door): here no single door is "
           "garage↔bedroom, yet the garage is a cut vertex on the whole route. A "
           "circulation-shape defect reachability (NO_ACCESS) can't see. This is a "
           "structural design flaw — it cannot be waived with an `accept` pragma. "
           "Route the bedrooms off a hall that reaches the core without crossing "
           "the garage.",
           hint="Route the bedrooms off a hall that reaches the public core without crossing "
           "the garage or shop, e.g. a `door` from that hall to a living, kitchen or "
           "dining room.",
           accept_denied=True),
        _c("GARAGE_NO_ENTRY", I, "Garage/shop has no people-door",
           "A garage or shop abuts the house but has no interior door into it.",
           hint="Add a people-door from the garage or shop into an adjacent mudroom, hall or "
           "living space, e.g. `door <garage> - <room>`."),
        _c("GARAGE_VEHICLE_DOOR", W, "Garage/shop has no vehicle door",
           "A garage/shop bay needs an exterior overhead/sectional door; otherwise "
           "it is trapped behind the dwelling and cannot function as a vehicle or equipment bay.",
           hint="Add `door <garage> <wall> overhead` on an exterior wall of the bay; if it has "
           "none, move the garage or shop to the perimeter first."),
        _c("GARAGE_SEPARATION", I, "Garage/dwelling fire separation required",
           "A garage or shop shares a wall with conditioned space, or has habitable "
           "space above it. IRC R302.6 requires the common wall to be a fire "
           "separation (min ½ in gypsum) and, where a habitable room is above, the "
           "ceiling to be ⅝ in Type X gypsum. A barndominium shop bay is treated as "
           "a garage. Declaring the detailed wall — `wall <garage> - <room> rated` "
           "— records the separation and silences the reminder for that pair "
           "(verified, not just reminded); a ceiling can't be declared, so "
           "habitable space above keeps reminding.",
           hint="Detail the common wall as a fire separation (min ½ in gypsum; ⅝ in Type X "
           "ceiling under habitable space), then declare `wall <garage> - <room> rated`."),
        _c("GARAGE_DOOR", I, "Garage/dwelling door must be self-closing & rated",
           "A door between a garage or shop and the dwelling must be self-closing "
           "and 20-minute fire-rated (or a solid-core/solid-wood door at least "
           "1-3/8 in thick) per IRC R302.5.1. The reminder anchors on the `door` "
           "statement itself — the opening that has to carry the rated leaf. A door "
           "into a sleeping room is barred outright (GARAGE_BEDROOM).",
           hint="Specify a self-closing, 20-minute fire-rated (or at least 1-3/8 in "
           "solid-core) door on the garage-to-dwelling opening."),
        _c("CLOSET_DOOR_SWING", I, "Swing door fills a shallow closet",
           "A swing door serves a closet shallower than the door is wide, so the "
           "leaf can't fully open inside it. There is no IRC rule here — it's a "
           "usability nudge. Make it a bypass/sliding or bifold door so the leaf "
           "doesn't fill the closet. Only a leaf that swings into the closet is "
           "judged — by its `into`, or by default when it fits no better in the "
           "room; a leaf that opens into the room is fine.",
           hint="Make the closet door `bifold` or `sliding`, or swing the leaf into the room "
           "with `into <room>`, so it doesn't fill the closet."),
        _c("PROGRAM_MISMATCH", W, "Plan doesn't match its program",
           "The rooms placed don't match the declared `program`: exact bed/bath "
           "counts, an at-least requirement for another room type (e.g. "
           "`1 laundry`), or a minimum conditioned `area`.",
           hint="Add or remove rooms until the bed/bath counts, required room types and `area` "
           "match, or update the `program` line to the intent you mean."),
        _c("PROGRAM_AREA_OVERRUN", I, "Drawn area exceeds declared program area",
           "The source declares a `program ... area` value but the resolved "
           "conditioned interior is substantially larger. `program area` still "
           "acts as a minimum for compatibility; this nudge catches the common "
           "case where an author/model meant it as a target from the brief.",
           hint="If the brief's area is a target, shrink the envelope or rooms toward it; if "
           "it is only a minimum, raise or omit `area` on the `program` line."),
        _c("BRIEF_ACCEPTANCE", I, "Agent candidate misses harness acceptance",
           "A deterministic agent harness folded an acceptance shortfall (missing "
           "program rooms, area band, minimum score, or critic approval) into the "
           "diagnostic stream so the next LLM revision sees it like compiler "
           "feedback. It is not emitted by normal compilation.",
           hint="Revise the plan against the brief: add the missing program rooms, bring the "
           "area into its band, and raise the score or answer the critic's objections."),
        _c("REQUIRE_UNMET", W, "Plan doesn't satisfy a `require` statement",
           "The compiled geometry doesn't satisfy a declared spatial requirement: "
           "`adjacent` needs a shared wall on the same level (purely geometric — "
           "a door or cased opening alone doesn't count; adjacency is what a "
           "`door` needs, so the checks agree), `separate` forbids one (rooms on "
           "different levels are trivially separate), `exterior` needs a wall on "
           "the footprint edge (optionally a specific side), and `area` sets a "
           "minimum nominal room area (the figure `program area` uses). Like "
           "PROGRAM_MISMATCH it's a contract check on intent, not a code "
           "violation, so it warns rather than blocks.",
           hint="Change the geometry to satisfy it: abut the rooms for `adjacent`, buffer them "
           "for `separate`, move the room to the footprint edge for `exterior`, or "
           "enlarge it for `area`."),
        _c("REQUIRE_REF", E, "Requirement references unknown room",
           "A `require` statement names a room id that doesn't exist — a mistyped "
           "id would otherwise silently check nothing.",
           hint="Correct the id in the `require` statement to an existing room, or declare the "
           "room it names."),
        # --- declared wall attributes (the `wall` statement) ------------------
        _c("WALL_REF", E, "Wall statement references unknown room",
           "A `wall` statement names a room id that doesn't exist — a mistyped id "
           "would otherwise silently declare nothing.",
           hint="Reference an existing room id in the `wall` statement (check for typos), or "
           "declare the room."),
        _c("WALL_NOADJ", E, "Wall statement between non-adjacent rooms",
           "A `wall` statement declares attributes of the shared wall between two "
           "rooms, but the pair doesn't share one (a corner touch isn't enough, "
           "and rooms on different levels never share a wall) — the declared wall "
           "doesn't exist. Same geometry rule an interior `door` needs.",
           hint="Name two different rooms that share an edge on the same level, repositioning "
           "them to abut if needed, or drop the `wall` statement."),
        _c("WALL_UNUSED", I, "Plumbing wall serves no wet room",
           "A wall is declared `plumbing` (a 2x6 wet wall for supply/waste runs) "
           "but neither room flanking it is a bath, kitchen, laundry or utility — "
           "the declaration matches no fixtures. Put the wet wall where fixtures "
           "back onto it, or drop the attribute.",
           hint="Move the `plumbing` attribute to a wall that a bath, kitchen, laundry or "
           "utility backs onto, or drop it."),
        _c("WALL_BEARING_AXIS", I, "Bearing wall runs across the frame's span",
           "A wall declared `bearing` runs parallel to the frame's bents (across "
           "the span), so it can't carry an interior post line — post lines run "
           "along the building's long axis, splitting the bents' clear span. The "
           "frame ignored the declaration; declare a wall running the long way, "
           "or leave the span to the auto interior supports.",
           hint="Declare `bearing` on a wall running along the building's long axis instead, "
           "or drop it and leave the span to the auto interior supports."),
        # --- suites / zones (the `suite` / `zone` statements) ---------------
        _c("SUITE_SHADOW", W, "Suite id shadows a room id",
           "A suite is named like an existing room, so a zone member with that "
           "name resolves to the room and the suite silently never expands. "
           "Rename the suite.",
           hint="Rename the suite so its id differs from every room id, and update any `zone` "
           "that lists it."),
        _c("SUITE_REF", E, "Suite references unknown room",
           "A `suite` statement lists a member room id that doesn't exist — a "
           "mistyped id would otherwise group nothing. Reference a real room, or "
           "declare it.",
           hint="List only existing room ids in the `suite` (check for typos), or declare the "
           "missing room."),
        _c("SUITE_OVERLAP", W, "Room in more than one suite",
           "A room is declared a member of two different suites. A room belongs "
           "to one suite (a bedroom's own bath/closet), so this is almost always "
           "an authoring slip; drop it from all but one. A warning, not an error "
           "— the plan still builds — matching the other declared-intent checks.",
           hint="Keep each room in one `suite`; drop it from every other suite that lists it."),
        _c("ZONE_REF", E, "Zone references unknown room or suite",
           "A `zone` statement lists a member that names neither a room nor a "
           "declared suite. Zone members are room ids or suite ids; reference an "
           "existing one, or declare it.",
           hint="List only existing room ids or declared suite ids in the `zone` (check for "
           "typos), or declare the missing room or `suite`."),
        _c("ZONE_OVERLAP", W, "Room in more than one zone",
           "A room falls in two zones — directly, or because it is in a suite "
           "that a zone lists while another zone lists the room. Zones are "
           "mutually-exclusive bands (private wing, public core), so this is an "
           "authoring slip; keep each room in one zone.",
           hint="Keep each room in one `zone`; drop it from all but one, remembering that a "
           "room inside a listed suite is already in that suite's zone."),
        _c("ZONE_CROSS", I, "Room crosses its zone's band",
           "A clearly public room (living/kitchen/dining) is the only such room "
           "in a zone that otherwise holds only private rooms (bed/bath), or the "
           "reverse — a public room stranded in the private band. A design nudge, "
           "not a rule: it fires only when the room is in exactly one zone and "
           "that zone is unambiguously the opposite band, so a mixed open-concept "
           "zone (or a plan with no zones) never triggers it.",
           hint="Move the room to a zone of its own band, or regroup the zones consistently; "
           "or keep it and record why with `# barndsl: accept ZONE_CROSS \"reason\"`."),
        # --- structural frame (the `frame` directive) -----------------------
        _c("POST_OBSTRUCT", I, "Support post in open floor",
           "An auto-placed interior support post (needed where the beam span "
           "exceeds the configured limit) lands out in a room's open floor rather "
           "than on a wall line. Align a partition/closet/island to it, or widen "
           "`span` so no interior support is needed.",
           hint="Align a partition, closet or island with the post line, or increase "
           "`frame span`. Or keep the post and record why with "
           "`# barndsl: accept POST_OBSTRUCT \"reason\"`."),
        _c("POST_IN_OPENING", W, "Post inside a window/door",
           "An auto-placed structural post coincides with a window or exterior "
           "door opening — you can't frame an opening through a column. Shift the "
           "opening along its wall into a clear bay (between posts), or change the "
           "`frame bay` spacing so no post lands on it. A post at the opening's "
           "jamb is fine (that's how it's framed); only a post inside it flags.",
           hint="Shift the opening along its wall into a clear bay between posts, or change "
           "the `frame bay` spacing so no post lands inside it."),
        _c("LOAD_PATH", I, "Upper partition unsupported below",
           "An interior wall on an upper level lands over the open floor of a room "
           "below, with no wall, beam, or post beneath it — the floor framing must "
           "carry it (IRC R502). Fine for a light partition on adequate joists; a "
           "bearing wall wants direct support below.",
           hint="Put a wall, beam or post under the partition, or size the framing for it; for "
           "a light partition, keep it and record why with "
           "`# barndsl: accept LOAD_PATH \"reason\"`."),
        _c("BAY_WIDE", I, "Wide frame bay spacing",
           "The frame's bay spacing is wider than typical residential post-frame "
           "(~12 ft on centre). Legal with adequately sized members, but it asks "
           "more of the beams and posts — lower `bay` or have them engineered.",
           hint="Lower the bay spacing, e.g. `frame bay 12`, or keep it with engineer-sized "
           "beams and posts and record why with `# barndsl: accept BAY_WIDE \"reason\"`."),
        # --- agent layer ----------------------------------------------------
        _c("DESIGN", I, "Architect's critique",
           "A design-quality suggestion folded in from the agent's architect review.",
           hint="Act on the architect's suggestion; fix any BLOCKING structural issue first, "
           "since it caps the plan's score until resolved."),
        _c("TRUNCATED", I, "Generated reply was cut off",
           "The agent's generated plan hit the output token cap before it finished "
           "(on both the write and its retry), so the source may be incomplete. "
           "Folded into the feedback so the next revision is written more "
           "concisely.",
           hint="Write a more concise plan that fits the output token cap: omit optional "
           "detail first, then trim rooms or openings."),
        _c("NO_PROGRAM", I, "No `program` statement",
           "The source declares no `program` line, so the compiler cannot check the "
           "plan delivers the brief's beds/baths/area. Derive one from the brief — "
           "`program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]`.",
           hint="Add a `program` line derived from the brief: "
           "`program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]`."),
        # --- Revit build log (`barndsl revit-log`) ---------------------------
        _c("REVIT_FAIL", W, "Element failed to build in Revit",
           "The pyRevit builder hit an API error creating this element (see the "
           "message for Revit's reason); the rest of the build carried on, so the "
           "model is missing it. Usually a template/family problem — check the "
           "build log's resources block for what was picked.",
           hint="Check the build log's resources block for what was picked, then load a "
           "working family into the Revit template or map the pass to a named type in the "
           "config.json sidecar."),
        _c("REVIT_SKIP", W, "Element skipped by the Revit build",
           "The builder had nothing to build this element with — a missing level, "
           "host wall, family or type — so it is absent from the model. Load a "
           "matching family into the template or map the pass to a named type in "
           "the config.json sidecar.",
           hint="Load a matching family or type into the Revit template, or map the pass to a "
           "named type in the config.json sidecar."),
        _c("REVIT_NOTE", I, "Revit build used a stand-in",
           "The element built, but not the way the plan asked: a stand-in family, "
           "a flat fallback for a gable profile, or a type hint that matched "
           "nothing. The model is usable; refine the template (or the config "
           "mapping) to close the gap.",
           hint="Load the family or type the plan asked for into the Revit template, or map "
           "the pass to a named type in the config.json sidecar."),
    ]
)


def explain(code: str) -> str:
    """Return a human-readable explanation of ``code`` (case-insensitive)."""
    info = REGISTRY.get(code.strip().upper())
    if info is None:
        known = ", ".join(sorted(REGISTRY))
        return f"Unknown diagnostic code '{code}'.\nKnown codes: {known}"
    sev = info.severity.value
    if info.varies:
        others = sorted(s.value for s in info.severities - {info.severity})
        sev += f", or {' or '.join(others)} by context"
    return (
        f"{info.code} [{sev}] ({info.category}; owner: {info.owner}) — {info.title}\n\n"
        f"{info.explanation}\n\nHow to fix: {info.hint}"
    )
