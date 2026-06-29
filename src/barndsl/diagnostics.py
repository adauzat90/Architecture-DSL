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
_VARYING = frozenset({"NO_ACCESS", "ENTRY_PRIVATE", "DOOR_SWING"})


def _c(code: str, severity: Severity, title: str, explanation: str) -> tuple[str, CodeInfo]:
    return code, CodeInfo(code, severity, title, explanation)


E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO

#: code -> CodeInfo. Grouped by emitting phase for readability.
REGISTRY: dict[str, CodeInfo] = dict(
    [
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
        # --- envelope / wings / top level -----------------------------------
        _c("ENVELOPE", E, "Bad envelope",
           "The envelope must have positive width and length, e.g. `envelope 60 x 40`."),
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
        # --- room programs --------------------------------------------------
        _c("BEDROOM_AREA", E, "Bedroom too small",
           "A bedroom is below the ~70 sq ft IRC minimum habitable area (R304)."),
        _c("BEDROOM_DIM", E, "Bedroom too narrow",
           "A bedroom's smallest dimension is below the 7 ft minimum (R304)."),
        _c("HALL_WIDTH", E, "Hallway too narrow",
           "A hallway is below the 3 ft (36 in) minimum width (R311.6)."),
        _c("ROOM_TIGHT", I, "Room below a workable size",
           "A room is smaller than the usable floor its function needs — by area "
           "(kitchen ~70, full bath ~48, half bath ~30 sq ft) or by shortest side "
           "(full bath >= 6 ft, half bath >= 5 ft, so the fixtures fit across it). "
           "Bedrooms are covered by BEDROOM_AREA."),
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
        _c("DOOR_NARROW", W, "Door too narrow",
           "A swinging interior door is below the 30 in minimum clear width."),
        _c("DOOR_SIZE", I, "Non-standard door width",
           "A swing door's width isn't a manufactured leaf size (interior "
           "24/28/30/32/36 in; exterior 30/32/36, doubles 60/72). Snap it to the "
           "nearest so it's orderable off-the-shelf."),
        _c("OPEN_BATH", W, "Bathroom has no door",
           "A bathroom is joined by an `open` passage; baths need a door for privacy."),
        # --- openings -------------------------------------------------------
        _c("WINDOW_REF", E, "Window references unknown room",
           "A window names a room id that doesn't exist."),
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
        _c("ENTRY_INTERIOR", E, "Entry on an interior wall",
           "An exterior door is on a wall that doesn't face outside."),
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
        # --- access ---------------------------------------------------------
        _c("NO_ENTRY", E, "No exterior door",
           "The plan has no exterior door — no way to enter the building."),
        _c("NO_ACCESS", E, "Room unreachable",
           "A room can't be reached from any entrance through interior doors. "
           "A warning (not error) for a closet/pantry/loft, which may be open "
           "or reached by stairs not yet modelled."),
        # --- egress & light -------------------------------------------------
        _c("BEDROOM_EGRESS", E, "Bedroom has no escape opening",
           "Every bedroom needs an emergency escape opening — a window or its "
           "own exterior door — on an exterior wall (IRC R310)."),
        _c("EGRESS_SIZE", W, "Egress opening too small",
           "A bedroom's escape opening is below the IRC R310 minimums: ~5.7 sq "
           "ft net clear opening (5.0 at grade), >= 20 in clear width, >= 24 in "
           "clear height, sill <= 44 in above the floor."),
        _c("EGRESS_DOOR", W, "No wide egress door",
           "No exterior egress door is at least 32 in clear wide (R311.2)."),
        _c("NAT_LIGHT", W, "Insufficient natural light",
           "A habitable room's glazing on exterior walls is below 8% of floor "
           "area (R303.1)."),
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
           "second way out. Garage/porch doors don't count."),
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
           "walls, spreading plumbing runs out."),
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
        _c("MASTER_ENSUITE", I, "No private ensuite",
           "On a floor with two or more full bathrooms, no bedroom has a private "
           "(ensuite) bath — every bath is shared. The primary bedroom should get "
           "its own."),
        _c("ROOM_PROPORTION", I, "Awkwardly elongated room",
           "A habitable room is more than ~3:1 long-to-short and hard to furnish."),
        _c("GARAGE_BEDROOM", W, "Garage opens into a bedroom",
           "A garage must not open directly into a sleeping room (IRC R302.5.1)."),
        _c("GARAGE_NO_ENTRY", I, "Garage has no people-door",
           "A garage abuts the house but has no interior door into it."),
        _c("PROGRAM_MISMATCH", W, "Plan doesn't match its program",
           "The rooms placed don't match the declared `program`: exact bed/bath "
           "counts, an at-least requirement for another room type (e.g. "
           "`1 laundry`), or a minimum conditioned `area`."),
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
        _c("BAY_WIDE", I, "Wide frame bay spacing",
           "The frame's bay spacing is wider than typical residential post-frame "
           "(~12 ft on centre). Legal with adequately sized members, but it asks "
           "more of the beams and posts — lower `bay` or have them engineered."),
        # --- agent layer ----------------------------------------------------
        _c("DESIGN", I, "Architect's critique",
           "A design-quality suggestion folded in from the agent's architect review."),
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
