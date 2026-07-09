"""Structural circulation-topology defects: the garage/shop pass-through.

A plan can compile 0/0/0 and still be *structurally* broken — the whole reason
this suite exists. The motivating case (a real solver run) scored 99.3/100 while
the only interior route from the living core to every bedroom crossed a full-width
shop band; two bedrooms were 2:1 tunnels; and a 5×16 rectangle of the envelope was
unassigned. These tests pin the machinery that now *sees* those flaws:

- GARAGE_PASSTHROUGH — a WARNING when the garage/shop is a cut vertex between the
  public core and one or more bedrooms (validation._dq_garage_passthrough).
- it is on the accept-denylist (pragma.ACCEPT_DENIED_CODES), so a
  `# barndsl: accept GARAGE_PASSTHROUGH` can't silence it out of the score.
- the continuous `topology` score component (score._topology_penalty), redundant
  with the warning so the defect can't be capped away.
- AREA_VOID — a concentrated unassigned void even at high coverage.
- the per-type ROOM_PROPORTION threshold (bedrooms ~1.8:1) and the worst-dominated
  proportion score term.
"""

from __future__ import annotations

from barndsl import compile_source, design_score
from barndsl.pragma import ACCEPT_DENIED_CODES

# The exact failing plan from the motivating run. It compiles with zero errors and
# scored 99.3/100 before topology awareness. The two `# barndsl: accept GARAGE_DOOR`
# pragmas are load-bearing — they downgrade the fire-rated-door reminders, which is
# what let the old score reach 99.3; keep them verbatim.
FAILING = """\
plan "Barndominium 3-Bed"
envelope 60 x 48
ceiling 10
program 3 bed 2 bath area 2000

room living:  living   at 0,0   size 25 x 16
room kitchen: kitchen  at 25,0  size 18 x 16
room dining:  dining   at 43,0  size 17 x 16
room shop:    shop     at 0,16  size 60 x 12
room hall:    hallway  at 0,28  size 55 x 4
room linen:   closet   at 55,28 size 5 x 4

room bed2:    bedroom  at 0,32  size 8 x 16
room closet2: closet   at 8,32  size 5 x 16
room mcloset: closet   at 13,32 size 5 x 16
room master:  bedroom  at 18,32 size 12 x 16
room mbath:   bathroom at 30,32 size 6 x 16
room bath2:   bathroom at 36,32 size 6 x 16
room bed3:    bedroom  at 42,32 size 8 x 16
room closet3: closet   at 50,32 size 5 x 16

open living - kitchen width 8
open kitchen - dining width 8

door hall - master width 2.67 offset 0.5 into master
door hall - bed2 width 2.67 offset 0.5 into bed2
door hall - bed3 width 2.67 offset 0.5 into bed3
door hall - bath2 width 2.67 offset 0.5 into bath2
door hall - linen width 2.5 offset 0.5 into linen

door master - mbath width 2.5 offset 0.5 into mbath
door master - mcloset width 2.5 offset 10 into master
door bed2 - closet2 width 2.5 offset 0.5 into bed2
door bed3 - closet3 width 2.5 offset 0.5 into bed3

door hall - shop width 2.67 offset 25  # barndsl: accept GARAGE_DOOR
door living - shop width 2.67 offset 0.5  # barndsl: accept GARAGE_DOOR

entry living south width 3 offset 0.5
entry dining east width 3 offset 0.5

porch front at -2,-6 size 7 x 6 covered
porch back at 58,0 size 6 x 6 covered

window living south width 10 offset 6
window living west width 5 offset 5.5
window kitchen south width 7 offset 5.5
window dining south width 6 offset 5.5
window dining east width 4 offset 10
window master north width 5 offset 3.5
window bed2 north width 4 offset 2
window bed3 north width 4 offset 2
window mbath north width 2.5 offset 1.75 sill 5 tempered
window bath2 north width 2.5 offset 1.75 sill 5 tempered

wall shop - living rated
wall shop - kitchen rated
wall shop - dining rated
wall shop - hall rated
wall shop - linen rated

alarm smoke in master
alarm smoke in bed2
alarm smoke in bed3
alarm smoke in hall
alarm smoke_co in hall
"""

