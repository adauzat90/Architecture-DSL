"""Phase 13 — the professional-review code-rule pack.

Each new/changed check gets a positive fire, a negative non-fire, and a threshold
edge, in the house style:

* WINDOW_FALL (R312.2) — operable low-silled window on an upper storey;
* WINDOW_TEMPERED case (d) (R308.4.3) — large low glazing panel, anywhere;
* STAIR_LANDING (R311.7.3) — a single flight rising past 12 ft 7 in;
* DOOR_THRESHOLD (R311.3.1) — the once-per-plan threshold-drop reminder;
* GARAGE_DOOR (R302.5.1) — now anchored on the `door` statement line;
* RECEPTACLE_COUNTER (E3901.4) — kitchen counter small-appliance receptacles;
* CLOSET_DOOR_SWING — a swing door that fills a shallow closet;
* the kitchen auto-seed spread (range/sink/refrigerator no longer butted).
"""

from __future__ import annotations

from barndsl import RoomType as T
from barndsl import barndominium, compile_source, validate
from barndsl.diagnostics import REGISTRY
from barndsl.fixtures import resolve_room_fixtures
from barndsl.profiles import get_profile


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def _scodes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


# --- WINDOW_FALL (IRC R312.2) ------------------------------------------------


def _two_storey(sill: float, *, level: int = 1, kind: str = "casement"):
    return (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=20)
        .add_room("up", T.BEDROOM, x=0, y=0, width=24, length=20, level=level)
        .entrance("living", "south", width=3, offset=10)
        .add_window("up", "north", width=4, offset=10, sill_height=sill, kind=kind)
    )


def test_operable_low_window_upstairs_needs_fall_protection():
    assert "WINDOW_FALL" in _codes(_two_storey(1.5))


def test_low_window_on_the_ground_floor_is_fine():
    # Level 0 is (in the model) at grade — R312.2's "more than 72 in above grade"
    # can't be met, so no fall-protection nag.
    assert "WINDOW_FALL" not in _codes(_two_storey(1.5, level=0))


def test_fixed_upstairs_window_is_exempt():
    # A fixed sash doesn't open, so there's no fall path (R312.2 is operable-only).
    assert "WINDOW_FALL" not in _codes(_two_storey(1.5, kind="fixed"))


def test_window_fall_sill_threshold_edge():
    # 24 in exactly is not "below 24 in" — just under it fires.
    assert "WINDOW_FALL" not in _codes(_two_storey(24 / 12))
    assert "WINDOW_FALL" in _codes(_two_storey(23.9 / 12))


# --- WINDOW_TEMPERED case (d): large low glazing panel (IRC R308.4.3) --------


def _panel(width: float, sill: float, head: float, *, tempered: bool = False):
    # A window well clear of any door/tub/stair, so only the large-panel case can
    # trigger. The kitchen keeps the window off the entry wall.
    return (
        barndominium("x").envelope(30, 20).ceiling(10)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=20)
        .entrance("living", "south", width=3, offset=2)
        .add_window(
            "living", "north", width=width, offset=12,
            sill_height=sill, head_height=head, tempered=tempered,
        )
    )


def test_large_low_panel_needs_tempering_anywhere():
    # 4 ft wide × (5.0 - 1.0) = 16 sq ft, bottom 12 in, top 60 in — a walk-into pane.
    assert "WINDOW_TEMPERED" in _codes(_panel(4.0, 1.0, 5.0))


def test_declared_tempered_panel_is_silent():
    assert "WINDOW_TEMPERED" not in _codes(_panel(4.0, 1.0, 5.0, tempered=True))


def test_small_or_high_panel_is_not_a_hazard():
    # Under 9 sq ft (2 ft × 4 ft = 8): not a large panel.
    assert "WINDOW_TEMPERED" not in _codes(_panel(2.0, 1.0, 5.0))
    # Bottom edge at 18 in exactly is not "below 18 in".
    assert "WINDOW_TEMPERED" not in _codes(_panel(4.0, 18 / 12, 5.0))
    # Top edge at 36 in exactly is not "above 36 in".
    assert "WINDOW_TEMPERED" not in _codes(_panel(4.0, 1.0, 36 / 12))


