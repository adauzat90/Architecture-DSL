"""Claude-powered agentic workflow built around the compiler.

The agent *writes architecture in the DSL*. Each round it emits DSL source, the
compiler returns diagnostics (errors + fix hints), and those diagnostics are fed
straight back as the next prompt — a compile-fix loop, exactly like a developer
iterating against compiler output:

    1. **write**    DSL source from the brief (+ prior source + diagnostics);
    2. **compile**  → plan + diagnostics (line, code, hint) + a deterministic
       0-100 design score (see :mod:`barndsl.score`) + the resolved geometry
       pack (see :mod:`barndsl.introspect`);
    3. **critique** the design for quality (optional, model-driven, anchored to
       the score evidence — with the rendered floor-plan image attached when
       the optional ``cairosvg`` raster dependency is installed, so the critic
       judges the drawing, not just the text);
    4. **revise**   feed the structured diagnostics + score + critique back,
       rewrite the DSL, repeat — until it compiles clean, the critic is
       satisfied AND the score clears ``target_score`` (or the cap is hit).

The loop hill-climbs on the score: every step is scored, and ``design()``
returns the **best-scoring** iteration, not the last — a regression on the
final round is never silently returned.

Requires ``anthropic`` and ``ANTHROPIC_API_KEY``. Install ``pip install 'barndsl[agent]'``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .compiler import DSL_REFERENCE, CompileResult, compile_source
from .compiler import _KEYWORDS as _STMT_KEYWORDS
from .introspect import plan_summary, render_ascii_plan, summary_text
from .score import ScoreReport, design_score
from .validation import Issue, Severity

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"

#: Environment variable that overrides the model everywhere (CLI ``design`` and
#: the playground's ``/api/design``). Set this when the endpoint is not native
#: Anthropic — e.g. ``ANTHROPIC_BASE_URL`` points at DeepSeek's compat gateway,
#: where ``claude-opus-4-8`` does not resolve and silently returns empty output.
MODEL_ENV_VAR = "BARNDSL_MODEL"


def resolve_model(model: str | None = None) -> str:
    """The model to drive the agent with: explicit arg, else ``$BARNDSL_MODEL``, else the default.

    An explicit ``model`` argument always wins. Otherwise the ``BARNDSL_MODEL``
    environment variable is consulted (so a non-Anthropic endpoint can pick a
    model the gateway actually serves), falling back to :data:`DEFAULT_MODEL`.
    Read at call time, not import time, so setting the var before a run takes.
    """
    return model or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL


#: The per-call output-token cap for generation and critique. It bounds thinking
#: **plus** the answer, so a reasoning model that thinks heavily needs generous
#: headroom: ``deepseek-v4-pro`` can burn 8000+ tokens thinking about a single
#: plan, and at the old 8000 cap the reply hit ``stop_reason=max_tokens`` mid-
#: thought and returned no DSL at all. 32000 leaves room to think and still emit.
#: It is a cap, not a target — non-reasoning models (e.g. Opus) stop well under
#: it and pay only for what they generate.
DEFAULT_MAX_TOKENS = 32000

#: Environment variable that overrides :data:`DEFAULT_MAX_TOKENS` — raise it for a
#: model that reasons even more, or lower it to bound cost on a terse one.
MAX_TOKENS_ENV_VAR = "BARNDSL_MAX_TOKENS"


def resolve_max_tokens(max_tokens: int | None = None) -> int:
    """The per-call token cap: explicit arg, else ``$BARNDSL_MAX_TOKENS``, else the default.

    An explicit argument wins; otherwise the ``BARNDSL_MAX_TOKENS`` environment
    variable is used when it is a positive integer. Anything missing or malformed
    (non-numeric, ``<= 0``) falls back to :data:`DEFAULT_MAX_TOKENS`, so a stray
    value can never silently produce a zero/negative cap that the API would reject.
    """
    if max_tokens is not None:
        return max_tokens
    raw = os.environ.get(MAX_TOKENS_ENV_VAR)
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_TOKENS
        if value > 0:
            return value
    return DEFAULT_MAX_TOKENS

#: Below this score the design is not "done" even if the critic is satisfied.
DEFAULT_TARGET_SCORE = 90.0

#: Environment variable overriding :data:`DEFAULT_TARGET_SCORE` — the score gate
#: the loop hill-climbs toward. Lower it for a cheaper, "good enough" run.
TARGET_SCORE_ENV_VAR = "BARNDSL_TARGET_SCORE"

#: Default number of write → compile → critique → revise rounds.
DEFAULT_MAX_ITERATIONS = 3

#: Environment variable overriding :data:`DEFAULT_MAX_ITERATIONS`. More rounds =
#: more chances to converge (and more cost); fewer = faster, cheaper.
MAX_ITERATIONS_ENV_VAR = "BARNDSL_MAX_ITERATIONS"

#: Sentinel for "argument not supplied" where ``None`` is itself a meaningful
#: value — ``target_score=None`` disables the gate, so it can't double as "unset".
_UNSET: Any = object()

#: Effective-score ceiling applied to a step whose critique reported blocking
#: structural issues (see :attr:`CritiqueSpec.blocking_issues`). The TRUE score is
#: untouched — this clamps only the *gating total* used for the target gate and
#: best-step selection, so a structurally broken plan can never end the loop nor
#: win over a sound one on raw score alone. 65 sits below any realistic target so
#: a blocked plan is always held for a revision round.
BLOCKING_CLAMP = 65.0


def resolve_target_score() -> float:
    """The default score gate: ``$BARNDSL_TARGET_SCORE`` else :data:`DEFAULT_TARGET_SCORE`.

    A malformed value (non-numeric) falls back to the default. Note ``0`` is a
    valid gate that the loop can never fall below, i.e. effectively disabled —
    the same convention the CLI's ``--target-score 0`` uses.
    """
    raw = os.environ.get(TARGET_SCORE_ENV_VAR)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_TARGET_SCORE


def resolve_max_iterations() -> int:
    """The default round cap: ``$BARNDSL_MAX_ITERATIONS`` else :data:`DEFAULT_MAX_ITERATIONS`.

    Missing or malformed (non-numeric, ``<= 0``) falls back to the default, so a
    stray value can never produce a zero/negative cap that would skip the loop.
    """
    raw = os.environ.get(MAX_ITERATIONS_ENV_VAR)
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_MAX_ITERATIONS


#: The one-liner both the CLI and the playground surface when the agent can't run.
AGENT_INSTALL_HINT = "pip install 'barndsl[agent]' and set ANTHROPIC_API_KEY"


def agent_availability() -> tuple[bool, str | None]:
    """Whether the Claude agent can run here — ``(available, reason)``.

    Two gates, checked in order: the optional ``anthropic`` package must import,
    and ``ANTHROPIC_API_KEY`` must be set. Returns ``(True, None)`` when both
    hold, otherwise ``(False, <actionable reason>)``. The key's *value* is never
    read into the reason or returned anywhere — only its presence is probed — so
    this is safe to serve to a browser. Shared by the CLI ``design`` command and
    the playground's ``/api/agent`` probe so the two never diverge.
    """
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, f"the agent extra is not installed — {AGENT_INSTALL_HINT}"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, f"ANTHROPIC_API_KEY is not set — {AGENT_INSTALL_HINT}"
    return True, None


#: Total attempts (initial + retries) for a transient API failure on a single
#: generation call, and the backoff between them. ``anthropic`` is an optional
#: extra, so the retryable exception *types* are resolved lazily (see
#: :func:`_retryable_api_errors`); when it is absent the tuple is empty and the
#: guard catches nothing — the call simply behaves as if there were no retry.
_API_RETRY_ATTEMPTS = 3
#: Seconds to sleep before retry 2 and retry 3. Kept short and read through
#: :func:`time.sleep`, which tests patch to make the backoff instantaneous.
_API_RETRY_BACKOFF = (2.0, 4.0)


def _retryable_api_errors() -> tuple[type[BaseException], ...]:
    """The anthropic exception types a transient failure should be retried on.

    Resolved lazily, not at import time: ``anthropic`` is an optional extra, so
    catching these types must never require it to be installed. Returns an empty
    tuple when the package is absent (``except ():`` catches nothing), so the
    retry guard degrades to a plain call rather than raising ``ImportError``.
    ``APIStatusError`` (any 4xx/5xx with a status) is included here but the
    caller re-raises it unless the status is a 5xx — a 4xx is the caller's bug,
    not a transient blip, so it must propagate immediately.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is installed in CI
        return ()
    return (
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
    )

_DESIGN_RULES = """\
HOW AN ARCHITECT THINKS (do this before writing rooms):
1. ZONE the box into bands before placing anything. A barndo reads as three
   zones: PUBLIC (living/kitchen/dining - the day zone), PRIVATE (bedrooms +
   their baths - the night zone), and SERVICE (shop/garage, laundry, utility,
   mudroom). Keep each zone contiguous; put a buffer (hall, closets, laundry,
   or mudroom) on every seam between a loud/dirty zone and a quiet one. Declare
   the intent so the compiler can check it: `zone public: ...`, `zone night:
   ...`, and `suite primary: master mbath mcloset`.
2. CIRCULATE with one clear spine. From the front entry you must reach EVERY
   bedroom without passing THROUGH the garage/shop, a utility room, another
   bedroom, or a bathroom. The path to the night zone runs off the public core
   or a hall - NEVER through the service zone. Hang each private room one deep
   off the spine; keep total hallway under ~15% of the interior.
3. PLACE for daylight and privacy. Habitable rooms (living/kitchen/dining/
   bedroom/office/loft) take the PERIMETER for windows; bury halls, baths,
   closets, pantry, utility inside. Give the great room two exposures (windows
   on two walls). Put the primary suite at the OPPOSITE end from the kids'
   rooms; don't back its head wall onto the garage or the living-room TV wall.
   Aim rooms near 1.2:1-1.6:1 - never a tunnel (a 2:1+ room won't furnish).
4. OPEN the core, door the private. `open` living-kitchen-dining into one
   great room (vault it under the ridge with `vaulted` for the barndo look);
   give every bath and bedroom a real `door` for privacy. The kitchen is a
   ROOM, not a corridor - no through-traffic across the work triangle.

BARNDOMINIUM IDIOMS worth reaching for:
- Anchor the shop/garage at a GABLE END (short wall) or in its own wing, its
  `overhead` door facing the drive; buffer it from the house with a mudroom
  or hall (never a shared wall with a bedroom). `require separate <shop> <bed>`.
- A mudroom/drop-zone at the family entry (off the garage or the drive side)
  catches coats and boots before the kitchen. Keep it COMPACT - near 6 x 8 and
  at least 5 ft wide (a bench plus a walkway). A 4-ft-wide strip running the
  building's depth is a corridor wearing a mudroom label (MUDROOM_SHAPE);
  buffer the shop with the laundry/pantry stacked beside a short mudroom
  instead of stretching one room the whole seam.
- Deep covered porches on the SOUTH face, the front porch aligned to the
  living-room glazing; a back porch off the kitchen/dining for the grill.
- Kitchen toward the east for morning light; keep sink-range-fridge a tight
  triangle, dining on the kitchen's open side, with a door out to the porch.

LINT the compiler enforces (satisfy these too):
- Rooms stay inside the envelope, don't overlap, and tile it COMPLETELY -
  a room-sized unassigned pocket (AREA_VOID) is a compile ERROR: every
  enclosed square foot must belong to a room, so absorb any gap into a
  neighbour or declare a room (storage/closet/pantry/utility) there.
- An interior `door` only joins two rooms that SHARE A WALL; every room must be
  reachable from an `entry` through interior doors/opens.
- Bedrooms >= 70 sqft, smallest side >= 7 ft, each with an egress `window` on
  an exterior wall. At least one bath near the beds. One `entry` >= 2.67 ft.
- Habitable rooms need windows >= 8% of floor area (put them on exterior walls).
- WET_GROUP: cluster bath/kitchen/laundry on a shared plumbing wall.
- NO_CLOSET: every bedroom needs a closet reached by a door FROM it.
- BED_SOUND: buffer two adjacent beds with closets/hall on the shared wall.
- MASTER_ENSUITE / PRIVATE_PASSTHROUGH: the primary gets its own ensuite; never
  route the only path to a room through a bath or someone else's bedroom.
- GARAGE_PASSTHROUGH / GARAGE_BEDROOM: bedrooms must be reachable without
  walking through the shop/garage, and a garage never opens into a bedroom -
  buffer with a mudroom or hall (IRC R302.5.1).
- HALL_DEADEND / DOOR_CENTERED: cap a hall at a doorway; back swing doors to a
  corner with `offset` so a wall flank stays furnishable.
"""