# A good hall-spine plan: the shop is a dead-end bay on the WEST gable end (an
# exterior wall for its overhead door), reached off a mudroom off the public core.
# The bedrooms hang off a hall that reaches the living core without ever touching
# the shop — so no structural circulation defect exists. Scores clean.
GOOD = """\
plan "Good Shop Barndo"
envelope 60 x 32
ceiling 10
program 2 bed 1 bath

room shop:    shop     at 0,0   size 20 x 32
room living:  living   at 20,0  size 22 x 16
room kitchen: kitchen  at 42,0  size 18 x 16
room hall:    hallway  at 20,16 size 40 x 4
room mud:     mudroom  at 20,20 size 8 x 12
room bed1:    bedroom  at 28,20 size 10 x 12
room cl1:     closet   at 38,20 size 4 x 12
room bed2:    bedroom  at 42,20 size 8 x 12
room cl2:     closet   at 50,20 size 4 x 12
room bath:    bathroom at 54,20 size 6 x 12

open living - kitchen width 8
door living - hall width 3 offset 2
door hall - mud width 2.67 offset 0.5 into mud
door hall - bed1 width 2.67 offset 0.5 into bed1
door hall - bed2 width 2.67 offset 0.5 into bed2
door hall - bath width 2.67 offset 0.5 into bath
door bed1 - cl1 width 2.5 offset 0.5 into bed1
door bed2 - cl2 width 2.5 offset 0.5 into bed2
door mud - shop width 2.67 offset 4  # barndsl: accept GARAGE_DOOR

entry living south width 3 offset 4
entry kitchen south width 3 offset 2

window living south width 8 offset 12
window kitchen south width 8 offset 8
window bed1 north width 4 offset 3
window bed2 north width 4 offset 3
window bath east width 2.5 offset 4 sill 5 tempered
window mud north width 3 offset 2

wall shop - living rated
wall shop - mud rated

alarm smoke in bed1
alarm smoke in bed2
alarm smoke in hall
alarm smoke_co in hall

porch p1 at 24,-6 size 6 x 6 covered
porch p2 at 43,-6 size 6 x 6 covered
"""


def _codes(diags, *, accepted=None):
    """Codes present, optionally filtered by accepted state."""
    out = set()
    for d in diags:
        if accepted is None or bool(getattr(d, "accepted", False)) == accepted:
            out.add(d.code)
    return out


def _warnings(result):
    return {d.code for d in result.warnings}


# --- the motivating regression ----------------------------------------------


def test_failing_plan_fires_garage_passthrough_and_void_error():
    # Historically this plan compiled 0/0/0 and scored 99.3 — that was the whole
    # problem. Today its 80 sqft dead pocket is an AREA_VOID *error* (a house has
    # no void areas) and the shop-as-corridor is a GARAGE_PASSTHROUGH warning.
    result = compile_source(FAILING)
    assert result.plan is not None
    assert [d.code for d in result.errors] == ["AREA_VOID"]
    assert "GARAGE_PASSTHROUGH" in _warnings(result)


def test_failing_plan_passthrough_names_every_severed_bedroom():
    result = compile_source(FAILING)
    gp = next(d for d in result.warnings if d.code == "GARAGE_PASSTHROUGH")
    # All three bedrooms are cut off from the public core by the shop band.
    for bed in ("bed2", "bed3", "master"):
        assert f"'{bed}'" in gp.message


def test_failing_plan_scores_below_80():
    # The old score was 99.3. The warning (-8), the topology term (-15, all three
    # bedrooms severed), the void, and the tunnel-bedroom proportion all bite now.
    report = design_score(compile_source(FAILING))
    assert report.total < 80.0, report.to_dict()
    assert report.components["topology"] > 0