# --- STAIR_LANDING (IRC R311.7.3) -------------------------------------------


def _stair_plan(ceiling: float, to_level: int = 1):
    return (
        barndominium("x").envelope(30, 24).ceiling(ceiling)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .add_room("loft", T.LOFT, x=0, y=0, width=30, length=24, level=to_level)
        .entrance("living", "south", width=3, offset=10)
        .add_stair("s", x=0, y=0, width=6, length=12, from_level=0, to_level=to_level)
    )


def test_tall_flight_wants_an_intermediate_landing():
    # 13 ft storey → 156 in of rise in one flight, past R311.7.3's 151 in.
    assert "STAIR_LANDING" in _codes(_stair_plan(13.0))


def test_normal_storey_flight_needs_no_landing():
    assert "STAIR_LANDING" not in _codes(_stair_plan(9.0))


def test_stair_landing_rise_threshold_edge():
    # 12 ft storey (144 in) is under 151 in; 13 ft (156 in) is over.
    assert "STAIR_LANDING" not in _codes(_stair_plan(12.0))
    assert "STAIR_LANDING" in _codes(_stair_plan(13.0))


def test_stair_landing_riser_count_follows_the_profile():
    # The quoted allowable-riser count uses the profile riser, like STAIR_RUN.
    issue = next(
        i for i in validate(_stair_plan(20.0), get_profile("strict")).issues
        if i.code == "STAIR_LANDING"
    )
    # strict's 7 in riser allows more risers in the 151 in flight than the 7.75 in
    # default, so its quoted count is higher.
    assert "21 risers" in issue.message  # floor(151 / 7)


# --- DOOR_THRESHOLD (IRC R311.3.1) ------------------------------------------


def _entry_plan(with_porch: bool):
    b = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=20)
        .entrance("living", "south", width=3, offset=10)
    )
    if with_porch:
        b.add_porch("p", x=8, y=-6, width=7, length=6, covered=True)
    return b


def test_threshold_reminder_fires_once_before_a_porch_is_drawn():
    issues = [i for i in validate(_entry_plan(False)).issues if i.code == "DOOR_THRESHOLD"]
    assert len(issues) == 1
    assert issues[0].room == "living"


def test_threshold_reminder_stands_down_once_landings_exist():
    assert "DOOR_THRESHOLD" not in _codes(_entry_plan(True))


# --- GARAGE_DOOR now anchors on the door statement line (IRC R302.5.1) -------

_GARAGE_SRC = """\
plan "G"
envelope 40 x 24
ceiling 9
room living: living at 0,0 size 24 x 24
room mud: mudroom east-of living size 8 x 24
room garage: garage east-of mud size 8 x 24
entry living south width 3 offset 10
door living - mud width 3 offset 10
door mud - garage width 3 offset 10
"""


def test_garage_door_reminder_points_at_the_door_line():
    r = compile_source(_GARAGE_SRC)
    hits = [d for d in r.diagnostics if d.code == "GARAGE_DOOR"]
    assert len(hits) == 1
    # The `door mud - garage` statement is line 9 — the reminder anchors there,
    # not on the garage room.
    assert hits[0].line == 9
    assert "self-closing" in hits[0].message


# --- RECEPTACLE_COUNTER (IRC E3901.4) ---------------------------------------


def _counter_kitchen(outlet_offsets: list[float], *, electrical: bool = True):
    b = (
        barndominium("x").envelope(20, 16).ceiling(9)
        .add_room("kit", T.KITCHEN, x=0, y=0, width=20, length=16)
        .entrance("kit", "south", width=3, offset=16)
        .add_fixture("counter", "kit", along="north", run_from=0, run_to=12)
    )
    for off in outlet_offsets:
        b.add_outlet("kit", "north", offset=off)
    if electrical and not outlet_offsets:
        b.add_outlet("kit", "south", offset=1)  # opt into the electrical layer
    return b


def test_bare_counter_run_wants_a_receptacle():
    # A 12 ft counter run with no receptacle serving it.
    assert "RECEPTACLE_COUNTER" in _codes(_counter_kitchen([]))