#: The design method, stated as an ordered procedure. It sits between the
#: architectural principles (_DESIGN_RULES) and the placement mechanics
#: (_PLACEMENT_CRAFT): zone/circulate/place/open is the WHAT, this is the HOW-you-
#: work — parti header first, zones before rooms, then a walk-through the plan.
_DESIGN_PROCESS = """\
DESIGN PROCESS (follow when drafting a plan; when REVISING, obey the revision instruction in the request instead):
1. Open your source with a 2-3 line PARTI as a comment header stating the
   concept - zoning, the circulation spine, and the shop strategy. Example:
     # concept: public core (S) opens to a vaulted great room; 4 ft spine
     # feeds a private night wing (W); shop at the E gable, mudroom-buffered.
     # entry lands in a foyer; back porch off the dining.
2. Lay the ZONES down before rooms: sketch the public band, the night wing and
   the service end as blocks, then tile rooms inside each. Declare `zone` and
   `suite primary: ...` so the compiler checks the bands you intended.
3. WALK THE PLAN before you finish. Trace it and confirm:
   - Enter the front door: do you land in a foyer/mudroom/living space (a
     coat/drop landing), NOT straight into a bedroom hall or a bath?
   - From that entry, can you reach EVERY bedroom WITHOUT crossing the
     garage/shop, a utility room, another bedroom, or a bathroom?
   - Does the kitchen see the dining and a door to a porch, and is it OFF the
     through-path (not a corridor)?
   - Is there a coat/drop landing at each exterior door people use daily?
   If any answer is no, move rooms (not trim) until it is yes.
"""

#: Placement craft distilled from the authoring guide. The grammar reference
#: says what is *legal*; this says what *works* — the anchor rule and the
#: tile-then-connect idiom are where a model that free-hands `at x,y`
#: coordinates loses whole iterations to overlap and shared-wall errors.
_PLACEMENT_CRAFT = """\
HOW TO PLACE ROOMS (craft that keeps plans compiling first try):
- PREFER RELATIVE ANCHORS over `at x,y`. Abutting rooms automatically share a
  wall — exactly what an interior `door` requires. Hand-placed coordinates are
  where overlap and no-shared-wall errors come from (one foot off, or touching
  only at a corner, shares nothing).
- THE ANCHOR RULE — an anchor sets BOTH coordinates: `east-of`/`west-of` butt
  that wall and COPY the reference room's y; `north-of`/`south-of` stack on
  that wall and COPY its x. Chaining in one direction (a row or a column) is
  safe; branching a second column off a room already off the spine collides.
  For a hallway spine, hang EVERY served room directly off the spine, one room
  deep. `align near|far|center` / `offset <n>` slide a room along the shared
  wall; two anchors (one horizontal + one vertical) pin a corner.
- TILE, THEN CONNECT. Place rooms so neighbours abut and the envelope fills
  with little waste; then add one `door` per adjacency people actually walk.
- EGRESS FIRST. Give every bedroom its exterior-wall window as you place it —
  a missing egress window is the most common hard error.
- Habitable rooms (living/kitchen/dining/bed/office) go on the PERIMETER so
  they can take real windows; bury halls, baths, closets, storage inside.
- A bath gets a `door` (privacy); use `open` for kitchen/living/dining flow.
- Doors read best swung `into` the room they serve, hinged near a corner
  (`into <room> hinge near`), and backed to the wall's end with `offset`.
"""

#: Furnishing craft: the `fixture` catalog and where furniture belongs. Wet
#: rooms seed their own fixtures; furniture is an authoring act, so the prompt
#: asks for it explicitly — an empty bedroom on the drawing reads as an
#: unfinished design. Shared with the critic so "furnish the office" lands in
#: terms the generator understands.
_FURNISH_CRAFT = """\
FURNISH THE KEY ROOMS (a plan reads as a home when furniture proves each room works):
- Wet rooms furnish THEMSELVES: a bath seeds toilet/lavatory/tub, a kitchen
  seeds range/sink/refrigerator, a laundry seeds washer/dryer - write no
  `fixture` line for those unless you are deliberately moving one.
- Furniture is yours to place. EXACT grammar:
    fixture <kind> in <room> wall N|S|E|W [offset <n>]
        # auto-slots along that wall, sliding clear of door swings; add
        #   `offset <n>` (ft from the wall's S/W corner, like a door/window)
        #   only when you must pin the spot yourself
    fixture <kind> in <room> at <x>,<y>      # room-local feet from the SW corner
    fixture counter in <room> along N|S|E|W [from <a> to <b>] [depth <d>]
  Furniture kinds: bed_queen, bed_twin, sofa, armchair, dining_table,
  coffee_table, desk, dresser, wardrobe, kitchen_island, counter.
- MINIMUM furnishing: a bed in EVERY bedroom (bed_queen for the primary,
  bed_twin for kids/guests), sofa + coffee_table in the living room, a
  dining_table in the dining room, a desk in an office. A kitchen_island
  earns its place in a kitchen roomier than ~12 ft across.
- Craft: prefer a bare `wall N|S|E|W` over `offset`/`at` - the auto-slot
  clears door swings by itself; hand-pinned spots are where FIXTURE_DOOR and
  FIXTURE_OVERLAP warnings come from. Free-standing pieces (tables, island)
  centre themselves - leave ~2 ft of walkway around them. Never park tall
  casework (a wardrobe) over a bedroom's egress window, and stop a counter
  run short of doorways with `from`/`to`.
- BED FENG SHUI: the bed is the room's anchor - head it to a SOLID, windowless
  wall (never the wall its door is in: the leaf would sweep at the sleeper's
  head, and never a wall shared with a toilet/shower), centred with nightstand
  room on both sides (the auto-slot centres beds and sofas when the wall is
  clear). The sleeper should see the door without lying directly in line with
  it. Pick the bed wall FIRST, then place the door and window on the other
  walls to suit.
"""

#: A complete plan that compiles 0 errors / 0 warnings / 0 infos and scores
#: 100/100 — pinned by a test so it can never rot against the grammar. One
#: worked example anchors the output format better than any instruction,
#: especially for non-Claude models driven through a compat gateway.
_EXAMPLE_PLAN = """\
plan "Maple Two-Bed"
envelope 51 x 30
ceiling 10
program 2 bed 1 bath

room living:  living   at 0,0            size 20 x 30
room kitchen: kitchen  east-of living    size 22 x 14
room bath:    bathroom east-of kitchen   size 9 x 14
room hall:    hallway  north-of kitchen  size 31 x 4
room bed1:    bedroom  north-of hall     size 11 x 12
room c1:      closet   east-of bed1      size 4 x 12
room bed2:    bedroom  east-of c1        size 11 x 12
room c2:      closet   east-of bed2      size 5 x 12

open living - kitchen width 8
door living - hall width 3
door hall - bath width 2.67 offset 5.83 into bath hinge near
door hall - bed1 width 2.67 offset 0.5 into bed1 hinge near
door hall - bed2 width 2.67 offset 0.5 into bed2 hinge near
door bed1 - c1 width 2.5 offset 0.5 into bed1 hinge near
door bed2 - c2 width 2.5 offset 0.5 into bed2 hinge near
entry living south width 3 offset 8
entry living west width 3 offset 24

porch front at 6,-6 size 7 x 6 covered
porch side at -6,22 size 6 x 7 covered

window living west width 14 offset 8
window kitchen south width 8 offset 6
window bath east width 4 offset 5 sill 5
window bed1 north width 4 offset 3
window bed2 north width 4 offset 3

alarm smoke in bed1
alarm smoke in bed2
alarm smoke in hall
"""

#: The lead worked example: an ANNOTATED version of examples/gallery/hall_spine.barn
#: ("Birch Hollow", the strongest plan in the pool). It keeps that geometry but
#: adds `zone`/`suite` declarations and comments that explain WHY each move is
#: good, so the few-shot teaches architectural thinking, not just syntax. A test
#: compiles it and asserts 0 errors / 0 warnings.
_EXAMPLE_HALL_SPINE = """\
plan "Birch Hollow"
envelope 69 x 33
ceiling 10
program 3 bed 2 bath
require adjacent kitchen dining      # dining touches the kitchen: one eat-in flow
require separate master bed2         # primary sits away from the kids' wing

# concept: public band opens across the south into one great room; a 4 ft spine
# runs the north edge feeding a west kids' wing and an east primary suite; the
# hall dies at the master door, so no corridor runs past its last doorway.

suite primary: master mbath mcloset
zone public:  living kitchen dining
zone night:   bed1 bed2 master

# --- PUBLIC BAND (south): living-kitchen-dining open as one core.
#     Every public room is on the south wall for daylight + the front porch. ---
room living:  living  at 0,0   size 27 x 15
room kitchen: kitchen at 27,0  size 20 x 15   # centred: sees living AND dining
room dining:  dining  at 47,0  size 22 x 15   # on the kitchen's open side, by the back door

# --- THE SPINE: 4 ft, the ONLY corridor. It reaches every bedroom directly,
#     so no bedroom is entered through another private room. ---
room hall: hallway at 0,15 size 63 x 4

# --- WEST KIDS' WING: bed1 + bed2 do NOT share a wall - a stacked pair of
#     closets sits between them, buffering sound. ---
room bed1:    bedroom at 0,19   size 13 x 14
room closet1: closet  at 13,19  size 6 x 7     # bed1's closet, on the party wall
room closet2: closet  at 13,26  size 6 x 7     # bed2's closet, backing closet1 = sound buffer
room bed2:    bedroom at 19,19  size 13 x 14

# --- CENTRE: shared hall bath between the kids' wing and the suite. ---
room bath1: bathroom at 32,19 size 9 x 14

# --- EAST PRIMARY SUITE: ensuite backs onto the hall bath (one wet wall, short
#     plumbing runs); the walk-in caps the hall's east end so the corridor
#     stops at the master door instead of running past it. ---
room mbath:   bathroom at 41,19 size 6 x 14
room master:  bedroom  at 47,19 size 16 x 14   # opposite end from the kids
room mcloset: closet   at 63,15 size 6 x 18    # caps the corridor's east end

open living - kitchen width 10                 # open core: one great room
open kitchen - dining width 10
open living - hall width 4                     # public core feeds the spine
door hall - bed1 width 3 offset 0.5            # each bedroom hangs off the spine,
door hall - bed2 width 3 offset 0.5            #   one room deep, private door
door hall - bath1 width 2.67 offset 3
door hall - master width 3 offset 12.5         # master door AT the hall's end
door bed1 - closet1 width 2.5 offset 0.5 into bed1
door bed2 - closet2 width 2.5 offset 0.5
door master - mbath width 2.67 offset 0.5 into mbath   # ensuite reached only from master
door master - mcloset width 2.5 offset 10.5 into master

entry living south width 3 offset 20           # front door lands in the living core
entry dining east width 3 offset 6             # 2nd door off dining, by the back porch

porch front at 18,-6 size 7 x 6 covered        # landings at both doors (IRC R311.3);
porch back at 69,4 size 6 x 7 covered          #   front porch aligned to living glazing
window living south width 10 offset 2          # great room takes the south light
window kitchen south width 8 offset 6
window dining south width 8 offset 6
window bed1 north width 5 offset 4             # every bed's egress window, exterior wall
window bed2 north width 5 offset 4
window master north width 6 offset 6
window bath1 north width 3 offset 2 sill 5     # high privacy transom
window mbath north width 3 offset 1 sill 5

# --- FURNITURE: prove each room works. Baths/kitchen seed their own fixtures;
#     beds head to windowless walls; `wall <W>` auto-slots clear of door swings;
#     the free-standing tables centre themselves. ---
fixture bed_queen in master wall E
fixture bed_twin in bed1 wall W
fixture bed_twin in bed2 wall E
fixture sofa in living wall W
fixture coffee_table in living
fixture dining_table in dining

alarm smoke in bed1
alarm smoke in bed2
alarm smoke in master
alarm smoke in hall                            # smoke alarm outside the beds (IRC R314)
"""

#: An L-shaped footprint using `wing`, copied verbatim from
#: examples/gallery/lshape.barn (a test-pinned clean plan). Pinned by a test to
#: equal that file exactly, so the few-shot can never drift from the gallery.
_EXAMPLE_LSHAPE = """\
# L-shaped plan exercising `wing` — a 36x30 main block with an 18x18 primary
# suite projecting east. The footprint union (not a rectangle) drives which
# walls are exterior; the seam between block and wing is interior. The primary
# suite has its own ensuite + walk-in; the laundry doubles as a mudroom with a
# back door; bed1 caps the west end of the hall (its door at that end). All
# exterior dims are on the 3 ft build module.
plan "Maple Bend"
envelope 36 x 30
wing 18 x 18 at 36,0
ceiling 10
program 2 bed 2 bath

# --- main block public band (south) ---
room living:  living  at 0,0   size 20 x 14
room kitchen: kitchen at 20,0  size 16 x 14

# --- spine: 4 ft ---
room hall: hallway at 0,14 size 36 x 4

# --- main block private band (north) ---
room bed1:    bedroom  at 0,18   size 16 x 12
room closet1: closet   at 16,18  size 4 x 12
room bath:    bathroom at 20,18  size 8 x 12
room laundry: laundry  at 28,18  size 8 x 12

# --- east wing: primary suite with a private ensuite + walk-in ---
room master:  bedroom  at 36,0  size 12 x 18
room mbath:   bathroom at 48,0  size 6 x 9
room mcloset: closet   at 48,9  size 6 x 9

open living - kitchen width 10
open living - hall width 4
door hall - bed1 width 3 offset 0.5
door hall - bath width 2.67
door hall - laundry width 2.5
door hall - master width 3 into master
door bed1 - closet1 width 2.5 offset 0.5 into bed1
door master - mbath width 2.67 offset 0.5 into mbath
door master - mcloset width 2.5 offset 0.5

entry living south width 3 offset 14
entry laundry north width 3 offset 2

# Covered landings at both exterior doors — a floor to step onto (IRC R311.3).
porch front at 12,-6 size 7 x 6 covered
porch mud at 29,30 size 6 x 6 covered
window living south width 8 offset 2
window kitchen south width 6 offset 6
window bed1 north width 6 offset 5
window bath north width 3 offset 2 sill 5
window master south width 5 offset 4
window mbath east width 2 offset 3 sill 5

# Smoke alarms: one in each bedroom plus the hall outside the sleeping rooms
# (IRC R314); the bath windows are high privacy transoms clear of the tubs.
alarm smoke in bed1
alarm smoke in master
alarm smoke in hall
"""