def test_failing_plan_passthrough_cannot_be_accepted_away():
    # Try to waive it by anchoring an accept pragma on the severed bedroom's line
    # (bed2 sorts first, so that's where the diagnostic anchors and a user would
    # place the pragma). The denylist must refuse it.
    injected = FAILING.replace(
        "room bed2:    bedroom  at 0,32  size 8 x 16",
        "room bed2:    bedroom  at 0,32  size 8 x 16  # barndsl: accept GARAGE_PASSTHROUGH",
    )
    result = compile_source(injected)
    # Still an active (unaccepted) warning, and the pragma drew an ACCEPT_DENIED.
    assert "GARAGE_PASSTHROUGH" in _warnings(result)
    assert "GARAGE_PASSTHROUGH" not in _codes(result.diagnostics, accepted=True)
    assert "ACCEPT_DENIED" in {d.code for d in result.diagnostics}
    # And the topology deduction survives, so the score can't be rescued.
    report = design_score(result)
    assert report.components["topology"] > 0
    assert report.total < 80.0


def test_garage_passthrough_is_on_the_accept_denylist():
    assert "GARAGE_PASSTHROUGH" in ACCEPT_DENIED_CODES


# --- the good plan (negative case) ------------------------------------------


def test_good_shop_plan_has_no_structural_defect():
    result = compile_source(GOOD)
    assert result.plan is not None and not result.errors
    # None of the structural checks fire on a properly buffered gable-end shop.
    codes = {d.code for d in result.diagnostics}
    assert "GARAGE_PASSTHROUGH" not in codes
    assert "AREA_VOID" not in codes
    assert "ROOM_PROPORTION" not in codes


def test_good_shop_plan_scores_clean():
    report = design_score(compile_source(GOOD))
    assert report.components["topology"] == 0.0
    assert report.components["proportion"] == 0.0
    assert report.total >= 85.0, report.to_dict()


# --- unit: _dq_garage_passthrough in isolation ------------------------------


def _ctx(plan):
    from barndsl.validation import _door_graph

    return _door_graph(plan), {r.id: r for r in plan.rooms}


def test_passthrough_check_isolated_fires_on_cut_vertex_shop():
    from barndsl.validation import _dq_garage_passthrough

    plan = compile_source(FAILING).plan
    graph, by_id = _ctx(plan)
    found = []
    _dq_garage_passthrough(plan, graph, by_id, found.append)
    assert {i.code for i in found} == {"GARAGE_PASSTHROUGH"}


def test_passthrough_check_isolated_silent_on_buffered_shop():
    from barndsl.validation import _dq_garage_passthrough

    plan = compile_source(GOOD).plan
    graph, by_id = _ctx(plan)
    found = []
    _dq_garage_passthrough(plan, graph, by_id, found.append)
    assert found == []


def test_passthrough_silent_when_no_garage():
    # A garage-less plan can never fire it (guard: no garages → return).
    from barndsl.validation import _dq_garage_passthrough

    src = """\
plan "No shop"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 18 x 24
room hall:   hallway at 18,0 size 4 x 24
room bed:    bedroom at 22,0 size 8 x 24
door living - hall width 3
door hall - bed width 2.67 into bed
entry living south width 3 offset 4
window living south width 6 offset 6
window bed south width 4 offset 2
"""
    plan = compile_source(src).plan
    graph, by_id = _ctx(plan)
    found = []
    _dq_garage_passthrough(plan, graph, by_id, found.append)
    assert found == []


def test_detached_shop_with_no_door_is_not_a_cut_vertex():
    # A shop that abuts the house but has NO interior door isn't on any route, so
    # removing it can't sever a bedroom — it must not fire (the guard the spec
    # calls out explicitly).
    from barndsl.validation import _dq_garage_passthrough

    src = """\
plan "Detached shop"
envelope 40 x 28
ceiling 10
room shop:   shop    at 0,0   size 16 x 28
room living: living  at 16,0  size 24 x 14
room hall:   hallway at 16,14 size 24 x 4
room bed:    bedroom at 16,18 size 24 x 10
open living - hall width 4
door hall - bed width 2.67 into bed
entry living south width 3 offset 6
entry shop west width 9 offset 8 overhead
window living south width 8 offset 8
window bed north width 5 offset 8
"""
    plan = compile_source(src).plan
    graph, by_id = _ctx(plan)
    found = []
    _dq_garage_passthrough(plan, graph, by_id, found.append)
    assert found == []


# --- unit: the topology score component -------------------------------------


