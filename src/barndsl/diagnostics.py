"""Central registry of diagnostic codes.

Every code the compiler (``compiler.py``) or validator (``validation.py``) can
emit is catalogued here with its usual severity, a one-line title, and a longer
explanation — often citing the IRC clause behind a code check. The codes
themselves still live as string literals at each call site (so the emitting code
reads naturally); this module is the *documentation* surface and the source for
``barndsl explain <CODE>``.

Keeping a single table also makes the set of codes auditable: a test can assert
the registry covers every code the validator actually emits, so a new check
can't ship without a human-readable explanation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .validation import Severity


@dataclass(frozen=True)
class CodeInfo:
    """A documented diagnostic code."""

    code: str
    #: The severity this code is *usually* emitted at. A few codes vary by
    #: context (noted in ``explanation``): ``NO_ACCESS`` is an error for most
    #: rooms but a warning for a closet/pantry/loft; ``ENTRY_PRIVATE`` is a
    #: warning into a bath but an info into a bedroom.
    severity: Severity
    title: str
    explanation: str

    @property
    def varies(self) -> bool:
        return self.code in _VARYING


#: Codes whose severity depends on context (see :attr:`CodeInfo.severity`).
_VARYING = frozenset(
    {"NO_ACCESS", "ENTRY_PRIVATE", "DOOR_SWING", "DOOR_NO_LANDING", "AREA_VOID"}
)


def _c(code: str, severity: Severity, title: str, explanation: str) -> tuple[str, CodeInfo]:
    return code, CodeInfo(code, severity, title, explanation)


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
           "ordinary warnings and infos. Resolve the underlying problem."),
        _c("ACCEPT_UNKNOWN", W, "Accept pragma names an unknown code",
           "An `# barndsl: accept <CODE>` pragma named a code the registry doesn't "
           "know (a typo, or an old name). The pragma suppresses nothing. Use a "
           "real diagnostic code — the message lists did-you-mean candidates, and "
           "`barndsl explain` / the registry has the exact spellings."),
        _c("ACCEPT_UNUSED", I, "Accept pragma matched nothing",
           "An `# barndsl: accept <CODE>` pragma fired on nothing — the code never "
           "appeared on the line it targets (a trailing pragma's own line, or the "
           "statement following a standalone one), or the standalone pragma had no "
           "following statement. A stale pragma outlives the diagnostic it once "
           "waived; remove it so the audit trail stays honest."),
        # --- lexer / parser (always errors) ---------------------------------
        _c("UNTERMINATED_STRING", E, "Unterminated string literal",
           "A quoted value has no closing '\"' before the end of the line."),
        _c("SYNTAX", E, "Syntax error",
           "A required keyword or token is missing or out of place. The caret "
           "points at where the parser expected something else."),
        _c("BAD_NUMBER", E, "Malformed number",
           "A field that expects a plain number in feet got a non-number, a "
           "quoted value, or a non-finite value (nan/inf). Write measurements "
           "as bare feet, e.g. `10.5`."),
        _c("BAD_LEVEL", E, "Invalid floor level",
           "`level`/`from`/`to` must be a whole number >= 0 (0 = ground)."),
        _c("BAD_COUNT", E, "Invalid count",
           "A `program` bed/bath count must be a whole number >= 0."),
        _c("EMPTY_ID", E, "Empty identifier",
           "An id or name slot received an empty string. Give it a non-empty name."),
        _c("BAD_TYPE", E, "Unknown room type",
           "The room type isn't one of the known kinds (living, kitchen, "
           "bedroom, bathroom, …)."),
        _c("BAD_WALL", E, "Unknown wall",
           "A wall must be one of north / south / east / west."),
        _c("EXTRA_TOKENS", E, "Unexpected trailing tokens",
           "The statement parsed fully but extra tokens remain on the line."),
        _c("BAD_PLACEMENT", E, "Invalid placement",
           "A room needs `at <x>,<y>` or a relative anchor (east-of/west-of/"
           "north-of/south-of). At most one anchor per axis."),
        _c("PLACE_REF", E, "Bad placement reference",
           "A relative anchor points at a room that isn't defined yet, or a room "
           "is placed relative to itself. Define the reference room first."),
        _c("BAD_OPTION", E, "Unknown option",
           "An option keyword isn't valid for this statement (e.g. an unknown "
           "`entry`/`window` option)."),
        _c("UNKNOWN_STMT", E, "Unknown statement",
           "The line doesn't start with a known statement keyword."),
        # --- cross-file composition (the `use` statement + part files) ------
        _c("USE_UNRESOLVED", E, "Part path can't be resolved",
           "A `use \"<relpath>\"` names a part file that can't be resolved: the "
           "path is missing, absolute, escapes the including file's directory "
           "(`..`/symlink), exceeds the size limit, or the source has no home "
           "directory to resolve against (a pasted or browser-opened buffer). "
           "Paths are always relative to the including file — compile the file, "
           "or serve its folder, and keep parts under it."),
        _c("USE_ALIAS_DUP", E, "Duplicate use alias",
           "Two `use` statements share an `as <alias>`. Every id inside a part is "
           "stamped `<alias>.<id>`, so aliases must be unique across the plan — "
           "give each instance its own (`m`, `m2`, `bath_1`, ...)."),
        _c("USE_NESTED", E, "Use nested deeper than 2",
           "A part used by a part tries to `use` a third part. Composition is "
           "depth-2 (host → part → part); a `use` reaching depth 3 is refused. "
           "Flatten the deepest level, or `use` it one level up."),
        _c("USE_CYCLE", E, "Part cycle",
           "A part `use`s itself, or two parts `use` each other (a → b → a). A part "
           "library is a tree, not a ring — break the loop. The cycle path is named "
           "in the message; the loader stops cleanly instead of recursing forever."),
        _c("USE_PART_INVALID", E, "Part fails to compile",
           "A used part file doesn't compile cleanly on its own (in fragment mode): "
           "it has one or more errors of its own. The part-internal diagnostics are "
           "reported once, anchored to the part file — fix the part, then re-use it."),
        _c("PART_HOST_STMT", E, "Host-only statement in a part",
           "A part file uses a statement that describes a whole building, not a "
           "reusable block — `envelope`, `plan`, `wing`, `ceiling`, `program`, "
           "`require`, `site`, `setback`, `building`, `street`, `orientation`, "
           "`roof`, `overhang`, `finish`, `frame`, or `electrical`. A part borrows "
           "the host's; size it by its rooms and drop the statement. (`use` and "
           "`stair` ARE allowed in a part — Phase 20 nested + multi-level parts.)"),
        _c("PARAM_UNKNOWN", E, "Unknown param name",
           "A bare name stands where a number is expected inside a part, but it "
           "isn't a declared `param`. Declare it (`param <name> = <number>`), or use "
           "a number. Params are numbers only in v1 — no arithmetic."),
        _c("PARAM_UNDECLARED", E, "Use sets an undeclared param",
           "A `use ... with <name>=<value>` names a param the part doesn't declare. "
           "Add `param <name> = <default>` to the part, or drop the pair — the "
           "part's declared params are listed in the message."),
        _c("PARAM_DUP", E, "Param declared or set twice",
           "A `param <name>` is declared more than once in a part, or a `with` "
           "clause sets the same param twice. Declare/set each param once."),
        _c("PARAM_IN_PLAN", E, "param in a whole plan",
           "`param` declares a *part* parameter — a whole plan (a `.barn` with a "
           "`plan` header) has no use-site to pass values from. Move `param` into a "
           "part file; the host passes values with `use ... with name=value`."),
        _c("PART_ORIGIN", I, "Part origin normalized",
           "A part's south-west-most corner wasn't at 0,0, so the loader shifted "
           "the whole part to the origin before stamping (the `at` on the `use` "
           "line then places that corner). Harmless — parts are authored in their "
           "own local feet and needn't start at 0,0."),
        _c("PART_EMPTY", E, "Part declares no rooms",
           "A part file (any `.barn` file with no `plan` header) must declare at "
           "least one `room`. An empty part composes nothing."),
        # --- envelope / wings / top level -----------------------------------
        _c("ENVELOPE", E, "Bad envelope",
           "The envelope must have positive width and length, e.g. `envelope 60 x 40`."),
        _c("DIM_IMPLAUSIBLE", E, "Implausible dimension",
           "An envelope, room, or wing side is non-finite or larger than the "
           "1000 ft plausibility limit — no barndominium runs that far, and such a "
           "value overflows the area takeoff to `inf`. Almost always a typo (a "
           "stray digit, or feet entered as inches × something). Use a realistic "
           "measurement in feet; the value is clamped so the rest of the report "
           "still reads, but the plan stays unbuildable until it is fixed."),
        _c("WING_SIZE", E, "Bad wing size",
           "A `wing` block must have positive dimensions."),
        _c("FOOTPRINT_SPLIT", E, "Disconnected footprint",
           "A wing doesn't share a wall with the rest of the building; a corner "
           "touch isn't enough. The footprint must be one connected shape."),
        _c("CEILING", E, "Ceiling too low",
           "Habitable space needs a ceiling of at least 7 ft (9–12 is typical)."),
        _c("EMPTY", E, "Empty plan",
           "The plan has no rooms."),
        _c("DUP_ID", E, "Duplicate room id",
           "Two rooms share an id; ids must be unique."),
        _c("NO_BATH", W, "No bathroom",
           "The plan has no bathroom or half-bath."),
        _c("NOTE_OUTSIDE", I, "Positioned note outside the footprint",
           "A `note \"...\" at <x>,<y>` is anchored outside the building footprint "
           "(envelope + wings). Often intentional — annotating the site, a setback, "
           "or a future addition — so it's only a gentle nudge, never an error: if "
           "the callout means to sit on the plan, move its `at` point inside the "
           "walls."),
        _c("FLOOR_FINISH", W, "Unrecognised floor finish",
           "A room's `floor \"...\"` hint matched no material in the 3D palette, "
           "so it falls back to the default finish for its room type. Not "
           "blocking — pick a recognised name (tile, concrete, wood/plank, "
           "carpet, ...) to control the 3D floor material."),
        # --- site / setbacks (the `site` / `setback` statements) -------------
        _c("SETBACK", E, "Footprint violates the setbacks",
           "The building footprint (envelope + wings + porches) doesn't fit "
           "inside the buildable rectangle — the lot (`site`) minus its yard "
           "setbacks. `front` and `rear` consume the plan's north-south depth "
           "(front along the south/entry edge); `side` clears both the east and "
           "west edges. A dimensions-only check — barndsl has no lot-position "
           "statement — so it compares the footprint's bounding box against the "
           "buildable width and length. Shrink the footprint, enlarge the lot, or "
           "reduce the setbacks. Not a substitute for a survey/site plan."),
        _c("SITE", E, "Invalid site declaration",
           "The `site` lot dimensions are zero/negative/non-finite, or a "
           "`setback` value is negative. Declare a real lot: positive "
           "dimensions, non-negative setbacks."),
        _c("SETBACK_NO_SITE", E, "Setback without a site",
           "A `setback` statement declares yard setbacks but no `site <W> x <L>` "
           "gives the lot dimensions to measure them against. Add a `site` line, "
           "or drop the setbacks."),
        _c("SITE_REQUIRED", E, "Site feature without a site",
           "A `drive`/`walk`/`well`/`septic`/`service` places itself in lot feet, "
           "so it needs a `site <W> x <L>` to sit on. Declare the lot dimensions, "
           "or remove the site feature. (`grade` is the exception — it describes "
           "the building's height above grade and needs no lot.)"),
        _c("SITE_REF", E, "Walk names an unknown room",
           "A `walk from <room> to drive` names a room that doesn't exist. The walk "
           "starts at that room's exterior door, so it must be a real room with an "
           "exterior door. Name an existing entry room."),
        _c("WELL_SEPTIC_CLEAR", W, "Well too close to the septic",
           "A private well sits closer to the septic tank/drain field than the "
           "common 100 ft health-department separation. This is a public-health "
           "rule (not IRC) and varies by jurisdiction (50-100 ft is typical). Move "
           "the well or septic apart, and confirm the figure with the county health "
           "department."),
        _c("DRIVE_DOOR", I, "Drive with no path to a door",
           "The plan has a driveway but neither a `walk` nor a drive edge comes "
           "within a few feet of any exterior door — guests park and have no path "
           "to an entry. Add `walk from <room> to drive`, or extend the drive to a "
           "door."),
        _c("SEPTIC_SETBACK", I, "Septic inside a setback",
           "A septic tank or its drain field falls inside a required yard setback "
           "band. Septic components are usually held out of the setbacks too; "
           "confirm the allowed septic setback with the county health department."),
        _c("SITE_OVERLAP", W, "Site features of the same kind overlap",
           "Two declared driveways overlap on the lot. The cost takeoff sums each "
           "drive's area independently, so an overlap double-counts the shared "
           "paving in the estimate (and the drawing paints it twice). Only drives "
           "are checked: a `walk` is auto-routed to terminate at a drive, so a "
           "walk-drive overlap is by design and a walk-walk overlap of two thin "
           "auto-routed paths isn't a meaningful double-count. Merge or separate "
           "the overlapping drives so each patch of paving is declared once."),
        _c("PORCH_GUARD", W, "Porch needs a guard (R312.1)",
           "The declared `grade` puts the finish floor more than 30 in above "
           "finished grade, so every porch is a walking surface that needs a 36 in "
           "guard (IRC R312.1) with balusters blocking a 4 in sphere. Note the "
           "guard on the drawings. Silent when no `grade` is declared or the floor "
           "sits <= 30 in above grade."),
        _c("RECOVERY_LIMIT", W, "Partial-plan checks incomplete",
           "Parse-error recovery kept a partial plan, but frame placement or "
           "validation crashed on it and was skipped - the diagnostics listed "
           "are incomplete. Fix the parse error(s) to get the full report."),
        # --- geometry -------------------------------------------------------
        _c("ROOM_GEOMETRY", E, "Non-finite room geometry",
           "A room has nan/inf coordinates or size."),
        _c("ROOM_SIZE", E, "Non-positive room size",
           "A room's width or length is <= 0."),
        _c("OUT_OF_BOUNDS", E, "Room outside the footprint",
           "A room extends past the envelope (or, with wings, outside the "
           "footprint union)."),
        _c("OVERLAP", E, "Rooms overlap",
           "Two rooms on the same level intersect. Reposition so they only abut."),
        _c("AREA_OVERFLOW", W, "Assigned area exceeds footprint",
           "The level-0 room area sums to more than the footprint — rooms likely "
           "overlap or the envelope is too small."),
        _c("AREA_UNUSED", I, "Footprint under-used",
           "A large share of the footprint isn't assigned to any room."),
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
           "grow a neighbour over it, or trim the envelope."),
        # --- room programs --------------------------------------------------
        _c("BEDROOM_AREA", E, "Bedroom too small",
           "A bedroom is below the ~70 sq ft IRC minimum habitable area (R304)."),
        _c("BEDROOM_DIM", E, "Bedroom too narrow",
           "A bedroom's smallest dimension is below the 7 ft minimum (R304)."),
        _c("ROOM_HABITABLE", W, "Habitable room below the R304 minimum",
           "A habitable room (living, dining, office/den, loft) is below the IRC "
           "R304 minimum — 70 sq ft of floor area (R304.1) and 7 ft in every "
           "horizontal dimension (R304.2). Bedrooms carry the same rule as a hard "
           "error via BEDROOM_AREA/BEDROOM_DIM (not repeated here); a kitchen is "
           "exempt from both (R304.2). The thresholds follow the active profile's "
           "habitable-room minimums."),
        _c("HALL_WIDTH", E, "Hallway too narrow",
           "A hallway is below the 3 ft (36 in) minimum width (R311.6)."),
        _c("ROOM_TIGHT", I, "Room below a workable size",
           "A room is smaller than the usable floor its function needs — by area "
           "(kitchen ~70, full bath ~48, half bath ~30 sq ft) or by shortest side "
           "(full bath >= 6 ft, half bath >= 5 ft, so the fixtures fit across it). "
           "Bedrooms are covered by BEDROOM_AREA."),
        _c("BATH_CLEARANCE", W, "Bathroom can't fit its fixtures",
           "A bathroom's clear (finish-face) interior can't hold its fixtures with "
           "code clearances — a water closet needs 15 in from its centreline to any "
           "wall/fixture and 21 in of clear floor in front (IRC R307.1), and a full "
           "bath needs a 5 ft wall for the tub. Enlarge the room so toilet, lavatory "
           "and tub/shower fit."),
        _c("KITCHEN_FIT", I, "Kitchen tight for its appliances",
           "A kitchen's clear interior is too small to hold a sink, range and "
           "refrigerator along the counters with a comfortable ~40 in working aisle. "
           "Enlarge it or lengthen the counter run."),
        _c("BED_CLEARANCE", I, "Bedroom too tight to furnish",
           "A bedroom clears its area and 7 ft nominal dimension but its clear "
           "(finish-face) shape still can't hold a queen bed (5×6.67) against a wall "
           "with a ~24 in walk-around — needs about 7×6.67 ft clear. Catches the "
           "narrow room that passes the area/dimension checks but not the layout; "
           "the livability companion to the wet-room fixture checks."),
        _c("DINING_CLEARANCE", I, "Dining room too tight to furnish",
           "A dining room's clear interior is too small to seat a 4-person table "
           "(~3 ft) with ~30 in of chair-pull and circulation all round (about 8 ft "
           "clear each way). Enlarge it."),
        _c("ACCESS_ENTRY", I, "No-step entrance (accessible target)",
           "An accessible plan needs at least one no-step entrance (threshold ≤ ½ in) "
           "with a level landing (ANSI A117.1). Thresholds aren't in the geometry, so "
           "this is a reminder. Only emitted when the plan opts in via `accessible`."),
        _c("ACCESS_DOOR", I, "Door too narrow for an accessible route",
           "A door/opening on the living route is below the ~32 in clear width an "
           "accessible route needs (a ~34 in leaf; a 36 in exterior door) — ANSI "
           "A117.1 §404. Only emitted when the plan opts in via `accessible`."),
        _c("ACCESS_BATH", I, "Bath lacks a wheelchair turning space",
           "A ground-floor bath's clear short side is under the 60 in wheelchair "
           "turning circle (ANSI A117.1 §304); plan a roll-in shower and grab-bar "
           "blocking too. Only emitted when the plan opts in via `accessible`."),
        _c("ACCESS_SINGLE_FLOOR", I, "No single-floor living",
           "Accessible / aging-in-place living wants a bedroom and a full bath on the "
           "one no-stair entry level; the entry level is missing one. Only emitted "
           "when the plan opts in via `accessible`."),
        _c("ROOM_CLEAR", I, "Clear dimension falls short once walls are built",
           "A room meets a code minimum on its nominal (centreline) rectangle but "
           "falls below it once the bounding walls' thickness is subtracted. IRC "
           "habitability minimums (R304 area/width, R311.6 hall width) are measured "
           "between finished surfaces, and Revit's room schedule reports that same "
           "clear area — so a plan can compile clean yet build short. Grow the room "
           "by roughly a wall thickness so the clear dimension still meets the "
           "minimum."),
        # --- fixtures & furnishings (the `fixture` statement) ----------------
        _c("FIXTURE_ROOM", E, "Fixture references unknown room",
           "A `fixture` statement places a fixture `in` a room id that doesn't "
           "exist — a mistyped id would otherwise place nothing. Reference a room "
           "that's defined."),
        _c("FIXTURE_OOB", W, "Fixture outside the room",
           "An author-placed fixture's footprint extends past its room's clear "
           "(finish-face) interior. Its `at` is room-local feet from the SW corner; "
           "move it inward or grow the room. Auto-seeded fixtures are fitted "
           "automatically, so only explicit `fixture` placements are checked."),
        _c("FIXTURE_OVERLAP", W, "Fixtures overlap",
           "Two fixtures occupy the same floor — an author-placed one overlaps "
           "another fixture (a seed or another placement). Slide one along its "
           "wall, or back it to a different wall."),
        _c("FIXTURE_DOOR", I, "Fixture blocks a door swing",
           "An author-placed fixture sits in the clear floor a hinged door swings "
           "through, so the door can't fully open past it. Keep the swing clear — "
           "slide the fixture off the door approach, or swing the door the other "
           "way."),
        _c("FIXTURE_TOILET_CLEARANCE", W, "Toilet clearance below IRC R307.1",
           "An author-placed toilet has less than 15 in from its centreline to the "
           "nearest side wall or fixture, or less than 21 in of clear floor in "
           "front (IRC R307.1). Give the water closet a 30 in bay (15 in each side "
           "of centre) and 21 in in front — slide it along the wall or widen the "
           "room. Only explicit `fixture toilet` placements are checked; the "
           "auto-seed is fitted for you."),
        _c("FIXTURE_FRONT", I, "Fixture's clear-floor strip is blocked",
           "An author-placed fixture's approach — the clear floor it needs in front "
           "of its face (the `front` clearance in the catalog) — is cut off by a "
           "wall or overlapped by another fixture, so you can't use or stand at it. "
           "A free-standing piece (table, island) instead wants ~2 ft of walkway on "
           "at least one long side. Slide it clear, or move it toward the centre."),
        _c("FIXTURE_ROOM_TYPE", I, "Fixture in an unusual room type",
           "An author-placed fixture sits in a room type it isn't usually found in "
           "— a toilet/tub/shower/lavatory outside a bath or laundry, a "
           "range/refrigerator/island outside a kitchen, a bed outside a bedroom or "
           "loft. Often a typo in the room id or type; if it's deliberate, ignore "
           "the note. Storage/utility placements (a water heater in a utility, a "
           "washer in a mudroom, a desk anywhere) are never flagged."),
        _c("FIXTURE_BACKING", I, "Wall-backed fixture floats mid-floor",
           "An author-placed piece that normally backs to a wall (toilet, vanity, "
           "tub, shower, sink, range, fridge, counter, wardrobe, dresser, water "
           "heater, washer, dryer) sits more than ~0.5 ft off every wall of its "
           "room. Back it to a wall with `at`, or drop the coordinates and give it "
           "`wall N|S|E|W` to auto-place against a wall."),
        _c("FIXTURE_EGRESS", W, "Fixture blocks a bedroom escape window",
           "A tall author-placed piece (refrigerator, wardrobe, water heater) parks "
           "over a bedroom's emergency-escape window, so no one could get out "
           "through it (IRC R310). Keep the egress window clear — move the fixture "
           "to another wall."),
        _c("COUNTER_DOOR", W, "Counter run crosses a doorway",
           "A `fixture counter ... along <wall>` run spans a doorway, opening or "
           "entry on that wall — you can't build countertop across an opening. Stop "
           "the run short of the opening with `from <a> to <b>` (room-local feet "
           "along the wall), or move the run to another wall. A full-wall run over a "
           "door is the usual cause; a broken L/U leg clears it."),
        _c("COUNTER_ROOM", I, "Counter in an unusual room type",
           "A counter run sits in a room type where a run of casework reads as odd "
           "(a bedroom, closet, hallway or loft). Counters belong to a kitchen, "
           "pantry, bath, laundry, mudroom or shop; if this is a deliberate bar or "
           "work ledge, ignore the note."),
        _c("SINK_NO_COUNTER", I, "Kitchen sink not set into a counter",
           "A kitchen sink isn't set into any counter run — a sink wants countertop "
           "to each side to work at. Extend a `fixture counter ... along <wall>` run "
           "past the sink so it drops into the countertop. Only fires once the "
           "kitchen has counters placed to compare against; a bath lavatory (its own "
           "vanity) is never judged."),
        _c("RANGE_WINDOW", W, "Range under an operable window",
           "A range/cooktop sits directly under a window that opens. A draft "
           "through the sash can blow out a burner, and a curtain hangs over the "
           "flame — common code and NKBA practice keep a cooktop out from under an "
           "openable window. Slide the range along the wall, clear of the sash "
           "(a fixed, non-opening window overhead is fine)."),
        _c("RANGE_LANDING", I, "No landing surface beside the range",
           "A range has no counter, sink, island or refrigerator within 1 ft to "
           "either side to set a hot pan down (NKBA wants a landing surface flanking "
           "the cooktop). Add a `fixture counter` next to the range. Only fires when "
           "the kitchen already has other casework placed to compare against."),
        _c("KITCHEN_TRIANGLE", I, "Kitchen work triangle too spread out",
           "The sink–range–refrigerator work triangle (centre to centre) sums to "
           "more than ~26 ft, so the cook walks marathons between the three "
           "stations (NKBA keeps each leg ~4–9 ft, the perimeter ~13–26 ft). Draw "
           "the three appliances closer together. A compact, efficient galley is "
           "not flagged; a genuinely cramped kitchen is caught by KITCHEN_FIT."),
        _c("DRYER_VENT", I, "Dryer far from an exterior wall",
           "A dryer is more than ~10 ft from any exterior wall of its room (or its "
           "room has none), so the exhaust duct runs long and bendy — lint collects "
           "and airflow drops, a fire risk and an efficiency loss. Put the laundry "
           "on an exterior wall, or keep the dryer near one."),
        _c("WATER_HEATER_PLACEMENT", I, "Water heater placement needs protection",
           "A `water_heater` fixture sits somewhere its installation needs extra "
           "protection the DSL can't draw. In a garage or shop, a fuel-fired or "
           "electric water heater's ignition source must be elevated 18 in above "
           "the floor (or be a listed flammable-vapour-ignition-resistant unit), "
           "IRC M1307.3. On an upper floor (level 1+) over habitable space, it "
           "needs a drain pan piped to an approved drain so a leak doesn't soak the "
           "ceiling below, IRC P2801.6. A one-per-heater INFO naming which case "
           "applies — carry the detail onto the plumbing/mechanical documents."),
        _c("FIXTURE_STAIR", W, "Fixture on a stair footprint",
           "A fixture's footprint overlaps a stair's run or landing on the same "
           "level, so it fouls the flight. Keep the stair and its landing clear — "
           "slide the fixture off the footprint, or run the stair along a wall."),
        # --- doors ----------------------------------------------------------
        _c("SELF_DOOR", E, "Door to self",
           "An interior door connects a room to itself."),
        _c("DOOR_REF", E, "Door references unknown room",
           "A door/entry names a room id that doesn't exist."),
        _c("DOOR_NOADJ", E, "Door between non-adjacent rooms",
           "A door joins two rooms that don't share a wall (they may only touch "
           "at a corner), or a cross-level door whose footprints don't overlap."),
        _c("DOOR_FIT", W, "Door wider than the wall",
           "A door is wider than the shared wall it sits on."),
        _c("DOOR_OOB", E, "Door runs off the shared wall",
           "A positioned interior door's offset+width exceeds the shared wall it "
           "sits on. Keep offset >= 0 and offset + width <= the shared length."),
        _c("DOOR_SWING", W, "Door swing problem",
           "Error: a door's `into` names a room it doesn't connect. Warning: the "
           "leaf can't fully open because the room it swings into is shallower "
           "than the door is wide — swing it the other way or narrow it."),
        _c("DOOR_BLOCKS_HALL", I, "Door swings into a hallway",
           "A door swings into a hallway and, open, its leaf leaves under a 3 ft "
           "passage beside it — it blocks circulation. Swing it into the room."),
        _c("DOOR_HITS_FIXTURE", W, "Door swing crowds out a fixture",
           "A door's swing arc eats the wall run a wet room or kitchen needs to "
           "place a fixture (toilet, tub, lavatory, sink, range, refrigerator) that "
           "the room otherwise has the floor area for — keeping the leaf's path "
           "clear drops it. This is the gap BATH_CLEARANCE (capacity only, "
           "door-blind) can't see. Move the door along the wall, swing it the other "
           "way (`into <room>` / `hinge near|far`), make it a pocket/sliding door, "
           "or enlarge the room."),
        _c("DOOR_SWING_INWARD", W, "Exterior door can't clear inward",
           "An exterior door swings inward (the residential default), but the room "
           "is too shallow for its leaf to fully open. Deepen the room, narrow the "
           "door, or use an out-swing or sliding door."),
        _c("DOOR_SWING_PRIVACY", I, "Private-room door swings out",
           "A bedroom or bathroom door is set to swing out into circulation. It "
           "should open into the private room so the leaf screens the view on entry "
           "and folds flat against a wall rather than sweeping the corridor."),
        _c("DOOR_SWING_UNSET", I, "Private-room door swing not pinned",
           "A bedroom or bathroom door has no `into` direction and its default "
           "geometric swing opens out of the room. Pin it with `into <room>` so it "
           "reliably opens into the private space (and stays that way if the layout "
           "shifts)."),
        _c("DOOR_NARROW", W, "Door too narrow",
           "A swinging interior door is below the 30 in minimum clear width."),
        _c("DOOR_SIZE", I, "Non-standard door width",
           "A swing door's width isn't a manufactured leaf size (interior "
           "24/28/30/32/36 in; exterior 30/32/36, doubles 60/72), a declared "
           "double/french pair isn't a stock pair width (48/60/64/72 in total), "
           "or an overhead door isn't a stock sectional size (widths 8/9/10/12/16 "
           "ft, heights 7/8 ft). Snap it to the nearest so it's orderable "
           "off-the-shelf."),
        _c("OVERHEAD_ROOM", I, "Overhead door in a living space",
           "An overhead (sectional garage) door is on a room that isn't a garage "
           "or shop — unusual for a living space. Either the room should be a "
           "garage/shop bay, or the door should be a people-door (`entry`)."),
        _c("OVERHEAD_HEADER", I, "Wide overhead opening needs an engineered header",
           "An overhead door wider than 10 ft (a 12 or 16 ft double) spans more "
           "than a stock header carries — the header and the jamb posts over the "
           "opening must be engineered with the building frame."),
        _c("OPEN_BATH", W, "Bathroom has no door",
           "A bathroom is joined by an `open` passage; baths need a door for privacy."),
        # --- openings -------------------------------------------------------
        _c("WINDOW_REF", E, "Window references unknown room",
           "A window names a room id that doesn't exist."),
        _c("OPENING_SIZE", E, "Non-positive opening width",
           "A window, interior door, or exterior door (including an overhead "
           "door) was declared with `width <= 0`. A zero- or negative-width "
           "opening isn't a buildable opening, and it misprices in the estimate "
           "(an overhead line vanishes; an entry still bills a full leaf). Give "
           "it a positive width, e.g. `width 3`."),
        _c("OPENING_OOB", E, "Opening runs off the wall",
           "A window/entry's offset+width exceeds the wall it sits on."),
        _c("OPENING_CLASH", E, "Openings overlap",
           "Two openings (window/entry) overlap on the same wall span — they "
           "can't both physically occupy that run of wall."),
        _c("WINDOW_INTERIOR", W, "Window on an interior wall",
           "A window is on a wall that isn't on the building envelope, so it "
           "provides no daylight or egress."),
        _c("WINDOW_SILL", W, "Window head at or below its sill",
           "A window's head height is not above its sill height, so it encloses "
           "no glazed area. Set `head` above `sill` (both are ft above the floor)."),
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
           "the schedule."),
        _c("WINDOW_FALL", W, "Operable window needs fall protection",
           "An operable window has a sill below 24 in on an upper storey. IRC "
           "R312.2 requires window fall protection (an opening-control device or "
           "fall guard, ASTM F2090) where an operable sash sits below 24 in and "
           "more than 72 in above the grade below. The model carries no grade "
           "elevation, so an upper level (>= 1) is the proxy for 'well above "
           "grade'. Fit an opening-control device that limits the sash to a 4 in "
           "clear opening yet still releases for escape — do not raise the sill, "
           "which would fight the R310 egress-window rule. A fixed sash is exempt."),
        _c("ENTRY_INTERIOR", E, "Entry on an interior wall",
           "An exterior door is on a wall that doesn't face outside."),
        _c("DOOR_THRESHOLD", I, "Threshold-to-landing drop at the egress door",
           "A reminder-class info (the model has no vertical threshold data, so it "
           "teaches rather than measures): at the required egress door the exterior "
           "landing may be no more than 1.5 in below the top of the threshold — "
           "7.75 in only where the door does not swing out over the landing (IRC "
           "R311.3.1). Nudged once, on the primary entry, and only before a "
           "porch/landing is modelled (mirroring DOOR_NO_LANDING's single info); "
           "once landings are drawn, the CD set carries the detail. Confirm the "
           "landing-to-threshold drop on the construction documents."),
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
           "`porch` at the door, or note the landing on the construction documents."),
        # --- stairs ---------------------------------------------------------
        _c("STAIR_GEOMETRY", E, "Non-finite stair geometry",
           "A stair has nan/inf coordinates or size."),
        _c("STAIR_SIZE", E, "Non-positive stair size",
           "A stair's width or length is <= 0."),
        _c("STAIR_LEVELS", E, "Bad stair levels",
           "A stair must connect two different levels >= 0, e.g. `from 0 to 1`."),
        _c("STAIR_OOB", E, "Stair outside the envelope",
           "A stair's footprint extends past the envelope."),
        _c("STAIR_FLOAT", W, "Stair lands in no room",
           "A stair doesn't overlap a room on one of the levels it connects."),
        _c("STAIR_RUN", W, "Stair footprint too small",
           "A stair's footprint is too short to physically hold the run the "
           "ceiling height requires (R311.7: ~7.75 in max riser, 10 in min "
           "tread). Lengthen its footprint or model a switchback."),
        _c("STAIR_HANDRAIL", I, "Stair flight needs a handrail",
           "A stair flight of four or more risers requires at least one handrail, "
           "34–38 in above the tread nosings and graspable the full length (IRC "
           "R311.7.8). The DSL doesn't model railings, so this is a one-per-plan "
           "checklist reminder on the first qualifying stair — carry the handrail "
           "onto the construction documents. Fewer than four risers is exempt."),
        _c("STAIR_HEADROOM", W, "Stair headroom can't develop",
           "A stair's footprint is too short for a floor opening (stairwell) that "
           "keeps 6 ft 8 in of headroom under the upper floor (IRC R311.7.2). "
           "Lengthen the run/opening or reduce the floor-to-floor height."),
        _c("STAIR_LANDING", I, "Flight too tall — needs an intermediate landing",
           "A single straight flight climbs more than 12 ft 7 in (151 in) of "
           "vertical rise. IRC R311.7.3 limits a flight to that rise between floor "
           "levels or landings, so a taller run needs an intermediate landing (a "
           "switchback or L-turn). Risers are computed the same way as STAIR_RUN "
           "(rise / max riser), so a gentler profile riser is reflected in the "
           "quoted count. A normal one-storey flight stays well under, so this only "
           "speaks up on a tall or multi-level run. INFO — the DSL models one "
           "straight flight, so note the mid-run landing on the construction "
           "documents."),
        # --- guards & life safety -------------------------------------------
        _c("LOFT_GUARD", I, "Open loft edge needs a guard",
           "An upper-level room only partially covers a room below, so it "
           "overlooks a double-height void. Its open edge is a walking surface "
           "more than 30 in up and needs a 36 in guard with balusters that block a "
           "4 in sphere (IRC R312)."),
        _c("ALARM_CO", I, "Smoke/CO alarms",
           "Two roles, both info. On a plan that has bedrooms but declares NO "
           "`alarm`, a teaching reminder to place smoke/CO alarms (IRC R314/R315). "
           "Once alarms ARE declared, the carbon-monoxide check: bedrooms coexist "
           "with an attached garage/shop but no `co`/`smoke_co` alarm sits outside "
           "the sleeping areas (IRC R315). INFO — fuel-fired appliances aren't "
           "modelled, so the attached garage/shop is the only trigger."),
        _c("ALARM_BEDROOM", W, "Bedroom has no smoke alarm",
           "A bedroom carries no `smoke` (or `smoke_co`) alarm. IRC R314.3 "
           "requires a smoke alarm in each sleeping room. Runs only once a plan "
           "declares any `alarm`. Add `alarm smoke in <bed>`."),
        _c("ALARM_HALL", W, "No smoke alarm outside a sleeping area",
           "No room adjacent to a bedroom carries a smoke alarm. IRC R314.3 wants "
           "a smoke alarm outside each sleeping area; this approximates 'outside' "
           "as a room sharing a door with the bedroom (a hallway if present, else "
           "any adjacent room). Add `alarm smoke in <hall>`."),
        _c("ALARM_LEVEL", W, "Level has no smoke alarm",
           "A storey of the dwelling carries no smoke alarm. IRC R314.3(3) "
           "requires at least one on every level, including basements. Add "
           "`alarm smoke in <room on that level>`."),
        _c("ELECTRICAL_PLAN", I, "Electrical / life-safety checklist",
           "An opt-in reminder (the `electrical` directive) for code requirements "
           "the DSL can't place from geometry: receptacle spacing (no wall point "
           ">6 ft from an outlet, IRC E3901.2) with GFCI/AFCI protection (E3902), "
           "switched lighting outlets at habitable rooms/halls/entries (R303.7 / "
           "E3903), stair lighting, and a level landing at each exterior door "
           "(R311.3). Carry these onto the construction documents. Once a room "
           "actually draws outlets, the sharper per-room checks (OUTLET_SPACING / "
           "OUTLET_GFCI / ROOM_NO_LIGHT) take over from this reminder."),
        _c("OUTLET_SPACING", W, "Receptacle spacing too wide",
           "A habitable room has drawn receptacles, but there is a point along its "
           "walls more than 6 ft from the nearest one — a lamp/appliance cord "
           "would have to cross a doorway to reach power. IRC E3901.2 requires "
           "receptacles so that no point along a wall line is more than 6 ft from "
           "one (i.e. one at least every 12 ft of wall run, measured around "
           "corners). Add an `outlet` in the worst gap. Only rooms that declare an "
           "outlet are checked (drawing the electrical layer is opt-in)."),
        _c("RECEPTACLE_COUNTER", W, "Kitchen counter needs a small-appliance receptacle",
           "A kitchen counter run (a `fixture counter ... along` run at least 12 in "
           "wide) leaves a point on the counter wall more than 24 in from a "
           "receptacle. IRC E3901.4 requires small-appliance receptacles spaced so "
           "no point along a counter is more than 24 in from one (receptacles at "
           "most 48 in apart, and one on every counter >= 12 in wide). Add an "
           "`outlet` on the counter wall in the gap. Only checked once a plan draws "
           "its electrical layer (an outlet/switch/light), like OUTLET_SPACING."),
        _c("OUTLET_GFCI", W, "Receptacle needs GFCI protection",
           "A receptacle in a kitchen, bathroom, laundry or utility (a wet/damp "
           "location) isn't marked `gfci`. IRC E3902 requires ground-fault "
           "circuit-interrupter protection for receptacles in those rooms (and "
           "outdoors). Add `gfci` to the `outlet`, or protect the circuit at the "
           "panel and note it."),
        _c("ROOM_NO_LIGHT", I, "Habitable room has power but no light",
           "A habitable room draws receptacles or switches but no lighting outlet. "
           "IRC E3903 requires at least one wall-switch-controlled lighting outlet "
           "in every habitable room (a switched receptacle counts). Add a `light` "
           "(or note a switched receptacle). Advisory — the switch is often there, "
           "just not drawn."),
        # --- access ---------------------------------------------------------
        _c("NO_ENTRY", E, "No exterior door",
           "The plan has no exterior people-door — no way to enter the building. "
           "An overhead garage door doesn't count (vehicle access, not an "
           "entrance), so a plan whose only exterior door is overhead still "
           "needs an `entry`."),
        _c("NO_ACCESS", E, "Room unreachable",
           "A room can't be reached from any entrance through interior doors. "
           "A warning (not error) for a closet/pantry/loft, which may be open "
           "or reached by stairs not yet modelled."),
        # --- egress & light -------------------------------------------------
        _c("BEDROOM_EGRESS", E, "Bedroom has no escape opening",
           "Every bedroom needs an emergency escape opening — a window that "
           "opens, or its own exterior door — on an exterior wall (IRC R310). "
           "A `fixed` window is glass that doesn't open: it daylights but is "
           "never an escape opening."),
        _c("EGRESS_SIZE", W, "Egress opening too small",
           "A bedroom's escape opening is below the IRC R310 minimums: ~5.7 sq "
           "ft net clear opening (5.0 at grade), >= 20 in clear width, >= 24 in "
           "clear height, sill <= 44 in above the floor. The clear opening "
           "follows the window kind: a casement clears ~its full glazed size, a "
           "slider ~half its glazed width, a double-hung ~half its glazed "
           "height; fixed glass never counts."),
        _c("EGRESS_DOOR", W, "No wide egress door",
           "No exterior egress door is at least 32 in clear wide (R311.2). A "
           "double/french pair provides its required clear width through ONE "
           "leaf, so it counts half its total width."),
        _c("NAT_LIGHT", W, "Insufficient natural light",
           "A habitable room's glazing on exterior walls is below 8% of floor "
           "area (R303.1)."),
        _c("VENT_AREA", W, "Insufficient natural ventilation",
           "A habitable room's OPENABLE window area on exterior walls is below 4% "
           "of its floor area (IRC R303.1's natural-ventilation floor, half the 8% "
           "glazing floor). A `fixed` window daylights but opens nothing, so it "
           "counts for NAT_LIGHT but not here. Make a window operable "
           "(casement/slider/double-hung), widen one, or confirm mechanical "
           "ventilation on the construction documents."),
        # --- solar orientation (advisory; needs a declared `orientation`) ---
        _c("SOLAR_WEST_GAIN", I, "Overheating west glazing",
           "A habitable room has a lot of west-facing glass. The low afternoon sun "
           "on a west wall is hard to shade and overheats the room. Shade it with a "
           "deep overhang/porch or awning, cut it back, or move it to the south "
           "face. Runs only when the plan declares an `orientation` (northern "
           "hemisphere)."),
        _c("SOLAR_NORTH_ONLY", I, "Room lit only from the north",
           "A living/dining/bedroom/kitchen is glazed only to the north — little "
           "direct sun, so it feels dim and cold in winter — while it has a sunnier "
           "(south/east/west) exterior wall to spare. Add a window on that wall. "
           "Offices are exempt (even north light is a valid studio choice). Runs "
           "only when the plan declares an `orientation`."),
        _c("SOLAR_SOUTH_UNUSED", I, "South wall left unglazed",
           "The plan has a substantial south-facing exterior wall but almost no "
           "south glazing — the best passive-solar face is nearly blank. South "
           "glass gives free low-angle winter sun that a summer-blocking overhang "
           "can shade. Runs only when the plan declares an `orientation`."),
        _c("SOLAR_SOUTH_NO_OVERHANG", I, "Unshaded south glazing",
           "The plan has a lot of south glazing but no roof `overhang` (and no "
           "covered porch over it) to shade it — the high summer sun overheats "
           "those rooms. A ~2 ft eave blocks the summer sun while still admitting "
           "the low winter sun. Runs only when the plan declares an `orientation`."),
        # --- thermal envelope (advisory; needs a declared `climate` zone) ---
        _c("ENERGY_ENVELOPE", I, "Envelope R-value guidance",
           "The prescriptive envelope targets (ceiling/wall/floor/slab R-values and "
           "window U-factor) for the plan's declared IECC `climate` zone, plus the "
           "steel-frame thermal-bridge note: insulate a metal shell with continuous "
           "exterior insulation, since steel studs short-circuit cavity insulation. "
           "Guidance, not the code of record — confirm with the adopted energy code."),
        _c("WINDOW_HEAVY", I, "High window-to-wall ratio",
           "Glazing exceeds ~28% of the gross exterior wall area — a high "
           "window-to-wall ratio that drives the heating/cooling load. The daylight "
           "floor (NAT_LIGHT, 8%) is the minimum; this is the practical ceiling. "
           "Concentrate glass on the south (winter gain) and shade it. Runs only "
           "when the plan declares a `climate` zone."),
        _c("APPROACH_ENTRY", I, "Front door doesn't face the street",
           "No people-door is on the wall the `street` directive names as facing "
           "the approach — the front door is around the side or back. Runs only "
           "when the plan declares a `street`."),
        _c("APPROACH_GARAGE", I, "Garage faces away from the street",
           "An overhead/garage door is on the wall opposite the `street` side, so a "
           "vehicle would have to drive around the house to reach it. Face it toward "
           "the approach or a side wall. Runs only when the plan declares a "
           "`street`."),
        # --- design quality (advisory) --------------------------------------
        _c("KITCHEN_FLOW", I, "Kitchen not open to living/dining",
           "An idiomatic barndo opens the kitchen to a dining or living area."),
        _c("BED_PRIVACY", I, "Bedroom opens onto a public room",
           "A bedroom opening straight onto living/kitchen/dining lacks privacy; "
           "buffer it with a hallway."),
        _c("BATH_DISTANCE", I, "Bedroom far from a bath",
           "A bedroom is more than two doors from any bathroom."),
        _c("BATH_VENT", I, "Windowless bathroom",
           "A bathroom has no exterior window, so it needs mechanical ventilation "
           "(IRC R303.3). The DSL can't model fans — confirm an exhaust fan."),
        _c("HALL_DEADEND", I, "Hallway dead end",
           "A hallway opens onto at most one room (a 1-room foyer is exempt), or it "
           "runs well past its last doorway into a blank wall — a dead-end stub. "
           "Trim it back to its last door, or put a room at the dead end."),
        _c("HALL_TIGHT", I, "Hallway tight",
           "A hallway meets the 3 ft code minimum but is under the 4 ft that's "
           "comfortable for two people or moving furniture."),
        _c("NO_BACK_DOOR", I, "Only one exterior door",
           "A home wants a front *and* a back door — a second exterior door (off "
           "the kitchen, mudroom or laundry, on another wall) for daily flow and a "
           "second way out. Garage/porch doors and overhead doors don't count."),
        _c("BATH_OVERSIZE", I, "Ensuite larger than its bedroom",
           "A private (ensuite) bath is larger than the bedroom it serves, a sign "
           "the suite is mis-proportioned. A bath should be the same size or smaller."),
        _c("STAIR_BLOCKS_DOOR", W, "Stair blocks a doorway",
           "A stair's footprint intrudes on the clear floor in front of a door, so "
           "you'd step off the stair straight into the doorway. Place the stair "
           "along a wall, clear of door approaches."),
        _c("STAIR_WALL", I, "Stair floats free of any wall",
           "A stair sits in the middle of a room rather than along an exterior or "
           "partition wall, where it would need railings all round and chops up the "
           "floor. (Mid-flight landings/turns aren't modelled.)"),
        _c("DOOR_CENTERED", I, "Door floats mid-wall",
           "A swing door is centred on a wall with usable wall on both flanks; "
           "backing it to a corner leaves one unbroken run to line with furniture. "
           "Only un-positioned swing leaves are flagged."),
        _c("WINDOW_PARTITION", I, "Window butts an interior wall",
           "A window sits against an interior partition where it meets the exterior "
           "wall — no room for framing/trim, and it reads off-balance. Pull it "
           "toward the wall centre or a true building corner; space windows evenly."),
        _c("DOOR_SWING_CLASH", I, "Door swings overlap",
           "Two door leaves sweep into the same space and would foul each other. "
           "Move one along its wall, narrow it, swing it the other way (`into` / "
           "`hinge`), or make one a pocket/sliding door."),
        _c("ENVELOPE_MODULE", I, "Exterior dimension off the build module",
           "An exterior (envelope or wing) measurement isn't a whole multiple of the "
           "3 ft build module. Rounding exterior dimensions to the module cuts sheet "
           "goods and framing with less waste."),
        _c("PRIVATE_PASSTHROUGH", W, "Routed through a private room",
           "A room is reachable only by passing through a bathroom or someone "
           "else's bedroom — a circulation defect."),
        _c("ENTRY_PRIVATE", W, "Entry into a private room",
           "An exterior entry opens into a bathroom (warning) or a bedroom "
           "(info — it might be a patio door)."),
        _c("WET_GROUP", I, "Scattered plumbing",
           "Three or more wet rooms (bath/kitchen/laundry/utility) share no "
           "walls, spreading plumbing runs out. A declared plumbing wall — "
           "`wall <bath> - <neighbour> plumbing` — that a wet room really backs "
           "onto also satisfies this: the wet wall exists, just shared with a "
           "dry room."),
        _c("PLUMBING_STACK", I, "Upper wet room not stacked",
           "An upper-floor wet room (bath/kitchen/laundry) sits over no wet room "
           "on the level below, so its waste stack can't drop straight down and "
           "must jog horizontally through the floor assembly and down through a "
           "dry room. Stack it over a wet room below (the cross-floor analogue of "
           "WET_GROUP)."),
        _c("CLOSET_SHAPE", I, "Long, skinny closet",
           "A closet has the floor area for a walk-in but is shaped as a narrow "
           "strip (>= 4:1). A more square footprint (under ~3:1, >= 4 ft deep) is "
           "a usable walk-in. Small reach-ins and wide/shallow closets are exempt."),
        _c("BED_SOUND", I, "Bedrooms share a party wall",
           "Two bedrooms share a wall directly, so sound carries between them. "
           "Stack each bedroom's closet on the shared wall (back-to-back) to buffer "
           "the sleeping rooms, or put a hall/closet between them."),
        _c("NO_CLOSET", I, "Bedroom has no usable closet",
           "A bedroom has no closet reached by a door from it — either none "
           "abuts it, or one abuts but with no door into it (e.g. a neighbour's "
           "closet)."),
        _c("LOW_STORAGE", I, "Storage-poor plan",
           "Dedicated storage (closets + pantry) is below a small fraction of the "
           "conditioned floor area — the whole-house storage the review flagged as "
           "invisible, now visible. Conservative floor (below the worked gallery), "
           "so it only catches a home with almost no closets. Declare a specific "
           "target with `program ... storage <sqft>`."),
        _c("MASTER_ENSUITE", I, "No private ensuite",
           "On a floor with two or more full bathrooms, no bedroom has a private "
           "(ensuite) bath — every bath is shared. The primary bedroom should get "
           "its own."),
        _c("ROOM_PROPORTION", I, "Awkwardly elongated room",
           "A habitable room is more than ~3:1 long-to-short and hard to furnish."),
        _c("GARAGE_BEDROOM", W, "Garage/shop opens into a bedroom",
           "A garage or shop must not open directly into a sleeping room (IRC "
           "R302.5.1). A barndominium shop bay is treated as a garage."),
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
           "the garage."),
        _c("GARAGE_NO_ENTRY", I, "Garage/shop has no people-door",
           "A garage or shop abuts the house but has no interior door into it."),
        _c("GARAGE_SEPARATION", I, "Garage/dwelling fire separation required",
           "A garage or shop shares a wall with conditioned space, or has habitable "
           "space above it. IRC R302.6 requires the common wall to be a fire "
           "separation (min ½ in gypsum) and, where a habitable room is above, the "
           "ceiling to be ⅝ in Type X gypsum. A barndominium shop bay is treated as "
           "a garage. Declaring the detailed wall — `wall <garage> - <room> rated` "
           "— records the separation and silences the reminder for that pair "
           "(verified, not just reminded); a ceiling can't be declared, so "
           "habitable space above keeps reminding."),
        _c("GARAGE_DOOR", I, "Garage/dwelling door must be self-closing & rated",
           "A door between a garage or shop and the dwelling must be self-closing "
           "and 20-minute fire-rated (or a solid-core/solid-wood door at least "
           "1-3/8 in thick) per IRC R302.5.1. The reminder anchors on the `door` "
           "statement itself — the opening that has to carry the rated leaf. A door "
           "into a sleeping room is barred outright (GARAGE_BEDROOM)."),
        _c("CLOSET_DOOR_SWING", I, "Swing door fills a shallow closet",
           "A swing door serves a closet shallower than the door is wide, so the "
           "leaf can't fully open inside it. There is no IRC rule here — it's a "
           "usability nudge. Make it a bypass/sliding or bifold door so the leaf "
           "doesn't fill the closet. Only a leaf swinging into the closet (or an "
           "unspecified side) is judged; one explicitly swinging into the room is "
           "fine."),
        _c("PROGRAM_MISMATCH", W, "Plan doesn't match its program",
           "The rooms placed don't match the declared `program`: exact bed/bath "
           "counts, an at-least requirement for another room type (e.g. "
           "`1 laundry`), or a minimum conditioned `area`."),
        _c("REQUIRE_UNMET", W, "Plan doesn't satisfy a `require` statement",
           "The compiled geometry doesn't satisfy a declared spatial requirement: "
           "`adjacent` needs a shared wall on the same level (purely geometric — "
           "a door or cased opening alone doesn't count; adjacency is what a "
           "`door` needs, so the checks agree), `separate` forbids one (rooms on "
           "different levels are trivially separate), `exterior` needs a wall on "
           "the footprint edge (optionally a specific side), and `area` sets a "
           "minimum nominal room area (the figure `program area` uses). Like "
           "PROGRAM_MISMATCH it's a contract check on intent, not a code "
           "violation, so it warns rather than blocks."),
        _c("REQUIRE_REF", E, "Requirement references unknown room",
           "A `require` statement names a room id that doesn't exist — a mistyped "
           "id would otherwise silently check nothing."),
        # --- declared wall attributes (the `wall` statement) ------------------
        _c("WALL_REF", E, "Wall statement references unknown room",
           "A `wall` statement names a room id that doesn't exist — a mistyped id "
           "would otherwise silently declare nothing."),
        _c("WALL_NOADJ", E, "Wall statement between non-adjacent rooms",
           "A `wall` statement declares attributes of the shared wall between two "
           "rooms, but the pair doesn't share one (a corner touch isn't enough, "
           "and rooms on different levels never share a wall) — the declared wall "
           "doesn't exist. Same geometry rule an interior `door` needs."),
        _c("WALL_UNUSED", I, "Plumbing wall serves no wet room",
           "A wall is declared `plumbing` (a 2x6 wet wall for supply/waste runs) "
           "but neither room flanking it is a bath, kitchen, laundry or utility — "
           "the declaration matches no fixtures. Put the wet wall where fixtures "
           "back onto it, or drop the attribute."),
        _c("WALL_BEARING_AXIS", I, "Bearing wall runs across the frame's span",
           "A wall declared `bearing` runs parallel to the frame's bents (across "
           "the span), so it can't carry an interior post line — post lines run "
           "along the building's long axis, splitting the bents' clear span. The "
           "frame ignored the declaration; declare a wall running the long way, "
           "or leave the span to the auto interior supports."),
        # --- suites / zones (the `suite` / `zone` statements) ---------------
        _c("SUITE_SHADOW", W, "Suite id shadows a room id",
           "A suite is named like an existing room, so a zone member with that "
           "name resolves to the room and the suite silently never expands. "
           "Rename the suite."),
        _c("SUITE_REF", E, "Suite references unknown room",
           "A `suite` statement lists a member room id that doesn't exist — a "
           "mistyped id would otherwise group nothing. Reference a real room, or "
           "declare it."),
        _c("SUITE_OVERLAP", W, "Room in more than one suite",
           "A room is declared a member of two different suites. A room belongs "
           "to one suite (a bedroom's own bath/closet), so this is almost always "
           "an authoring slip; drop it from all but one. A warning, not an error "
           "— the plan still builds — matching the other declared-intent checks."),
        _c("ZONE_REF", E, "Zone references unknown room or suite",
           "A `zone` statement lists a member that names neither a room nor a "
           "declared suite. Zone members are room ids or suite ids; reference an "
           "existing one, or declare it."),
        _c("ZONE_OVERLAP", W, "Room in more than one zone",
           "A room falls in two zones — directly, or because it is in a suite "
           "that a zone lists while another zone lists the room. Zones are "
           "mutually-exclusive bands (private wing, public core), so this is an "
           "authoring slip; keep each room in one zone."),
        _c("ZONE_CROSS", I, "Room crosses its zone's band",
           "A clearly public room (living/kitchen/dining) is the only such room "
           "in a zone that otherwise holds only private rooms (bed/bath), or the "
           "reverse — a public room stranded in the private band. A design nudge, "
           "not a rule: it fires only when the room is in exactly one zone and "
           "that zone is unambiguously the opposite band, so a mixed open-concept "
           "zone (or a plan with no zones) never triggers it."),
        # --- structural frame (the `frame` directive) -----------------------
        _c("POST_OBSTRUCT", I, "Support post in open floor",
           "An auto-placed interior support post (needed where the beam span "
           "exceeds the configured limit) lands out in a room's open floor rather "
           "than on a wall line. Align a partition/closet/island to it, or widen "
           "`span` so no interior support is needed."),
        _c("POST_IN_OPENING", W, "Post inside a window/door",
           "An auto-placed structural post coincides with a window or exterior "
           "door opening — you can't frame an opening through a column. Shift the "
           "opening along its wall into a clear bay (between posts), or change the "
           "`frame bay` spacing so no post lands on it. A post at the opening's "
           "jamb is fine (that's how it's framed); only a post inside it flags."),
        _c("LOAD_PATH", I, "Upper partition unsupported below",
           "An interior wall on an upper level lands over the open floor of a room "
           "below, with no wall, beam, or post beneath it — the floor framing must "
           "carry it (IRC R502). Fine for a light partition on adequate joists; a "
           "bearing wall wants direct support below."),
        _c("BAY_WIDE", I, "Wide frame bay spacing",
           "The frame's bay spacing is wider than typical residential post-frame "
           "(~12 ft on centre). Legal with adequately sized members, but it asks "
           "more of the beams and posts — lower `bay` or have them engineered."),
        # --- agent layer ----------------------------------------------------
        _c("DESIGN", I, "Architect's critique",
           "A design-quality suggestion folded in from the agent's architect review."),
        _c("TRUNCATED", I, "Generated reply was cut off",
           "The agent's generated plan hit the output token cap before it finished "
           "(on both the write and its retry), so the source may be incomplete. "
           "Folded into the feedback so the next revision is written more "
           "concisely."),
        _c("NO_PROGRAM", I, "No `program` statement",
           "The source declares no `program` line, so the compiler cannot check the "
           "plan delivers the brief's beds/baths/area. Derive one from the brief — "
           "`program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]`."),
        # --- Revit build log (`barndsl revit-log`) ---------------------------
        _c("REVIT_FAIL", W, "Element failed to build in Revit",
           "The pyRevit builder hit an API error creating this element (see the "
           "message for Revit's reason); the rest of the build carried on, so the "
           "model is missing it. Usually a template/family problem — check the "
           "build log's resources block for what was picked."),
        _c("REVIT_SKIP", W, "Element skipped by the Revit build",
           "The builder had nothing to build this element with — a missing level, "
           "host wall, family or type — so it is absent from the model. Load a "
           "matching family into the template or map the pass to a named type in "
           "the config.json sidecar."),
        _c("REVIT_NOTE", I, "Revit build used a stand-in",
           "The element built, but not the way the plan asked: a stand-in family, "
           "a flat fallback for a gable profile, or a type hint that matched "
           "nothing. The model is usable; refine the template (or the config "
           "mapping) to close the gap."),
    ]
)


def explain(code: str) -> str:
    """Return a human-readable explanation of ``code`` (case-insensitive)."""
    info = REGISTRY.get(code.strip().upper())
    if info is None:
        known = ", ".join(sorted(REGISTRY))
        return f"Unknown diagnostic code '{code}'.\nKnown codes: {known}"
    sev = info.severity.value + (" (context-dependent)" if info.varies else "")
    return f"{info.code} [{sev}] — {info.title}\n\n{info.explanation}"