#: A two-story plan using `level` and `stair`, copied verbatim from
#: examples/gallery/two_story.barn (a test-pinned clean plan). Pinned by a test to
#: equal that file exactly.
_EXAMPLE_TWO_STORY = """\
# Two-story plan with a `stair` and a `loft`. The loft sits on level 1 above the
# great room; the stair runs along the west wall (not marooned mid-room) and its
# footprint overlaps a room on each level, which links them and makes the loft
# reachable from the ground entry. The bedroom caps the west end of the hall and
# the laundry/mudroom the east end (its door at the hall end), so the corridor
# terminates at doorways. Exterior dims are on the 3 ft build module.
plan "Cedar Loft"
envelope 39 x 33
ceiling 9
program 1 bed 1 bath

# --- ground floor (level 0) ---
room living:  living   at 0,0   size 24 x 18
room kitchen: kitchen  at 24,0  size 15 x 18
room hall:    hallway  at 0,18  size 39 x 4
room bed:     bedroom  at 0,22  size 14 x 11
room closet:  closet   at 14,22 size 5 x 11
room bath:    bathroom at 19,22 size 8 x 11
room laundry: laundry  at 27,22 size 12 x 11

# --- upper floor (level 1) ---
room loft: loft at 0,0 size 24 x 18 level 1

# A queen bed against the bedroom's north wall (its head to the outside wall);
# the bath/kitchen/laundry fixtures auto-seed with no `fixture` line at all.
fixture bed_queen in bed wall N

# --- vertical circulation: along the west wall, clear of the kitchen doorway ---
stair flight at 0,3 size 4 x 12 from 0 to 1   # barndsl: accept STAIR_HANDRAIL "handrail on the west wall, on the construction documents"

open living - kitchen width 10
open living - hall width 4
door hall - bed width 3 offset 0.5
door hall - bath width 2.67 offset 2
door hall - laundry width 2.5 offset 9
door bed - closet width 2.5 offset 0.5 into bed

entry living south width 3 offset 14
entry laundry east width 3 offset 4

# Covered landings at both exterior doors — a floor to step onto (IRC R311.3).
porch front at 12,-6 size 7 x 6 covered
porch back at 39,24 size 6 x 7 covered
window living south width 10 offset 2
window kitchen south width 7 offset 6
window bed north width 4 offset 5
window bath north width 3 offset 2 sill 5
window loft south width 10 offset 7

# Smoke alarms: the bedroom + the hall outside it on the ground floor, and one on
# the loft level so every storey is covered (IRC R314.3(3)).
alarm smoke in bed
alarm smoke in hall
alarm smoke in loft
"""

# The system prompt is ordered to front-load architectural thinking and worked
# examples, and put the raw grammar LAST: the persona, then two complete worked
# plans (a hall-spine barndo, then an L-shaped `wing` plan), then the design
# rules, the design process, and the placement craft, then the full grammar
# reference for exact syntax, and finally the `program` mandate and the strict
# output contract. The contract sits at the very end on purpose — the model reads
# it last, right before it answers — so keep it there.
_GENERATE_SYSTEM = (
    "You are an expert residential designer specialising in barndominiums. You "
    "describe floor plans by writing source code in the barndsl architecture "
    "language, then refining it against the compiler's diagnostics until it is "
    "valid and well-designed.\n\n"
    "A COMPLETE, ANNOTATED EXAMPLE that compiles with zero errors and zero "
    "warnings — read the comments: it zones the box (public band, night wing, a "
    "shared centre), runs one 4 ft spine that reaches every bedroom directly, "
    "opens the core into one great room, buffers the two kids' beds with a "
    "stacked closet pair, gives the primary its own ensuite, and lands the front "
    "door in the living core:\n```barn\n" + _EXAMPLE_HALL_SPINE + "```\n"
    "\nAn L-shaped footprint using `wing` (the building is the union of the "
    "`envelope` and each wing; the seam between blocks is an interior wall):\n"
    "```barn\n" + _EXAMPLE_LSHAPE + "```\n"
    "\n"
    + _DESIGN_RULES
    + "\n"
    + _DESIGN_PROCESS
    + "\n"
    + _PLACEMENT_CRAFT
    + "\n"
    + _FURNISH_CRAFT
    + "\nFULL GRAMMAR REFERENCE (consult for exact syntax):\n\n"
    + DSL_REFERENCE
    + "\nYou MUST declare the brief's program as a `program` statement derived "
    "from the brief — grammar: `program <n> bed [<m> bath] [<k> <type> ...] "
    "[area <sqft>]` (e.g. `program 3 bed 2 bath area 1800`) — so the compiler "
    "checks the plan delivers what was asked, not what you remembered.\n"
    "\nOUTPUT FORMAT (strict): reply with exactly ONE ```barn code block "
    "containing the COMPLETE plan source — every line, never a diff, a "
    "fragment, or two alternatives. No prose, headings, or commentary outside "
    "the block; if you must reason first, keep it out of the final answer."
)

# The critic judges livability, not grammar: it is given the compiled
# diagnostics as evidence, so it does NOT need the full grammar reference. It
# shares the generator's design vocabulary instead — _DESIGN_RULES +
# _PLACEMENT_CRAFT + _FURNISH_CRAFT — so a suggestion it makes ("back the hall
# door to the end", "furnish the office") lands in terms the generator already
# understands.
_CRITIQUE_SYSTEM = (
    "You are a senior architect reviewing a barndominium plan (given as barndsl "
    "source plus the compiler's report) for design quality and livability.\n\n"
    "FIRST, walk the plan and answer these in your assessment - the design score "
    "cannot see all of them, so a high score does not excuse a 'no':\n"
    "  1. Enter the front door: do you land in a foyer/mudroom/living space, NOT "
    "straight into a bedroom hall or a bath?\n"
    "  2. From the entry, can you reach EVERY bedroom without crossing the "
    "garage/shop, a utility room, another bedroom, or a bathroom? A path to "
    "the bedrooms that runs THROUGH the garage/shop is a serious defect even "
    "if the score is high.\n"
    "  3. Does the kitchen see the dining and a porch, and is it OFF the "
    "through-path rather than a corridor?\n"
    "  4. Is the public zone contiguous, and the private zone buffered from the "
    "service zone (shop/laundry/utility)?\n"
    "  5. Is there a coat/drop landing at each daily entrance?\n\n"
    "Name the SINGLE biggest structural weakness and propose a ROOM-LEVEL move to "
    "fix it (swap two rooms, add a buffer, re-route the spine) - not a trim tweak. "
    "THEN list smaller nits. Judge flow, adjacencies, privacy, light, wasted space, "
    "and proportion - not just code-compliance. Anchor your verdict in the evidence. "
    "Be constructive but exacting. You share the designer's rulebook:\n\n"
    + _DESIGN_RULES
    + "\n"
    + _PLACEMENT_CRAFT
    + "\n"
    + _FURNISH_CRAFT
)

#: Appended to every critique prompt so the model emits JSON we can parse. Native
#: Anthropic gets the same instruction the SDK's structured output injects, so it
#: still validates via ``messages.parse``. Non-Anthropic gateways (DeepSeek) ignore
#: the SDK's injection but honour this in-prompt instruction, so they return JSON
#: (fenced or bare) that the fence-tolerant recovery path can then parse — instead
#: of the free-form prose they emit without it.
_CRITIQUE_JSON = (
    "\n\nRespond with ONLY a single JSON object — no prose or commentary around "
    "it — of exactly this shape:\n"
    '{"satisfied": true|false, "assessment": "<one-paragraph overall judgement>", '
    '"rationale": "<which diagnostics and score components justify the verdict>", '
    '"blocking_issues": ["<structural defect that makes the plan unshippable>", ...], '
    '"suggestions": ["<specific, actionable change>", ...]}\n'
    "Set `suggestions` to [] when satisfied is true. Give AT MOST 5 suggestions, "
    "ordered most-impactful first — five changes the designer will actually make "
    "beat a dozen that scatter the next revision.\n"
    "`blocking_issues` are ROOM-LEVEL STRUCTURAL defects (the walk-the-plan "
    "failures above) that make the plan unshippable regardless of score - leave "
    "it [] when there are none. Two hard rules: (1) a defect the compiler "
    "ALREADY reports (any coded diagnostic in the report - a missing landing, "
    "tempered glazing, a door width) is NEVER blocking; the score is already "
    "paying for it, so repeat it in suggestions only if you have a better fix "
    "than the hint. (2) blocking means fixing it requires MOVING ROOMS - if one "
    "or two edited lines (a porch, a window, one door) would fix it, it is a "
    "suggestion. Blocking (goes in blocking_issues): \"the only path from the "
    "living core to the bedrooms runs THROUGH the shop - swap the shop and bed "
    "wing and buffer with a mudroom\". Not blocking (a mere trim, goes in "
    "suggestions): \"shift the bed2 door flush to the hall's west end\"."
)

#: Appended to the critique system prompt only when a rendered PNG rides along —
#: the critic must not be told an image is attached when it isn't.
_CRITIQUE_VISION = (
    "\n\nA rendered floor-plan image of this plan is attached. Judge what only "
    "a drawing shows — proportion, circulation legibility, wasted pockets, "
    "dead-end halls, facade rhythm — and your `rationale` must cite what you "
    "see in the drawing (e.g. \"the kitchen is an island unreachable from the "
    "garage\"), not only the diagnostics."
)

