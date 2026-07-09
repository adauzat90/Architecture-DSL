"""Structural tests for the design-quality check registry.

These exercise the seam introduced when the 684-line ``_validate_design_quality``
was split into one ``_dq_*`` function per check, driven by
``_DESIGN_QUALITY_CHECKS``. They assert the registry stays well-formed and, more
importantly, that an individual check can now be run *in isolation* — the whole
point of the refactor.
"""

from __future__ import annotations

from barndsl import compile_source
from barndsl.elements import Barndominium
from barndsl.validation import (
    _DESIGN_QUALITY_CHECKS,
    _door_graph,
    _dq_kitchen_flow,
    _validate_design_quality,
)


def _ctx(plan: Barndominium):
    return _door_graph(plan), {r.id: r for r in plan.rooms}


def test_registry_is_well_formed():
    # Every entry is a distinct callable named `_dq_*`.
    names = [c.__name__ for c in _DESIGN_QUALITY_CHECKS]
    assert len(names) == len(set(names)), "duplicate check in registry"
    assert all(n.startswith("_dq_") for n in names)
    assert len(_DESIGN_QUALITY_CHECKS) == 28


def test_single_check_runs_in_isolation():
    # A kitchen with no path to dining/living should trip exactly KITCHEN_FLOW
    # when only that one check is invoked — no need to run the whole pipeline.
    src = """\
plan "Closed kitchen"
envelope 30 x 20
ceiling 9
room living:  living  at 0,0   size 18 x 20
room kitchen: kitchen at 18,0  size 12 x 20
entry living south width 3 offset 6
window living west width 6 offset 6
window kitchen east width 6 offset 6
"""
    plan = compile_source(src).plan
    graph, by_id = _ctx(plan)

    found = []
    _dq_kitchen_flow(plan, graph, by_id, found.append)

    codes = {i.code for i in found}
    assert codes == {"KITCHEN_FLOW"}, codes


def test_driver_equals_sum_of_checks():
    # Running the driver must produce exactly the union of running each check
    # individually — i.e. the split changed structure, not behaviour.
    src = """\
plan "Mixed"
envelope 40 x 30
ceiling 9
room living:  living   at 0,0   size 16 x 30
room kitchen: kitchen  at 16,0  size 12 x 12
room bath:    bathroom at 28,18 size 12 x 12
room laundry: laundry  at 16,18 size 10 x 12
room hall:    hallway  at 16,12 size 24 x 6
door living - hall width 3
door living - kitchen width 6
door hall - bath width 2.67
door hall - laundry width 2.67
entry living south width 3 offset 6
window bath east width 3 offset 4
"""
    plan = compile_source(src).plan
    graph, by_id = _ctx(plan)

    via_driver = []
    _validate_design_quality(plan, via_driver.append)

    via_checks = []
    for check in _DESIGN_QUALITY_CHECKS:
        check(plan, graph, by_id, via_checks.append)

    key = lambda i: (i.code, i.room, i.message)  # noqa: E731
    assert sorted(map(key, via_driver)) == sorted(map(key, via_checks))