def test_topology_penalty_full_when_all_bedrooms_severed():
    from barndsl.score import TOPOLOGY_WEIGHT, _topology_penalty

    plan = compile_source(FAILING).plan
    points, cause = _topology_penalty(plan)
    # All 3 of 3 bedrooms are severed → the full weight.
    assert points == TOPOLOGY_WEIGHT
    assert cause is not None and "3 of 3" in cause


def test_topology_penalty_zero_on_buffered_shop():
    from barndsl.score import _topology_penalty

    plan = compile_source(GOOD).plan
    points, cause = _topology_penalty(plan)
    assert points == 0.0 and cause is None


def test_topology_penalty_is_proportional_to_severed_fraction():
    # Two bedrooms, only one reachable only through the shop → half the weight.
    from barndsl.score import TOPOLOGY_WEIGHT, _topology_penalty

    src = """\
plan "Half severed"
envelope 44 x 32
ceiling 10
room living: living  at 0,0   size 20 x 16
room bed1:   bedroom at 20,0  size 12 x 16
room shop:   shop    at 0,16  size 24 x 16
room hall:   hallway at 24,16 size 20 x 4
room bed2:   bedroom at 24,20 size 20 x 12
open living - bed1 width 4
door living - shop width 2.67 offset 2
door shop - hall width 2.67 offset 2
door hall - bed2 width 2.67 into bed2
entry living south width 3 offset 6
window living south width 6 offset 6
window bed1 south width 4 offset 4
window bed2 north width 5 offset 8
"""
    plan = compile_source(src).plan
    points, cause = _topology_penalty(plan)
    # bed1 reaches the core directly (open living-bed1); bed2 only through the shop.
    assert points == TOPOLOGY_WEIGHT * 0.5
    assert "1 of 2" in cause


# --- unit: concentrated void (AREA_VOID + space term) -----------------------


def test_area_void_is_an_error_on_concentrated_gap_at_high_coverage():
    # ~97% covered overall, but one 96 sqft rectangle (x 44-60, y 0-6) is a room-
    # sized hole AREA_UNUSED (below-85%-only) never sees. In a substantially
    # tiled plan that hole is an ERROR — there are no void areas in a house.
    src = """\
plan "Pocket"
envelope 60 x 30
ceiling 9
room living: living  at 0,0   size 44 x 30
room kitchen: kitchen at 44,6  size 16 x 24
entry living south width 3 offset 6
window living south width 8 offset 8
window kitchen east width 5 offset 8
"""
    result = compile_source(src)
    assert "AREA_VOID" in {d.code for d in result.errors}
    all_codes = {d.code for d in result.diagnostics}
    assert "AREA_UNUSED" not in all_codes  # coverage is high; only the void speaks
    void = next(d for d in result.errors if d.code == "AREA_VOID")
    assert "44" in void.message and "60" in void.message  # names the gap's bbox


def test_area_void_stays_info_in_a_sparse_sketch():
    # Under 85% coverage the plan is an unfinished sketch AREA_UNUSED is already
    # nagging — the concentrated void doesn't escalate to an error there.
    src = """\
plan "Sketch"
envelope 60 x 30
ceiling 9
room living: living at 0,0 size 30 x 30
entry living south width 3 offset 6
window living south width 10 offset 14
"""
    result = compile_source(src)
    assert not result.errors
    codes_info = {d.code for d in result.infos}
    assert "AREA_VOID" in codes_info
    assert "AREA_UNUSED" in codes_info


def test_area_void_small_pocket_is_an_info_nudge():
    # A pocket between the note bar (20 sqft) and the error bar (45 sqft) in a
    # well-tiled plan: visible, but not blocking.
    src = """\
plan "Small pocket"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 18 x 24
room bed:    bedroom at 18,0 size 12 x 21
door living - bed width 2.67 into bed
entry living south width 3 offset 2
window living south width 8 offset 8
window bed south width 4 offset 4
"""
    result = compile_source(src)  # void: 12 x 3 = 36 sqft at (18,21)
    assert not result.errors
    assert "AREA_VOID" in {d.code for d in result.infos}