_FENCE_RE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n(.*?)```", re.DOTALL)

_PROGRAM_RE = re.compile(r"^\s*program\b", re.MULTILINE)


def _extract_source(text: str) -> str:
    """Pull the DSL out of a model reply.

    Three tiers, most-structured first: the longest fenced code block (the
    output contract asks for exactly one, but a chatty model sometimes fences
    an extra snippet in its commentary — longest wins); else the span from the
    first to the last line that starts with a DSL statement keyword or a ``#``
    comment, which trims the "Here is the plan:" / "This design provides…"
    prose a compat-gateway model wraps around unfenced source; else the whole
    reply. The trim only cuts leading/trailing chatter — interior lines are
    kept verbatim, so a stray mid-plan remark still surfaces as a compiler
    diagnostic on its own line rather than being silently dropped.
    """
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    lines = text.splitlines()

    def _is_statement(line: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("#"):
            return True
        first = stripped.split(None, 1)[0] if stripped else ""
        return first in _STMT_KEYWORDS

    starts = [i for i, ln in enumerate(lines) if _is_statement(ln)]
    if starts:
        return "\n".join(lines[starts[0] : starts[-1] + 1]).strip() + "\n"
    return text.strip() + "\n"


def _json_object_from_text(text: str) -> str | None:
    """Extract a JSON object substring from a model reply, or ``None``.

    Handles the two ways a model returns JSON when it isn't using native
    structured output: wrapped in a ```json fence, or embedded in prose. Prefers
    a fenced block that looks like an object, else falls back to the span from
    the first ``{`` to the last ``}``. Returns ``None`` when there is no object
    (e.g. the reply is pure prose), so the caller can degrade gracefully.
    """
    for block in _FENCE_RE.findall(text):
        block = block.strip()
        if block.startswith("{") and block.endswith("}"):
            return block
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return text[start : end + 1]
    return None


def _critique_from_text(text: str) -> CritiqueSpec | None:
    """Parse a :class:`CritiqueSpec` out of raw model text, or ``None``.

    The critique is a streamed reply the model was told to emit as a JSON object;
    this pulls the object out (fenced or embedded — see :func:`_json_object_from_text`)
    and validates it. Any failure (no JSON object, malformed, or missing fields)
    yields ``None`` rather than raising, so the caller degrades gracefully.
    """
    candidate = _json_object_from_text(text)
    if candidate is None:
        return None
    try:
        return CritiqueSpec.model_validate_json(candidate)
    except ValidationError:
        return None


def _pump_activity(stream: Any, on_activity: Callable[[str, str], None]) -> None:
    """Forward a message stream's text/thinking deltas to ``on_activity``.

    Iterated *before* ``stream.get_final_message()`` (which still returns the
    fully accumulated reply), so a UI can watch the model reason and write the
    DSL live instead of staring at a spinner until the round lands.
    ``on_activity(channel, delta)`` is called with ``channel`` one of
    ``"thinking"`` (extended-reasoning text) or ``"text"`` (the visible reply —
    the DSL being written, or the critic's JSON).

    Tolerant by design: only the two known delta shapes are forwarded, and any
    streaming hiccup is swallowed — the live feed is a nicety, never a reason to
    fail a design round (``get_final_message`` below still produces the result,
    or raises the same error it would have without streaming). A compat gateway
    (DeepSeek) that never emits ``thinking_delta`` events simply streams the
    ``text`` channel; the loop and the UI both degrade cleanly.
    """
    try:
        for event in stream:
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = getattr(event, "delta", None)
            kind = getattr(delta, "type", None)
            if kind == "text_delta":
                piece = getattr(delta, "text", "") or ""
                if piece:
                    on_activity("text", piece)
            elif kind == "thinking_delta":
                piece = getattr(delta, "thinking", "") or ""
                if piece:
                    on_activity("thinking", piece)
    except Exception:  # never let a streaming glitch abort the design loop
        pass


def render_feedback(
    result: CompileResult,
    score: ScoreReport | None = None,
    *,
    best_prior: "DesignStep | None" = None,
    score_history: list[float] | None = None,
) -> str:
    """Render a compact, deterministic feedback block for the revision prompt.

    Structured fields from :meth:`CompileResult.to_dict` — one line per
    diagnostic (``severity CODE (room) line N: message | hint: ...``) headed by
    the score total, its non-zero per-component deductions and their cause
    lines (:attr:`ScoreReport.details` — the worst offenders, by name and
    number). When the compile produced a plan, the geometry pack from
    :func:`barndsl.introspect.plan_summary` is appended — resolved room
    rectangles with exterior walls, the door/adjacency edges, unplaced
    footprint pockets, and the free wall spans an opening can legally use — so
    the model reads coordinates off a table instead of re-deriving them from
    its own source. Token-lean by design: no source snippets, no caret art —
    the model gets fields to act on, not human formatting to scrape.

    ``best_prior`` is the best *valid* iteration recorded so far (or ``None``).
    When it is supplied and this attempt regressed against it — failed to
    compile, or scored lower — a single ``REGRESSION`` line is added under the
    header so the model reads the lost gradient as a regression and knows to
    revise from that iteration, not this broken one.

    When the ``errors`` component gates the score (a 100-point flat deduction
    that pins the total at 0), the header also carries a **projected** score with
    just that deduction removed — the number the plan would earn once the errors
    are fixed. A raw ``0/100`` hides whether the continuous terms are already
    strong; the projection shows the reward for fixing the errors, so the model
    keeps the good bones instead of rewriting a plan that was nearly there.

    ``score_history`` (keyword-only) is the chronological run of scored totals so
    far; when two or more are given, a ``Score history: a -> b -> c (this round)``
    line under the header shows the trajectory, so the model sees whether it is
    climbing or thrashing across rounds.
    """
    if score is None:
        score = design_score(result)
    deductions = ", ".join(f"{k} -{v:g}" for k, v in score.components.items() if v)
    header = (
        f"Design score: {score.total:g}/100"
        + (f" — deductions: {deductions}" if deductions else " — no deductions")
    )
    # When errors flat-gate the score to 0, project what the plan would earn once
    # they are fixed: the same total with only the errors deduction backed out,
    # clamped to [0, 100]. ASCII-only ("->") for cp1252 consoles.
    errors_component = score.components.get("errors")
    if errors_component:
        others = sum(v for k, v in score.components.items() if k != "errors")
        projected = max(0.0, min(100.0, 100.0 - others))
        header = (
            f"Design score: {score.total:g}/100 (errors block scoring; "
            f"projected once errors are fixed: {projected:g}/100)"
            + (f" - deductions: {deductions}" if deductions else " - no deductions")
        )
    lines = [header]
    if score_history is not None and len(score_history) >= 2:
        trail = " -> ".join(f"{s:g}" for s in score_history)
        lines.append(f"Score history: {trail} (this round)")
    if best_prior is not None and best_prior.score is not None:
        broke = result.plan is None or bool(result.errors)
        if broke or score.total < best_prior.score.total:
            what = "does not compile" if broke else f"scored {score.total:g}"
            lines.append(
                f"REGRESSION: best valid iteration so far scored "
                f"{best_prior.score.total:g} (iteration {best_prior.iteration}); "
                f"this attempt {what} — you lost the gradient, revise from that "
                f"iteration, not this one."
            )
    for name, cause in score.details.items():
        lines.append(f"  {name} -{score.components[name]:g}: {cause}")
    for d in result.to_dict()["diagnostics"]:
        where = f" ({d['room']})" if d["room"] else ""
        loc = f" line {d['line']}" if d["line"] else ""
        line = f"{d['severity']} {d['code']}{where}{loc}: {d['message']}"
        if d["hint"]:
            line += f" | hint: {d['hint']}"
        lines.append(line)
    if result.plan is not None:
        lines.append(summary_text(plan_summary(result.plan)))
        # A drawing makes what the table hides obvious — a shop band sandwiched
        # between living and bedrooms shows up as stacked letter rows. The critic
        # gets this for free: its user content embeds render_feedback (see
        # `critique`), so both the writer and the reviewer see the plan.
        lines.append(render_ascii_plan(result.plan))
    return "\n".join(lines)


def _plan_png(result: CompileResult) -> bytes | None:
    """Render the compiled plan to PNG bytes for the multimodal critique.

    Best-effort and silent by design: returns ``None`` (never raises) when the
    compile produced no plan or the optional ``cairosvg`` raster dependency is
    missing or fails — the critique then runs text-only, exactly as before.
    cairosvg stays optional (``pip install 'barndsl[raster]'``), the same guard
    :func:`barndsl.render.save_render` applies.
    """
    if result.plan is None:
        return None
    try:
        import cairosvg

        from .render import render_svg

        return cairosvg.svg2png(bytestring=render_svg(result.plan).encode("utf-8"))
    except Exception:
        return None


class CritiqueSpec(BaseModel):
    """The model's design-quality review."""

    satisfied: bool = Field(
        description="True only if the plan is genuinely good and needs no changes."
    )
    assessment: str = Field(description="One-paragraph overall judgement.")
    rationale: str = Field(
        description=(
            "The concrete evidence for the verdict: which diagnostics and score "
            "components (by code/name) justify satisfied being true or false."
        )
    )
    suggestions: list[str] = Field(
        default_factory=list, description="Specific, actionable changes. Empty if satisfied."
    )
    #: Room-level STRUCTURAL defects (through-shop paths, entry into the bed hall,
    #: a stranded night zone) that make the plan unshippable regardless of score.
    #: Additive with a default so a reply that omits it still validates; the loop
    #: clamps a step's *gating total* to :data:`BLOCKING_CLAMP` while any are present.
    blocking_issues: list[str] = Field(
        default_factory=list,
        description="Structural defects that make the plan unshippable regardless of score.",
    )
    #: True only on the degraded fallbacks (the critique call failed, or returned
    #: no parseable JSON) — a neutral verdict the model never emits itself.
    #: Additive with a default, so ``model_validate_json`` still accepts a real
    #: reply that omits it; lets a caller tell "the critic passed it" apart from
    #: "the critic was skipped and we defaulted to the compile status".
    skipped: bool = Field(
        default=False,
        description="Internal: set when the critique degraded to a neutral fallback.",
    )


@dataclass
class DesignStep:
    iteration: int
    source: str
    result: CompileResult
    critique: CritiqueSpec | None = None
    score: ScoreReport | None = None
    #: The score the loop *gates and ranks on*. Equal to ``score.total`` normally,
    #: but clamped to :data:`BLOCKING_CLAMP` when the critique flagged blocking
    #: structural issues, so a structurally broken plan can never end the loop nor
    #: out-rank a sound one on raw score. ``None`` means "use the true score" — the
    #: default for steps a caller builds without going through the loop.
    gating_total: float | None = None
    #: True when this step was generated in restructure mode (the write prompt told
    #: the model to reconsider the LAYOUT). Surfaced by the CLI per-iteration line.
    restructured: bool = False
    #: True when this step was generated in repair mode (the previous round failed
    #: to compile, so the write prompt told the model to reproduce its prior source
    #: and fix only the erroring lines). Mutually exclusive with ``restructured`` —
    #: repair takes priority over restructure. Surfaced by the CLI line too.
    repaired: bool = False
    #: True when this round's FIRST write failed to compile and the in-round repair
    #: retry (one extra generation call, same iteration) produced the compiling
    #: source recorded here. Orthogonal to ``restructured``/``repaired`` — those
    #: say how the round's first write was prompted; this says its compile was
    #: rescued afterwards. Surfaced by the CLI line too.
    salvaged: bool = False

    @property
    def effective_total(self) -> float:
        """The gating total for ranking/gating: the stored clamp, else the true
        score, else ``-1.0`` when unscored (so it sorts below any scored step)."""
        if self.gating_total is not None:
            return self.gating_total
        return self.score.total if self.score is not None else -1.0


def _best_step(history: list[DesignStep]) -> DesignStep:
    """The highest-scoring step; ties go to the later iteration.

    Ranks on the *gating* total (:attr:`DesignStep.effective_total`), so a step
    the critic flagged as structurally broken — clamped to :data:`BLOCKING_CLAMP` —
    cannot win over a sound one that scored lower on raw points.
    """
    return max(history, key=lambda s: (s.effective_total, s.iteration))


def _best_valid_step(history: list[DesignStep]) -> DesignStep | None:
    """The highest-scoring step that actually *compiled* (a plan, no errors).

    Ranks on the *gating* total (:attr:`DesignStep.effective_total`), so a blocked
    step is de-preferenced here too. Ties go to the later iteration. Returns
    ``None`` when nothing valid has been recorded yet — used both to head off a
    regression (the "best prior valid" reference in the feedback) and to pick a
    sound source to revise from when the latest attempt failed to compile.
    """
    valid = [
        s
        for s in history
        if s.result.plan is not None and not s.result.errors and s.score is not None
    ]
    if not valid:
        return None
    return max(valid, key=lambda s: (s.effective_total, s.iteration))


@dataclass
class DesignResult:
    source: str
    result: CompileResult
    history: list[DesignStep] = field(default_factory=list)

    @property
    def plan(self):
        return self.result.plan

    @property
    def iterations(self) -> int:
        return len(self.history)

    @property
    def best_iteration(self) -> int:
        """Which iteration won (its source/result are what this carries)."""
        if not self.history:
            return 0
        return _best_step(self.history).iteration

    @property
    def score(self) -> ScoreReport:
        """The winning step's score report (recomputed if it wasn't stored)."""
        if self.history:
            best = _best_step(self.history)
            if best.score is not None:
                return best.score
        return design_score(self.result)


class BarndoAgent:
    """Drives the write → compile → score → critique → revise loop."""

    def __init__(self, model: str | None = None, client=None, max_tokens: int | None = None):
        # ``None`` resolves via $BARNDSL_MODEL then DEFAULT_MODEL, so the endpoint
        # (Anthropic vs. a compat gateway) can pick a model it actually serves.
        self.model = resolve_model(model)
        # Output-token cap for generation and critique; big enough for a reasoning
        # model to think and still emit (see resolve_max_tokens / DEFAULT_MAX_TOKENS).
        self.max_tokens = resolve_max_tokens(max_tokens)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "The agent requires the 'anthropic' package. "
                    "Install with: pip install 'barndsl[agent]'"
                ) from exc
            # Point the agent at a custom endpoint (proxy, gateway, or a
            # self-hosted Anthropic-compatible API) via ANTHROPIC_BASE_URL.
            # Unset → the SDK default (https://api.anthropic.com).
            base_url = os.environ.get("ANTHROPIC_BASE_URL") or None
            self._client = anthropic.Anthropic(base_url=base_url)
        return self._client

    def _available_client(self):
        """A usable client for the (optional) LLM-brief seed, or ``None``.

        Returns an injected client as-is; otherwise resolves the real client the
        same way :attr:`client` does, but swallows the missing-``anthropic`` error
        so the solver-seed path degrades to no-LLM-brief instead of raising. Kept
        separate from :attr:`client` (which must raise for the write/critique
        calls that genuinely need a client) — here, "no client" is a valid state
        that simply skips the LLM brief.
        """
        if self._client is not None:
            return self._client
        try:
            return self.client
        except Exception:
            return None

    # -- single steps ------------------------------------------------------

    def write_source(
        self,
        brief: str,
        prior: str | None = None,
        diagnostics: str | None = None,
        seed: str | None = None,
        on_activity: Callable[[str, str], None] | None = None,
        restructure: bool = False,
        repair: int = 0,
    ) -> str:
        """Generate DSL for one round (see :meth:`_write_source_ex`).

        Public signature unchanged for the common call; the loop calls
        :meth:`_write_source_ex` when it also needs the truncation flag.
        ``restructure``/``repair`` swap the revision instruction (see there).
        """
        source, _truncated = self._write_source_ex(
            brief, prior=prior, diagnostics=diagnostics, seed=seed,
            on_activity=on_activity, restructure=restructure, repair=repair,
        )
        return source

    def _write_source_ex(
        self,
        brief: str,
        prior: str | None = None,
        diagnostics: str | None = None,
        seed: str | None = None,
        seed_alternates: "list[SeedAlternate] | None" = None,
        on_activity: Callable[[str, str], None] | None = None,
        restructure: bool = False,
        repair: int = 0,
    ) -> tuple[str, bool]:
        """Generate DSL and report whether the reply was truncated.

        Returns ``(source, truncated)``. ``truncated`` is True only when the
        model hit ``stop_reason=max_tokens`` on BOTH the first try and the one
        silent retry — a reply cut off at the token cap is real DSL but may be a
        half-written plan, so the loop folds a deterministic TRUNCATED info into
        that round's feedback (after scoring) telling the model to be terser. A
        clean stop, or a truncation the retry recovered from, reports False.

        ``seed_alternates`` (only shown on the first write, alongside ``seed``) are
        structurally-different runner-up solver candidates the model may switch to
        or blend, so it isn't anchored to a single topology.

        ``restructure``/``repair`` swap the revision instruction. The default
        *refine* mode asks for the smallest local edit; *restructure* mode tells
        the model the layout itself is stuck and it MAY re-place rooms, change
        adjacencies and re-route circulation wholesale (turned on when the score
        plateaus, the critic flags a blocking issue, or a suggestion repeats).
        *repair* mode is used when a write failed to compile — both for the
        loop's in-round retry (same iteration, immediately after the failing
        write) and for the round after a failure that retry couldn't fix: it
        names the error count and forbids any redesign, so the model reproduces
        its prior source and edits only the few lines the errors name.
        ``repair`` (an error count > 0) takes priority over ``restructure`` — a
        broken plan is repaired, never restructured (see :meth:`design`).
        """
        prompt = f"Design brief:\n{brief}\n"
        if seed and not prior:
            prompt += (
                "\nA deterministic layout solver produced this dimensionally "
                "sound draft from the brief — rooms tile the envelope, interior "
                "doors sit on shared walls, and egress/daylight windows are "
                "placed. Start from these bones and improve the DESIGN (flow, "
                "adjacencies, zoning, proportion, light). You MAY re-place rooms "
                "or switch to an alternate start if the arrangement flows poorly "
                "- dimensional soundness matters, but the room arrangement is "
                "yours to improve:\n```barn\n" + seed + "```\n"
            )
            # Structural diversity: show the runner-up solver topologies so the
            # model can pick a different set of bones instead of polishing one.
            if seed_alternates:
                prompt += (
                    "\nALTERNATE STARTS (structurally different, also sound - you "
                    "may switch to or blend these bones instead of the seed):\n"
                )
                for alt in seed_alternates[:_MAX_SEED_ALTERNATES]:
                    prompt += (
                        f"\n[{alt.engine} engine, score {alt.score:g}]\n"
                        "```barn\n" + alt.source
                        + ("" if alt.source.endswith("\n") else "\n")
                        + "```\n"
                    )
        if prior:
            prompt += f"\nYour previous DSL:\n```barn\n{prior}```\n"
        if diagnostics:
            prompt += (
                "\nCompiler feedback on it (one line per diagnostic; the design "
                "score at the top is the number you are maximising — 100 is a "
                "clean, well-designed plan, and each deduction names where the "
                "points went). Fix EVERY error, address warnings where "
                "reasonable, and raise the score:\n\n" + diagnostics + "\n"
            )
        if prior or seed:
            prompt += (
                "\nReturn the COMPLETE revised source (every line — never a diff "
                "or a fragment) as one ```barn block. "
            )
            if repair:
                prompt += (
                    f"Your previous plan FAILED to compile with {repair} "
                    f"error(s). Do NOT redesign, re-place rooms, or change the "
                    "envelope. Reproduce your previous source exactly, changing "
                    "ONLY the smallest set of lines needed to fix each error "
                    "listed below (adjust a door/window offset or width, nudge "
                    "one room dimension). Every error message names the line and "
                    "the numbers that conflict - do the arithmetic and fix just "
                    "that. You may fix warnings ONLY when the fix does not move "
                    "rooms."
                )
            elif restructure:
                prompt += (
                    "The score has stalled or the architect flagged a structural "
                    "problem that local edits cannot fix. Reconsider the LAYOUT "
                    "itself, not just details: you MAY re-place rooms wholesale, "
                    "change adjacencies, and re-route circulation. Do NOT preserve "
                    "the current arrangement if a different one flows better. State "
                    "the new parti in a `# concept:` comment header. Then EARN the "
                    "restructure: re-derive every door/window offset and every "
                    "shared wall from the NEW room positions before you answer -- "
                    "prefer whole- or half-foot dimensions -- because a "
                    "restructured plan that fails to compile is a wasted round."
                )
            else:
                prompt += (
                    "Make the smallest revision that fixes the diagnostics: keep "
                    "every line that already works, and do not restructure or "
                    "rename rooms unless a diagnostic or the architect's review "
                    "demands it."
                )
        else:
            prompt += (
                "\nReturn the complete plan source as one ```barn block."
            )

        # One silent retry on an empty reply OR a truncated reply, then fail (on
        # empty) or accept-but-flag (on truncation). An empty body is what a
        # compat gateway returns when the model id doesn't resolve, and what a
        # reasoning model returns when it burns the whole token cap thinking —
        # both are configuration problems the caller must see, not a blank plan
        # the loop grinds against for max_iterations rounds. A truncated body is
        # partial DSL: worth one terser retry, but if the retry is also cut off
        # we still return what we have and let the loop warn the model.
        for attempt in (1, 2):
            msg = self._stream_message(prompt, on_activity)
            text = "".join(b.text for b in msg.content if b.type == "text")
            stop_reason = getattr(msg, "stop_reason", None)
            truncated = stop_reason == "max_tokens"
            if text.strip():
                # A truncated first reply is retried once (to get a complete
                # plan); a truncated second reply is returned anyway, flagged.
                if truncated and attempt == 1:
                    logger.warning(
                        "Model %r reply truncated at the token cap (attempt %d, "
                        "stop_reason=max_tokens) — retrying once for a complete plan.",
                        self.model, attempt,
                    )
                    continue
                return _extract_source(text), truncated
            logger.warning(
                "Model %r returned an empty reply (attempt %d, stop_reason=%s).",
                self.model, attempt, stop_reason,
            )
        raise RuntimeError(
            f"Model {self.model!r} returned no text twice in a row "
            f"(stop_reason={getattr(msg, 'stop_reason', None)!r}). If you are on "
            "a non-Anthropic gateway (ANTHROPIC_BASE_URL), check that "
            "BARNDSL_MODEL names a model the gateway actually serves, and that "
            "BARNDSL_MAX_TOKENS leaves a reasoning model room to think AND emit."
        )

    def _stream_message(
        self, prompt: str, on_activity: Callable[[str, str], None] | None
    ) -> Any:
        """Run one generation stream call, retrying transient API failures.

        A transient network/API error (``APIConnectionError``,
        ``RateLimitError``, or an ``APIStatusError`` with a 5xx status) must not
        abort the whole design run — the gateway hiccuped, not the plan. Retry up
        to :data:`_API_RETRY_ATTEMPTS` times with bounded backoff
        (:data:`_API_RETRY_BACKOFF`, patched short in tests). A 4xx
        ``APIStatusError`` is the caller's request being wrong (bad model id,
        auth, oversized prompt) — re-raise it immediately. When ``anthropic`` is
        absent the retryable tuple is empty, so this is a plain single call.
        """
        retryable = _retryable_api_errors()
        last_exc: BaseException | None = None
        for attempt in range(1, _API_RETRY_ATTEMPTS + 1):
            try:
                with self.client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=_GENERATE_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                    thinking={"type": "adaptive"},
                ) as stream:
                    if on_activity is not None:
                        _pump_activity(stream, on_activity)
                    return stream.get_final_message()
            except retryable as exc:
                # A 4xx status is a client error, not a transient blip — propagate.
                status = getattr(exc, "status_code", None)
                if status is not None and status < 500:
                    raise
                last_exc = exc
                if attempt >= _API_RETRY_ATTEMPTS:
                    break
                logger.warning(
                    "Generation call to model %r failed (%s: %s) — retry %d/%d.",
                    self.model, type(exc).__name__, str(exc)[:200],
                    attempt, _API_RETRY_ATTEMPTS - 1,
                )
                time.sleep(_API_RETRY_BACKOFF[min(attempt - 1, len(_API_RETRY_BACKOFF) - 1)])
        assert last_exc is not None  # only reached after the retry loop exhausts
        raise last_exc

    def critique(
        self,
        result: CompileResult,
        score: ScoreReport | None = None,
        on_activity: Callable[[str, str], None] | None = None,
    ) -> CritiqueSpec:
        if score is None:
            score = design_score(result)
        prompt = (
            "Review this barndominium plan.\n\n"
            f"DSL source:\n```barn\n{result.source}```\n\n"
            f"Score and diagnostics (your evidence):\n{render_feedback(result, score)}\n\n"
            "Assess the design and list concrete improvements. Set satisfied=true "
            "only if it compiles clean AND is a genuinely good layout, and in "
            "`rationale` cite the specific diagnostics and score components that "
            "justify your verdict."
            + _CRITIQUE_JSON
        )
        # Give the critic eyes: attach the rendered plan when it can be
        # rasterised, and only then claim (in the system prompt) that it was.
        png = _plan_png(result)
        system = _CRITIQUE_SYSTEM
        content: str | list = prompt
        if png is not None:
            system = _CRITIQUE_SYSTEM + _CRITIQUE_VISION
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode("ascii"),
                    },
                },
                {"type": "text", "text": prompt},
            ]
        # Stream the critique (like write_source). A non-streaming call
        # (``messages.parse``) is refused by the SDK once max_tokens is high on a
        # slow reasoning model — "Streaming is required for operations that may
        # take longer than 10 minutes" — and a reasoning model (deepseek-v4-pro)
        # needs that headroom to think. The model is told to emit a JSON object
        # (via ``_CRITIQUE_JSON``), which we parse fence-tolerantly, so this works
        # the same on native Anthropic and on compat gateways (DeepSeek) that
        # ignore Anthropic structured output. Any failure — no parseable JSON, or a
        # network/API error — degrades to a neutral verdict (with a warning) so the
        # compile-score-revise loop keeps running on any endpoint.
        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                thinking={"type": "adaptive"},
            ) as stream:
                if on_activity is not None:
                    _pump_activity(stream, on_activity)
                msg = stream.get_final_message()
        except Exception as exc:
            logger.warning(
                "Design critique call failed on model %r (%s: %s) — continuing "
                "without the critic; the loop still compiles, scores and revises.",
                self.model, type(exc).__name__, str(exc)[:200],
            )
            return CritiqueSpec(
                satisfied=result.ok,
                assessment="(critique skipped: the critique call failed)",
                rationale="",
                skipped=True,
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        crit = _critique_from_text(text)
        if crit is None:
            logger.warning(
                "Critique returned no parseable JSON on model %r — continuing "
                "without the critic.", self.model,
            )
            return CritiqueSpec(
                satisfied=result.ok,
                assessment="(critique skipped: no parseable critique returned)",
                rationale="",
                skipped=True,
            )
        return crit

    # -- full loop ---------------------------------------------------------

    def design(
        self,
        brief: str,
        max_iterations: int | None = None,
        critique: bool = True,
        on_step=None,
        target_score: float | None = _UNSET,
        seed_with_solver=None,
        *,
        seed_source: str | None = None,
        cancel: Callable[[], bool] | None = None,
        on_phase: Callable[[str, int], None] | None = None,
        on_activity: Callable[[str, int, str, str], None] | None = None,
    ) -> DesignResult:
        """Run the write → compile → score → critique → revise loop.

        A round whose write fails to compile gets ONE in-round repair retry (an
        extra generation call in the same iteration, repair-mode prompt: fix
        only the erroring lines) before the round is recorded; the retry's
        source is kept only when it is strictly less broken. Only a failure the
        retry couldn't fix costs the iteration and puts the NEXT round in
        cross-round repair mode.

        ``max_iterations`` and ``target_score`` default to the environment-backed
        values (:func:`resolve_max_iterations` / :func:`resolve_target_score`, i.e.
        ``$BARNDSL_MAX_ITERATIONS`` / ``$BARNDSL_TARGET_SCORE`` or the built-in
        defaults) when left unset. An explicit ``target_score=None`` still
        *disables* the gate — the ``_UNSET`` sentinel is what selects the default,
        so ``None`` keeps its distinct "no gate" meaning.

        ``seed_with_solver`` opts into seeding the loop from the deterministic
        layout engines (:mod:`barndsl.layout`/:mod:`barndsl.layout2`). ``design``
        itself only receives a free-text ``brief``, which the solver can't
        consume, so the caller passes an explicit solver program: a
        :class:`~barndsl.layout2.LayoutBrief2`, a
        :class:`~barndsl.layout.LayoutBrief`, or a textual brief (parsed as a v2
        brief). The best solver candidate is recorded as **iteration 0** — the
        floor best-iteration-wins must beat — and its DSL is handed to the first
        generation prompt as a dimensionally sound draft to refine rather than a
        blank page. Falsy (the default) leaves the loop exactly as it was; if the
        solver produces nothing that compiles, seeding is silently skipped.

        ``seed_source`` (keyword-only) turns the loop into a **refinement**: the
        caller's current DSL is fed to round 1 as the "previous" source with its
        own diagnostics, so "make the kitchen bigger" revises that plan instead
        of starting from a blank page. Unlike the solver seed it is *not* scored
        as a competing iteration — refinement intentionally reshapes the design
        per the new brief, and must not be vetoed by best-iteration-wins if the
        edit trades a point of score for the user's request. Default ``None``
        leaves the loop unchanged.

        ``cancel`` (keyword-only) is polled once at the top of every round; when
        it returns true the loop stops and hands back the best iteration so far.
        ``on_phase`` (keyword-only) is called with ``(phase, round)`` — phase one
        of ``"writing" | "compiling" | "critiquing"`` — as each round advances,
        so a UI can narrate the loop between the coarser ``on_step`` results.
        ``on_activity`` (keyword-only) is finer still: called with ``(phase,
        round, channel, delta)`` for each streamed token chunk of the writing and
        critiquing LLM calls — ``phase`` is ``"writing"`` or ``"critiquing"`` and
        ``channel`` is ``"thinking"`` (the model's reasoning) or ``"text"`` (the
        DSL being written / the critic's reply). It lets a UI show the agent
        think and write live instead of waiting for the round to land. All three
        default ``None`` (no behaviour change, no calls).
        """
        # Resolve the environment-backed defaults (an explicit arg always wins;
        # target_score=None stays "gate disabled" — only _UNSET means "default").
        if max_iterations is None:
            max_iterations = resolve_max_iterations()
        if target_score is _UNSET:
            target_score = resolve_target_score()

        history: list[DesignStep] = []
        source: str | None = None
        feedback: str | None = None
        # Revision mode for the NEXT round, decided from THIS round's outcome.
        # ``repair_next`` (the previous round's error count) wins over
        # ``restructure_next``: a plan that failed to compile is repaired line by
        # line, never restructured. ``restructure_next`` (score plateaued, blocked,
        # or a suggestion repeated) is only ever set when the round COMPILED — a
        # 0-score plateau built from failed rounds must not latch it on.
        restructure_next = False
        repair_next = 0

        # A 2-arg (channel, delta) adapter that stamps each streamed token with
        # the phase and round the design-level ``on_activity`` contract carries.
        # ``None`` when no activity was requested, so ``write_source``/``critique``
        # never iterate the stream (today's callers pay nothing).
        def activity_for(phase: str, rnd: int) -> Callable[[str, str], None] | None:
            if on_activity is None:
                return None
            return lambda channel, delta: on_activity(phase, rnd, channel, delta)

        # Candidate 0: the deterministic solver's best plan, if one was requested
        # and it compiles. Recorded as iteration 0 so best-iteration-wins can
        # return it, and its source seeds the first generation prompt. Structurally
        # different runners-up ride the first prompt as ALTERNATE STARTS so the
        # model isn't anchored to one solver topology.
        solver_seed: str | None = None
        seed_alternates: list[SeedAlternate] = []
        if seed_with_solver:
            seed_res = _solver_seed(seed_with_solver, client=self._available_client())
            if seed_res is not None:
                seed_step = seed_res.step
                # Review the seed like any compiling round, for two reasons.
                # Fairness: LLM rounds are clamped to BLOCKING_CLAMP on blocking
                # issues, so an unreviewed seed would out-rank a better-but-blocked
                # round on its unclamped score. Direction: with the seed's review
                # folded into round 1's feedback, the first write revises toward
                # the architect's notes instead of re-interpreting the brief.
                if critique and seed_step.result.ok and seed_step.score is not None:
                    if on_phase is not None:
                        on_phase("critiquing", 0)
                    seed_crit = self.critique(
                        seed_step.result, seed_step.score,
                        on_activity=activity_for("critiquing", 0),
                    )
                    seed_step.critique = seed_crit
                    _fold_critique(seed_step.result, seed_crit)
                    _fold_blocking(seed_step.result, seed_crit)
                    if seed_crit.blocking_issues:
                        seed_step.gating_total = min(
                            seed_step.score.total, BLOCKING_CLAMP
                        )
                        # The critic found a structural flaw in the seed itself:
                        # round 1 gets restructure licence (and its arithmetic
                        # discipline) instead of polishing a broken arrangement.
                        restructure_next = True
                # Round 1 revises the seed with its report in hand — score
                # breakdown, plan view, and the folded review — not just its
                # source. Without this the first write flies blind past the
                # seed's known weaknesses.
                if seed_step.score is not None:
                    feedback = render_feedback(
                        seed_step.result, seed_step.score,
                        score_history=[seed_step.score.total],
                    )
                history.append(seed_step)
                solver_seed = seed_step.source
                seed_alternates = seed_res.alternates
                if on_step:
                    on_step(seed_step)

        # Refinement: revise the caller's current plan. Prime round 1 with it as
        # the "prior" source plus its diagnostics so the first write is a revision.
        if seed_source is not None:
            source = seed_source
            seed_result = compile_source(seed_source, name=None)
            _fold_program_nudge(seed_result)
            feedback = render_feedback(seed_result)

        def scored_totals() -> list[float]:
            """The chronological totals of every scored step so far (incl. the
            solver seed at iteration 0), for the score-history feedback line."""
            return [s.score.total for s in history if s.score is not None]

        def compiling_scored_totals() -> list[float]:
            """Totals of only the steps that COMPILED (a plan, no errors).

            Feeds the stagnation detector: a failed round scores 0, so a run of
            failed rounds would otherwise read as a flat plateau and (wrongly)
            trigger restructure. Restructure is a signal that *compiling* plans
            have run out of gradient, so it must be judged over compiling scores
            alone — a 0 from a broken round is repaired, not restructured."""
            return [
                s.score.total
                for s in history
                if s.score is not None and s.result.ok
            ]

        for i in range(1, max_iterations + 1):
            if cancel is not None and cancel():
                break
            # Snapshot how THIS round writes, so the step (recorded below) reflects
            # how its source was generated. Repair wins over restructure: when the
            # previous round failed to compile the model reproduces its prior source
            # and fixes only the erroring lines (never redesigns), so a broken round
            # is never also restructured.
            repaired = repair_next > 0
            restructured = restructure_next and not repaired
            if on_phase is not None:
                on_phase("writing", i)
            # Generation can raise: the RuntimeError from two empty replies, or an
            # API error that survived the transient-retry loop. Never discard the
            # rounds already completed — if we have any history, warn and hand back
            # the best step so far; only re-raise when there is nothing to return.
            try:
                source, truncated = self._write_source_ex(
                    brief, prior=source, diagnostics=feedback, seed=solver_seed,
                    seed_alternates=seed_alternates,
                    on_activity=activity_for("writing", i),
                    restructure=restructured,
                    repair=repair_next if repaired else 0,
                )
            except Exception as exc:
                if not history:
                    raise
                logger.warning(
                    "Generation failed on iteration %d (%s: %s) — returning the "
                    "best of the %d completed iteration(s).",
                    i, type(exc).__name__, str(exc)[:200], len(history),
                )
                break
            if on_phase is not None:
                on_phase("compiling", i)
            result = compile_source(source, name=None)
            # The program nudge is folded before scoring: it is deterministic
            # (a pure function of the source), so the score stays a contract —
            # and a missing `program` line now costs points the loop can win back.
            _fold_program_nudge(result)
            # In-round repair: when THIS round's write fails to compile, spend one
            # extra generation call to fix it NOW instead of burning the iteration
            # and repairing next round. Restructure rounds are the usual patient
            # (moving rooms wholesale breaks a door offset or two), and a salvaged
            # restructure keeps its structural gains where the old flow lost them:
            # fail → next round repairs → the redesign itself is often reverted.
            # The retry reuses repair mode verbatim (reproduce, fix only the
            # erroring lines) and is kept only when STRICTLY less broken, so a
            # "repair" that redesigns into different errors is discarded.
            salvaged = False
            if (result.plan is None or result.errors) and not (
                cancel is not None and cancel()
            ):
                if on_phase is not None:
                    on_phase("writing", i)
                retry_feedback = render_feedback(
                    result, design_score(result), score_history=scored_totals()
                )
                try:
                    retry_source, retry_truncated = self._write_source_ex(
                        brief, prior=source, diagnostics=retry_feedback,
                        on_activity=activity_for("writing", i),
                        repair=max(len(result.errors), 1),
                    )
                except Exception as exc:
                    logger.warning(
                        "In-round repair failed on iteration %d (%s: %s) — "
                        "keeping the failed round.",
                        i, type(exc).__name__, str(exc)[:200],
                    )
                else:
                    if on_phase is not None:
                        on_phase("compiling", i)
                    retry_result = compile_source(retry_source, name=None)
                    _fold_program_nudge(retry_result)
                    if _compile_badness(retry_result) < _compile_badness(result):
                        source, result, truncated = (
                            retry_source, retry_result, retry_truncated
                        )
                        salvaged = result.ok
            score = design_score(result)
            crit = None
            # Gate the critique on ``ok``, not just a plan: a partially-recovered
            # plan WITH errors scores 0 and can never satisfy the critic, so a
            # reasoning-model critique call on it is burnt tokens.
            if critique and result.ok:
                if on_phase is not None:
                    on_phase("critiquing", i)
                crit = self.critique(result, score, on_activity=activity_for("critiquing", i))
            # Fold the architect's review into the diagnostic stream as INFO, so
            # design feedback travels the same channel as the compiler's errors.
            # (After scoring: the critique is model-driven, the score is not.)
            _fold_critique(result, crit)
            # Fold the critic's BLOCKING structural issues (same channel, marked
            # "BLOCKING:") so the next prompt sees the effective-score clamp reason.
            _fold_blocking(result, crit)
            # Fold a TRUNCATED info when the reply was cut off at the token cap on
            # both the write and its retry. Folded AFTER scoring (like the critique)
            # so it rides the feedback text without perturbing the score contract:
            # the plan may be a half-written stub, so tell the model to be terser.
            _fold_truncation(result, truncated)

            # The gating total is the TRUE score, clamped to BLOCKING_CLAMP when the
            # critic flagged a blocking structural issue. It drives the target gate
            # and best-step selection (via DesignStep.effective_total); the true
            # ``score`` object is untouched, so render_feedback stays honest.
            blocked = crit is not None and bool(crit.blocking_issues)
            gating_total = min(score.total, BLOCKING_CLAMP) if blocked else score.total

            step = DesignStep(
                i, source, result, crit, score,
                gating_total=gating_total, restructured=restructured,
                repaired=repaired, salvaged=salvaged,
            )
            history.append(step)
            if on_step:
                on_step(step)

            done = result.ok and (crit is None or crit.satisfied)
            # Gate mechanically on the GATING total, not the raw score: the critic's
            # "satisfied" alone can't end the loop while points remain, and a
            # blocking structural issue caps the effective score below any target.
            if target_score is not None and gating_total < target_score:
                done = False
            if done or i == max_iterations:
                break
            # When THIS round failed to compile (even after the in-round retry
            # above spent its one attempt), the NEXT round is a REPAIR round:
            # the model reproduces this exact (failed) source and fixes only the
            # erroring lines. Carry the failed source forward as the "prior" (so
            # "reproduce it, fix line N" is coherent) with its OWN diagnostics, and
            # do NOT revert to a best-valid source — that would invite a wholesale
            # redesign, the very regression repair mode fixes. A failed round never
            # latches restructure; repair takes priority and the restructure trigger
            # is judged over compiling rounds only (below).
            failed = result.plan is None or bool(result.errors)
            if failed:
                repair_next = sum(1 for d in result.diagnostics if d.severity is Severity.ERROR)
                restructure_next = False
                source = history[-1].source  # the failed source, verbatim
                feedback = render_feedback(result, score, score_history=scored_totals())
            else:
                # A compiling round: decide whether the NEXT round restructures.
                # A score plateau (judged over COMPILING scores only — a 0 from a
                # broken round must not read as a flat plateau), a blocking issue
                # this round, or the same suggestion two rounds running all signal
                # local edits have run out and the layout needs rework.
                repair_next = 0
                prev_crit = history[-2].critique if len(history) >= 2 else None
                restructure_next = (
                    _stagnating(compiling_scored_totals())
                    or blocked
                    or _repeated_suggestion(prev_crit, crit)
                )
                # Flag a regression against the best valid iteration *before* this
                # one, so a lower-scoring round reads as a regression. Carry the
                # true score trajectory so the model sees whether it is climbing.
                feedback = render_feedback(
                    result, score, best_prior=_best_valid_step(history[:-1]),
                    score_history=scored_totals(),
                )

        if not history:
            # Cancelled before any round was recorded: still return a well-formed
            # result (the refinement seed, or an empty compile) rather than raise.
            src = seed_source or ""
            return DesignResult(src, compile_source(src, name=None), history)
        best = _best_step(history)
        return DesignResult(best.source, best.result, history)


def _fold_critique(result: CompileResult, crit: CritiqueSpec | None) -> None:
    """Append the architect's suggestions to ``result`` as INFO diagnostics.

    This makes design-quality feedback first-class: it shows up in
    ``result.report()`` (carets and all) alongside code-check errors, so a
    single diagnostic stream carries both "is it valid" and "is it good".
    """
    if crit is None or crit.satisfied:
        return
    # Cap what rides into the next prompt: the critic is asked for at most five
    # suggestions, but a chatty model (DeepSeek returns 7-12 a round) ignores
    # that, and a dozen INFO lines scatter the revision instead of focusing it.
    for s in crit.suggestions[:5]:
        result.diagnostics.append(
            Issue(Severity.INFO, "DESIGN", s, hint="Architect's review (design quality).")
        )


def _fold_blocking(result: CompileResult, crit: CritiqueSpec | None) -> None:
    """Fold the critic's blocking structural issues into ``result`` as INFO.

    Rides the SAME diagnostic channel :func:`_fold_critique` uses (so it reaches
    the next revision prompt via ``render_feedback`` without editing it), but each
    message is prefixed ``BLOCKING: `` and led with the clamp fact, so the model
    reads the plan's effective score as capped at :data:`BLOCKING_CLAMP` until the
    defect is fixed. Independent of ``satisfied``: a blocking issue is folded even
    on the rare "satisfied but with a blocker" reply. ASCII-only for cp1252.
    """
    if crit is None or not crit.blocking_issues:
        return
    for issue in crit.blocking_issues[:5]:
        result.diagnostics.append(
            Issue(
                Severity.INFO,
                "DESIGN",
                f"BLOCKING: a blocking structural issue caps this plan's effective "
                f"score at {BLOCKING_CLAMP:g} until fixed: {issue}",
                hint="Architect's review (blocking structural defect - fix before polishing).",
            )
        )


def _compile_badness(result: CompileResult) -> float:
    """How broken a compile is, for judging the in-round repair retry.

    No plan at all outranks any recovered plan (an unparseable source gives the
    next repair nothing to stand on); recovered plans rank by error count; 0.0
    means it compiled clean of errors. The retry replaces the original only when
    STRICTLY lower — equal badness keeps the original, so a "repair" that merely
    trades one broken arrangement for another is discarded.
    """
    if result.plan is None:
        return float("inf")
    return float(len(result.errors))


def _normalize_suggestion(text: str) -> str:
    """Casefold + collapse whitespace, for comparing suggestions across rounds."""
    return " ".join(text.split()).casefold()


def _stagnating(totals: list[float], window: int = 2, eps: float = 2.0) -> bool:
    """True when the last ``window`` score-to-score deltas are each below ``eps``.

    The score plateaued: successive rounds are moving the total by less than
    ``eps`` points apiece, so local polishing has run out of gradient and the loop
    should try a structural restructure. Needs ``window + 1`` totals to form
    ``window`` deltas; fewer (or a big jump anywhere in the window) returns False.
    """
    if len(totals) < window + 1:
        return False
    recent = totals[-(window + 1):]
    deltas = [abs(b - a) for a, b in zip(recent, recent[1:])]
    return all(d < eps for d in deltas)


def _repeated_suggestion(
    prev: CritiqueSpec | None, cur: CritiqueSpec | None
) -> bool:
    """True when any normalized suggestion appears in BOTH critiques.

    Two consecutive rounds naming the same fix means local edits aren't landing
    it — a signal to restructure. ``None`` on either side (a skipped critique)
    returns False.
    """
    if prev is None or cur is None:
        return False
    prev_set = {_normalize_suggestion(s) for s in prev.suggestions}
    return any(_normalize_suggestion(s) in prev_set for s in cur.suggestions)


def _fold_truncation(result: CompileResult, truncated: bool) -> None:
    """Append a TRUNCATED info when the generation reply was cut off at the cap.

    Folded the same way :func:`_fold_critique` folds the critique — AFTER the
    step is scored, so it never perturbs the score contract, and rides the
    diagnostic stream into the next revision prompt. ``truncated`` is only True
    when the model hit ``stop_reason=max_tokens`` on both the write and its one
    retry (see :meth:`BarndoAgent._write_source_ex`): the reply is real DSL but
    may be a half-written plan, so the model is told its previous answer was cut
    off and to be more concise. ASCII-only for cp1252 consoles.
    """
    if not truncated:
        return
    result.diagnostics.append(
        Issue(
            Severity.INFO,
            "TRUNCATED",
            "Your previous reply was cut off at the output token cap before it "
            "finished, so this plan may be incomplete. Write a more concise plan "
            "-- fewer rooms/openings if needed -- so the full source fits.",
            hint="Keep the answer inside the token cap; omit optional detail first.",
        )
    )


def _fold_program_nudge(result: CompileResult) -> None:
    """Append an INFO when the source declares no ``program`` statement.

    Deterministic (regex on the source, no model call) and folded the same way
    :func:`_fold_critique` folds the critique — the nudge rides the diagnostic
    stream into the next revision prompt, so the loop self-corrects until the
    brief's intent is declared where the compiler can check it.
    """
    if _PROGRAM_RE.search(result.source):
        return
    result.diagnostics.append(
        Issue(
            Severity.INFO,
            "NO_PROGRAM",
            "No `program` statement: declare the brief's intent so the compiler "
            "checks the plan delivers it.",
            hint=(
                "Derive it from the brief, e.g. `program 3 bed 2 bath area 1800` "
                "(grammar: `program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]`)."
            ),
        )
    )


# Prose-brief extractors. `parse_brief2` speaks the structured brief-statement
# grammar (`room id: type area N` / `adjacent a b` / `entry`), not English, so a
# natural-language brief ("3 bed 2 bath ~2000 sqft with a shop bay") can't seed
# the loop directly — these turn the numbers and program flags out of the prose
# into that grammar. Case-insensitive; each is deliberately narrow so it doesn't
# fire on incidental digits. A bedroom count is the one *required* signal (below):
# no bed count means "this isn't a program", and we return None rather than guess.
_PROSE_BED_RE = re.compile(r"(\d+)[\s-]*(?:bed(?:room)?s?|br\b)", re.IGNORECASE)
# Baths may be a decimal (2.5 bath); we floor to int, min 1, so a half-bath adds
# no extra shared/ensuite room — the grammar has no half-bath *program* room and
# the deduped count keeps the derived program honest.
_PROSE_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)[\s-]*bath", re.IGNORECASE)
# Floor area: 3-6 digits (with optional grouping commas) followed by a sqft unit,
# so a stray "2 bath" or "10 ceiling" never reads as an area. Commas stripped.
_PROSE_AREA_RE = re.compile(
    r"(\d[\d,]{2,5})\s*(?:sq\.?\s*ft\.?|sqft|square\s+feet)", re.IGNORECASE
)
# Program presence flags. `shop` covers the barndominium shop bay / garage /
# workshop (all lower to the same SHOP room, which the fill engine tiles fine —
# verified: keeping it is score-neutral vs omitting it and it honours the brief).
_PROSE_SHOP_RE = re.compile(r"\b(?:shop|garage|workshop)\b", re.IGNORECASE)
_PROSE_OFFICE_RE = re.compile(r"\b(?:office|study)\b", re.IGNORECASE)
_PROSE_MUDROOM_RE = re.compile(r"\bmud\s*room\b", re.IGNORECASE)
_PROSE_DINING_RE = re.compile(r"\bdin(?:ing|e)\b", re.IGNORECASE)