def test_well_served_counter_run_is_quiet():
    # Receptacles at 2/6/10 ft leave every point within 24 in of one.
    assert "RECEPTACLE_COUNTER" not in _codes(_counter_kitchen([2.0, 6.0, 10.0]))


def test_receptacle_counter_reach_threshold_edge():
    # 0 and 4 → midpoint 24 in from each (OK); 0 and 5 → 30 in (too far).
    assert "RECEPTACLE_COUNTER" not in _codes(_counter_kitchen([0.0, 4.0, 8.0, 12.0]))
    assert "RECEPTACLE_COUNTER" in _codes(_counter_kitchen([0.0, 5.0]))


def test_receptacle_counter_gated_on_electrical():
    # No electrical layer at all → the whole check stands down (like OUTLET_SPACING).
    plan = (
        barndominium("x").envelope(20, 16).ceiling(9)
        .add_room("kit", T.KITCHEN, x=0, y=0, width=20, length=16)
        .entrance("kit", "south", width=3, offset=16)
        .add_fixture("counter", "kit", along="north", run_from=0, run_to=12)
    )
    assert "RECEPTACLE_COUNTER" not in _codes(plan)


# --- CLOSET_DOOR_SWING -------------------------------------------------------


def _closet_plan(depth: float, *, kind: str = "swing", into_closet: bool = True):
    # A bedroom with a shallow closet on its east wall; the door width (2.5) can
    # exceed the closet depth.
    b = (
        barndominium("x").envelope(30, 12).ceiling(9)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=20, length=12)
        .add_room("cl", T.CLOSET, x=20, y=0, width=depth, length=12)
        .entrance("bed", "south", width=3, offset=8)
        .add_window("bed", "north", width=4, offset=8)
    )
    b.connect("bed", "cl", width=2.5, kind=kind,
              swing_into="cl" if into_closet else "bed")
    return b


def test_swing_into_shallow_closet_is_flagged():
    assert "CLOSET_DOOR_SWING" in _codes(_closet_plan(2.0))


def test_deep_closet_swing_is_fine():
    assert "CLOSET_DOOR_SWING" not in _codes(_closet_plan(4.0))


def test_swing_away_from_the_closet_is_fine():
    # The leaf opens into the bedroom, so the closet depth doesn't bind it.
    assert "CLOSET_DOOR_SWING" not in _codes(_closet_plan(2.0, into_closet=False))


def test_sliding_closet_door_is_fine():
    assert "CLOSET_DOOR_SWING" not in _codes(_closet_plan(2.0, kind="sliding"))


# --- kitchen auto-seed spread ------------------------------------------------


def test_kitchen_seed_spreads_appliances_with_landing_space():
    plan = (
        barndominium("x").envelope(14, 14).ceiling(9)
        .add_room("kit", T.KITCHEN, x=0, y=0, width=14, length=14)
        .entrance("kit", "south", width=3, offset=1)
    )
    room = plan.room("kit")
    seeds = {f.kind: f for f in resolve_room_fixtures(plan, room)}
    assert set(seeds) == {"range", "sink", "refrigerator"}
    # The refrigerator no longer butts straight against the range.
    rng, fridge = seeds["range"], seeds["refrigerator"]
    gap = fridge.x - (rng.x + rng.width)
    assert gap > 1.0  # a landing bay between the cooktop and the fridge


def test_seeded_kitchen_stays_clear_of_landing_and_triangle_nags():
    plan = (
        barndominium("x").envelope(12, 12).ceiling(9)
        .add_room("kit", T.KITCHEN, x=0, y=0, width=12, length=12)
        .entrance("kit", "south", width=3, offset=1)
    )
    codes = _codes(plan)
    assert "RANGE_LANDING" not in codes
    assert "KITCHEN_TRIANGLE" not in codes


# --- registry coverage -------------------------------------------------------


def test_new_codes_are_registered():
    for code in (
        "WINDOW_FALL", "STAIR_LANDING", "DOOR_THRESHOLD",
        "RECEPTACLE_COUNTER", "CLOSET_DOOR_SWING",
    ):
        assert code in REGISTRY, code