def test_area_void_silent_on_fully_tiled_plan():
    from barndsl.validation import MIN_CONCENTRATED_VOID, _largest_void

    src = """\
plan "Tiled"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 18 x 24
room bed:    bedroom at 18,0 size 12 x 24
door living - bed width 2.67 into bed
entry living south width 3 offset 4
window living south width 6 offset 8
window bed south width 4 offset 3
"""
    result = compile_source(src)
    assert "AREA_VOID" not in {d.code for d in result.infos}
    area, bbox = _largest_void(result.plan)
    assert area < MIN_CONCENTRATED_VOID


def test_space_term_charges_for_a_void_above_85_percent_coverage():
    # The same high-coverage pocket: the coverage term would forgive it (>85%),
    # but the concentrated-void term charges something anyway.
    src = """\
plan "Pocket"
envelope 60 x 30
ceiling 9
room living: living  at 0,0   size 44 x 30
room kitchen: kitchen at 44,6  size 16 x 24
entry living south width 3 offset 6
window living south width 8 offset 8
window kitchen east width 5 offset 8
"""
    report = design_score(compile_source(src))
    assert report.components["space"] > 0
    assert "void" in report.details["space"]


# --- unit: per-type proportion (ROOM_PROPORTION + score term) ---------------


def test_tunnel_bedroom_fires_room_proportion_at_2_to_1():
    # An 8×16 bedroom is 2.0:1 — under the generic 3:1 bar but over the 1.8:1
    # bedroom-specific one, so it now nudges where it used to stay silent.
    src = """\
plan "Tunnel bed"
envelope 30 x 16
ceiling 9
room living: living  at 0,0  size 22 x 16
room bed:    bedroom at 22,0 size 8 x 16
door living - bed width 2.67 into bed
entry living south width 3 offset 6
window living south width 8 offset 8
window bed north width 4 offset 2
"""
    result = compile_source(src)
    props = [d for d in result.infos if d.code == "ROOM_PROPORTION"]
    assert props and any("bed" in d.message and "2.0:1" in d.message for d in props)


def test_proportion_thresholds_track_the_room_type():
    # Bedroom AND office get the tight 1.8:1 cap (each holds one big furniture
    # piece plus a walk-around, and the score already dings past 1.6:1); dining
    # keeps the generic 3:1 default, so the same 2.0:1 shape stays silent as a
    # dining room while flagging as an office or a bedroom.
    src = """\
plan "Mixed proportions"
envelope 48 x 16
ceiling 9
room living: living at 0,0   size 16 x 16
room dining: dining at 16,0  size 8 x 16
room office: office at 24,0  size 8 x 16
room bed:    bedroom at 32,0 size 8 x 16
door living - dining width 2.67
door dining - office width 2.67
door office - bed width 2.67 into bed
entry living south width 3 offset 6
window living south width 6 offset 6
window dining south width 4 offset 2
window office south width 4 offset 2
window bed north width 4 offset 2
"""
    result = compile_source(src)
    props = {d.room for d in result.infos if d.code == "ROOM_PROPORTION"}
    assert "bed" in props
    assert "office" in props
    assert "dining" not in props


def test_proportion_score_is_worst_dominated_not_diluted():
    # Two 2:1 tunnel bedrooms among many square rooms. The old mean diluted them
    # to near-nothing; worst-dominated + bedroom-2x keeps the penalty visible.
    from barndsl.elements import Barndominium, Room, RoomType as T
    from barndsl.score import _proportion_penalty

    def plan_with_tunnels():
        p = Barndominium("p", 60, 40, 10)
        # five square habitable rooms + two 2:1 tunnel bedrooms
        for i in range(5):
            p.rooms.append(Room(f"sq{i}", T.LIVING, 0, i * 8, 12, 12))
        p.rooms.append(Room("t1", T.BEDROOM, 20, 0, 8, 16))
        p.rooms.append(Room("t2", T.BEDROOM, 30, 0, 8, 16))
        return p

    points, cause = _proportion_penalty(plan_with_tunnels())
    # Diluted mean would be ~0.6; worst-dominated keeps it materially higher.
    assert points > 1.5
    assert cause is not None