def _brief2_from_prose(text: str):
    """Derive a :class:`~barndsl.layout2.LayoutBrief2` from a prose brief, or None.

    Regex-scrapes the program out of natural language and synthesises the same
    structured brief TEXT that ``examples/oakline.brief`` is written in, then
    parses it with :func:`~barndsl.layout2.parse_brief2` — reusing the tested
    parser keeps one format source of truth rather than hand-building the dataclass.
    Returns ``None`` unless a bedroom count >= 1 was found: that count is the
    signal the text describes a *program* at all, so nonsense ("hello world") and
    non-program prose fall through to no-seed.

    Room areas mirror oakline (living 380, kitchen 280, dining 170, hall 150,
    master 224, extra bedrooms 156, ensuite+shared baths 100/90). When a floor
    area is stated, every room is scaled by ``target / base_total`` clamped to
    ``[0.7, 1.6]`` so a 1400- and a 2600-sqft brief both land on believable rooms
    without the tiler ever failing on an absurd envelope.
    """
    m = _PROSE_BED_RE.search(text)
    if not m:
        return None
    beds = int(m.group(1))
    if beds < 1:
        return None
    bm = _PROSE_BATH_RE.search(text)
    # Floor to int, min 1: a half-bath adds no extra program room (see _RE above).
    baths = max(1, int(float(bm.group(1)))) if bm else 1
    am = _PROSE_AREA_RE.search(text)
    target = float(am.group(1).replace(",", "")) if am else None

    has_shop = bool(_PROSE_SHOP_RE.search(text))
    has_office = bool(_PROSE_OFFICE_RE.search(text))
    has_mudroom = bool(_PROSE_MUDROOM_RE.search(text))
    # Dining appears when mentioned, or implicitly for 3+ bedrooms — oakline (a
    # 3-bed) includes it, so a same-sized program should read the same way.
    has_dining = bool(_PROSE_DINING_RE.search(text)) or beds >= 3

    # (id, type, base area, min-dim-or-None) — the oakline program, extended.
    rooms: list[tuple[str, str, int, int | None]] = [
        ("living", "living", 380, None),
        ("kitchen", "kitchen", 280, None),
    ]
    if has_dining:
        rooms.append(("dining", "dining", 170, None))
    rooms.append(("hall", "hallway", 150, 4))  # min 4 = comfort hallway width
    rooms.append(("master", "bedroom", 224, None))
    # A closet per bedroom, sitting in the private band beside its bedroom (the
    # adjacency below makes it the bedroom's row-neighbour, and _connect_adjacencies
    # cuts the door NO_CLOSET wants). The master gets a walk-in (min 4 ft so it isn't
    # a strip); the secondary bedrooms get reach-ins. Sizing rides the same area
    # scale as every other room, so the totals still track the stated floor area.
    rooms.append(("mcloset", "closet", 40, 4))
    for i in range(2, beds + 1):
        rooms.append((f"bed{i}", "bedroom", 156, None))
        rooms.append((f"closet{i}", "closet", 24, None))
    ensuite = baths >= 2
    if ensuite:
        rooms.append(("mbath", "bathroom", 100, None))  # master ensuite
        rooms.append(("bath2", "bathroom", 90, None))  # shared, off the hall
    else:
        rooms.append(("bath", "bathroom", 100, None))  # one shared bath
    if has_office:
        rooms.append(("office", "office", 120, None))
    # A shop is a garage-class bay (overhead door, vehicles) — it must NOT open off
    # the sleeping hall (GARAGE_PASSTHROUGH / GARAGE_BEDROOM). Force a mudroom to
    # buffer it from the house even when the prose never named one, so the shop
    # doors into the mudroom and the mudroom into the public core.
    want_mudroom = has_mudroom or has_shop
    if want_mudroom:
        rooms.append(("mudroom", "mudroom", 80, None))
    # Shop bay: functionally a garage (min 12 ft to take a vehicle). Kept because
    # it costs no score and delivers what the brief asked for (verified empirically).
    if has_shop:
        rooms.append(("shop", "shop", 400, 12))

    base_total = sum(area for _, _, area, _ in rooms)
    # Scale to the stated floor area, clamped so a tiny or huge target can't make
    # a room the tiler chokes on; unstated area leaves everything at oakline size.
    scale = 1.0
    if target is not None and base_total > 0:
        scale = max(0.7, min(1.6, target / base_total))

    lines = ['plan "Derived"', "ceiling 10"]
    for rid, rtype, area, min_dim in rooms:
        line = f"room {rid}: {rtype} area {round(area * scale)}"
        if min_dim is not None:
            line += f" min {min_dim}"
        lines.append(line)

    # Adjacencies mirror oakline: open core, hall spine off the living room, every
    # bedroom + the shared bath off the hall, the master's ensuite, entry at living.
    lines.append("adjacent living kitchen")
    if has_dining:
        lines.append("adjacent kitchen dining")
    lines.append("adjacent living hall")
    bedroom_ids = ["master"] + [f"bed{i}" for i in range(2, beds + 1)]
    shared_bath = "bath2" if ensuite else "bath"
    lines.append("adjacent hall " + " ".join(bedroom_ids + [shared_bath]))
    if ensuite:
        lines.append("adjacent master mbath")
    # Each bedroom doors into its own closet (what NO_CLOSET checks for). The
    # closet chains beside its bedroom in the private band, so _connect_adjacencies
    # cuts the door on their shared wall.
    lines.append("adjacent master mcloset")
    for i in range(2, beds + 1):
        lines.append(f"adjacent bed{i} closet{i}")
    # Buffer the shop OFF the sleeping spine: it doors into the mudroom, and the
    # mudroom into the public core (kitchen if there is one, else living) — never
    # `adjacent hall shop`, which would make the vehicle bay a corridor to the beds
    # (GARAGE_PASSTHROUGH). The mudroom is the drop-zone between the drive and the
    # house that a barndo shop wants anyway.
    if want_mudroom:
        core = "kitchen"  # kitchen always exists in this program
        lines.append(f"adjacent {core} mudroom")
    if has_shop:
        lines.append("adjacent mudroom shop")
    lines.append("entry living")

    from .layout2 import parse_brief2

    # Should never raise (we control the grammar), but a malformed synthesis must
    # degrade to no-seed rather than crash the design loop.
    try:
        return parse_brief2("\n".join(lines) + "\n")
    except Exception:
        return None


#: The compact brief2-grammar summary + worked example the LLM-brief system prompt
#: carries. Modeled on ``examples/oakline.brief``; kept small so the one call is
#: cheap. The marker phrase "You translate briefs" is deliberately distinct from
#: the critique dispatch marker "senior architect" (see the FakeClient in
#: tests/test_agent_loop.py, which tells generation from critique by that phrase),
#: so a test double can key on this call type without colliding.
_BRIEF_LLM_SYSTEM = (
    "You translate briefs. Given a free-text description of a house, you write a "
    "structured brief in the barndsl brief2 grammar (rooms by TARGET AREA plus "
    "adjacencies) that a deterministic layout solver can consume.\n\n"
    "GRAMMAR (one statement per line):\n"
    '  plan "<name>"\n'
    "  ceiling <ft>\n"
    "  room <id>: <type> area <sqft> [min <ft>]   # size program, not coordinates\n"
    "  adjacent <a> <b> [<c> ...]                  # connect <a> to each of the rest\n"
    "  entry <room>\n"
    "Room types: living kitchen dining bedroom bathroom hallway closet pantry "
    "mudroom office laundry utility loft garage shop.\n\n"
    "RULES: give every bedroom count the brief asks for; put a hallway spine and "
    "hang the bedrooms + a shared bath off it; open the living/kitchen/dining core; "
    "if a shop or garage is requested, BUFFER it with a mudroom (adjacent shop "
    "mudroom, adjacent mudroom kitchen) and NEVER put the shop adjacent to the "
    "hall or a bedroom. Only output the brief - no prose, no code fences.\n\n"
    "WORKED EXAMPLE:\n"
    'plan "Oakline 3-Bed"\n'
    "ceiling 10\n"
    "room living: living area 380\n"
    "room kitchen: kitchen area 280\n"
    "room dining: dining area 170\n"
    "room hall: hallway area 150 min 4\n"
    "room master: bedroom area 224\n"
    "room mbath: bathroom area 100\n"
    "room bed2: bedroom area 156\n"
    "room bed3: bedroom area 156\n"
    "room bath2: bathroom area 90\n"
    "adjacent living kitchen\n"
    "adjacent kitchen dining\n"
    "adjacent living hall\n"
    "adjacent hall master bed2 bed3 bath2\n"
    "adjacent master mbath\n"
    "entry living\n"
)


def _brief_from_llm(text: str, client):
    """Ask the LLM to write a brief2 from free-text prose, or ``None`` on any failure.

    The last resort in the seed-resolution chain: reached only for free text that
    BOTH :func:`~barndsl.layout2.parse_brief2` and :func:`_brief2_from_prose`
    failed on (the regex needs a bedroom-count signal and returns ``None`` without
    one). Makes ONE streaming call (the SDK requires streaming for a possibly-slow
    reasoning model), parses the reply with ``parse_brief2``, and on ANY failure —
    a missing client, an API error, an empty or unparseable reply — returns
    ``None`` so the caller falls back silently to an unseeded loop. Never raises.
    """
    if client is None or not (text and text.strip()):
        return None
    from .layout2 import parse_brief2

    prompt = (
        "Write a barndsl brief2 for this house description. Output ONLY the brief "
        "(no fences, no commentary):\n\n" + text.strip() + "\n"
    )
    try:
        with client.messages.stream(
            model=resolve_model(),
            max_tokens=resolve_max_tokens(),
            system=_BRIEF_LLM_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
        ) as stream:
            msg = stream.get_final_message()
    except Exception as exc:
        logger.warning(
            "LLM brief-translation call failed (%s: %s) — falling back to an "
            "unseeded loop.",
            type(exc).__name__, str(exc)[:200],
        )
        return None
    reply = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    if not reply.strip():
        return None
    # Try the raw reply first, then a fence-stripped version (the reply may arrive
    # fenced despite the instruction — reuse the same fence recovery the source
    # extractor uses so a compat-gateway model still parses).
    for candidate in (reply, _extract_source(reply)):
        try:
            return parse_brief2(candidate)
        except Exception:
            continue
    logger.warning("LLM brief was unparseable — falling back to an unseeded loop.")
    return None


def _solver_candidate_sources(spec, client=None) -> list[tuple[str, str]]:
    """Emit ``(engine_label, DSL)`` for each solver candidate derived from ``spec``.

    ``spec`` is a program the deterministic engines can solve — a
    :class:`barndsl.layout2.LayoutBrief2` (run through the space-filling
    topologies), a :class:`barndsl.layout.LayoutBrief` (the v1 greedy engine), or
    a textual brief (parsed as a v2 brief). Enumerates engines × topologies the
    way the CLI does, capped at the three v2 topologies (bands · slice · dual) so
    runtime stays bounded — every one is near-free (no API call). Returns one
    ``(engine, DSL)`` pair per topology that produced a plan, so the caller can
    keep structurally different runners-up with their engine label; any failure is
    skipped, so seeding always degrades gracefully to "no seed" (``[]``).

    A ``str`` spec is resolved through a fallback chain, in order: the structured
    brief-statement grammar (:func:`~barndsl.layout2.parse_brief2`); then, when
    that raises, the regex prose reader (:func:`_brief2_from_prose`), which needs
    a bedroom-count signal; then, only when a ``client`` is available and both
    have failed, ONE LLM call (:func:`_brief_from_llm`) that writes a brief2 from
    the prose. Structured input keeps precedence, and the regex still wins over the
    LLM whenever it can parse — so the LLM path only fires on free text with no
    bedroom-count signal. When every resolver fails the spec is unusable (``[]``).
    """
    from .emit import emit_dsl
    from .layout import LayoutBrief, solve_layout
    from .layout2 import LayoutBrief2, parse_brief2, solve_layout2

    if isinstance(spec, str):
        text = spec  # keep the original prose for the LLM fallback below
        try:
            spec = parse_brief2(spec)
        except Exception:
            # Not the structured grammar — try to read a program out of the prose.
            spec = _brief2_from_prose(text)
            if spec is None and client is not None:
                # Regex found no bedroom-count signal; ask the LLM to write a
                # brief2. Any failure returns None (silent fall to no-seed).
                spec = _brief_from_llm(text, client)
            if spec is None:
                return []

    results: list[tuple[str, Any]] = []
    if isinstance(spec, LayoutBrief2):
        for engine in ("bands", "slice", "dual"):
            try:
                out = solve_layout2(spec, engine=engine)
            except Exception:
                continue
            if out is not None and out.plan is not None:
                results.append((engine, out))
    elif isinstance(spec, LayoutBrief):
        try:
            out = solve_layout(spec)
        except Exception:
            out = None
        if out is not None and out.plan is not None:
            results.append(("greedy", out))
    else:
        return []

    sources: list[tuple[str, str]] = []
    for engine, out in results:
        try:
            src = emit_dsl(out.plan)
        except Exception:
            continue
        # The emitter writes no `program` statement, which would dock the seed
        # the missing-program nudge and depress the floor below what its
        # geometry earns. Declare the intent the plan itself delivers — the
        # counts come from the same metrics() PROGRAM_MISMATCH checks against,
        # so the derived line is guaranteed consistent.
        if not any(
            line.strip().startswith("program ")
            for line in src.splitlines()
        ):
            m = out.plan.metrics()
            beds, baths = int(m["bedroom_count"]), int(m["bathroom_count"])
            if beds:
                stmt = f"program {beds} bed"
                if baths:
                    stmt += f" {baths} bath"
                src = stmt + "\n" + src
        sources.append((engine, src))
    return sources


@dataclass
class SeedAlternate:
    """A structurally-different runner-up solver candidate for the first prompt.

    Carries the engine that produced it, its design-score total, and its full DSL
    source, so :meth:`BarndoAgent.design` can offer the model a bounded set of
    alternate starts it may switch to or blend, instead of anchoring it to one.
    """

    engine: str
    score: float
    source: str


@dataclass
class SeedResult:
    """The solver seed handed to :meth:`BarndoAgent.design`.

    ``step`` is the best compiling candidate as iteration 0 (the floor
    best-iteration-wins must beat); ``alternates`` are up to two compiling
    runners-up from DIFFERENT engines than the winner, near-duplicates skipped, so
    the first write prompt can show the model real structural diversity.
    """

    step: DesignStep
    alternates: list[SeedAlternate] = field(default_factory=list)


#: How many structurally-different runner-up seeds to keep and show the model.
_MAX_SEED_ALTERNATES = 2


def _solver_seed(spec, client=None) -> SeedResult | None:
    """The solver's best compiling candidate + alternates, or ``None``.

    Each candidate is compiled, folded through the same ``program`` nudge the loop
    applies (so its score is directly comparable to the LLM's iterations), and
    scored with :func:`design_score`; the highest-scoring one that actually
    compiles (a plan, no errors) becomes iteration 0. Up to
    :data:`_MAX_SEED_ALTERNATES` other compiling candidates from DIFFERENT engines
    than the winner are retained as :class:`SeedAlternate`\\ s (near-duplicate
    sources skipped), so the caller can offer the model structural diversity
    instead of a single anchored seed. Returns ``None`` when the solver yields
    nothing that compiles, so the loop then proceeds exactly as it does today.
    """
    # (engine, source, DesignStep) for every candidate that compiled clean.
    scored: list[tuple[str, str, DesignStep]] = []
    for engine, src in _solver_candidate_sources(spec, client=client):
        try:
            result = compile_source(src, name=None)
        except Exception:
            continue
        if result.plan is None or result.errors:
            continue
        _fold_program_nudge(result)
        score = design_score(result)
        scored.append((engine, result.source, DesignStep(0, result.source, result, None, score)))
    if not scored:
        return None

    # Best by score wins iteration 0 (ties keep discovery order, i.e. bands first).
    scored.sort(key=lambda t: -(t[2].score.total if t[2].score else 0.0))
    win_engine, win_src, win_step = scored[0]

    # Keep up to N runners-up from DIFFERENT engines, skipping near-duplicate
    # sources (a slice/dual tiling that came out identical to the winner adds no
    # diversity). Normalise whitespace before comparing so cosmetic differences
    # don't defeat the dedup.
    def _norm(s: str) -> str:
        return "\n".join(line.strip() for line in s.strip().splitlines())

    seen_norm = {_norm(win_src)}
    seen_engines = {win_engine}
    alternates: list[SeedAlternate] = []
    for engine, src, step in scored[1:]:
        if len(alternates) >= _MAX_SEED_ALTERNATES:
            break
        if engine in seen_engines:
            continue
        norm = _norm(src)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        seen_engines.add(engine)
        alternates.append(
            SeedAlternate(engine=engine, score=step.score.total if step.score else 0.0, source=src)
        )
    return SeedResult(step=win_step, alternates=alternates)


def _solver_seed_step(spec, client=None) -> DesignStep | None:
    """The solver's best compiling candidate as iteration 0 — or ``None``.

    A thin wrapper over :func:`_solver_seed` that discards the alternates and
    hands back just the winning :class:`DesignStep`, preserving the original
    public contract (the loop uses :func:`_solver_seed` directly when it also
    needs the alternate starts for the first write prompt).
    """
    res = _solver_seed(spec, client=client)
    return res.step if res is not None else None


def design(
    brief: str,
    model: str | None = None,
    max_iterations: int | None = None,
    on_step=None,
    target_score: float | None = _UNSET,
    seed_with_solver=None,
    *,
    seed_source: str | None = None,
    cancel: Callable[[], bool] | None = None,
    on_phase: Callable[[str, int], None] | None = None,
    on_activity: Callable[[str, int, str, str], None] | None = None,
) -> DesignResult:
    """Convenience: run :class:`BarndoAgent` end-to-end on ``brief``.

    ``max_iterations``/``target_score`` left unset fall to the environment-backed
    defaults, exactly as :meth:`BarndoAgent.design` documents.
    """
    return BarndoAgent(model=model).design(
        brief,
        max_iterations=max_iterations,
        on_step=on_step,
        target_score=target_score,
        seed_with_solver=seed_with_solver,
        seed_source=seed_source,
        cancel=cancel,
        on_phase=on_phase,
        on_activity=on_activity,
    )
