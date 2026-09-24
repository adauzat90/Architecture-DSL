"""The check registry: every validator check is a ``check(ctx, add)`` listed once,
in run order, in ``_SHELL_CHECKS`` or ``_PLAN_CHECKS`` (TD-6).

It replaced two hand-kept dispatchers (a function calling the whole-plan checks
one by one, and a design-quality tuple with its own driver) and a special case
that handed the profile to one design-quality check. Checks read the profile
from their :class:`CheckContext`, so none can fall back to the IRC baseline by
a dropped argument.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

from barndsl import compile_source, validation
from barndsl.elements import Barndominium
from barndsl.profiles import DEFAULT, get_profile
from barndsl.validation import (
    CheckContext,
    _dq_kitchen_flow,
    _dq_kitchen_passthrough,
)

REGISTERED = validation._SHELL_CHECKS + validation._PLAN_CHECKS
SOURCE = Path(validation.__file__).read_text(encoding="utf-8")


def _ctx(plan: Barndominium) -> CheckContext:
    return CheckContext(plan, DEFAULT)


def test_every_check_is_registered_once():
    # A check that takes a CheckContext but isn't listed never runs: that's the
    # failure a hand-kept dispatcher allowed.
    names = [c.__name__ for c in REGISTERED]
    assert len(names) == len(set(names)), "a check is listed twice"

    def is_check(fn: ast.FunctionDef) -> bool:
        if not fn.args.args:
            return False
        first = fn.args.args[0]
        note = ast.unparse(first.annotation).strip("'\"") if first.annotation else ""
        return first.arg == "ctx" or note == "CheckContext"

    takes_ctx = {
        node.name for node in ast.parse(SOURCE).body
        if isinstance(node, ast.FunctionDef) and is_check(node)
    }
    assert takes_ctx == set(names)


def test_every_registered_check_takes_ctx_and_add():
    for check in REGISTERED:
        assert list(inspect.signature(check).parameters) == ["ctx", "add"], check.__name__


def test_nothing_defaults_the_profile():
    # The profile comes from the context, which requires one; no function in the
    # validator may quietly fall back to the IRC baseline. validate() is the
    # one entry point that turns None into DEFAULT, on purpose.
    assert all(f.default is dataclasses.MISSING for f in dataclasses.fields(CheckContext))
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name == "validate":
            continue
        args = node.args.args
        defaulted = [a.arg for a in args[len(args) - len(node.args.defaults):]]
        defaulted += [a.arg for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d is not None]
        assert "profile" not in defaulted, node.name
    # Nor may a body reach for the baseline itself (`profile or DEFAULT`,
    # `DEFAULT.min_…`): only validate() and _amended, which compares a profile
    # against the baseline to say when a threshold was amended, name it.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) or getattr(node, "name", None) in ("validate", "_amended"):
            continue
        uses = [n for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == "DEFAULT"]
        assert not uses, getattr(node, "name", ast.unparse(node)[:60])


def test_no_check_can_swap_the_shared_context():
    ctx = CheckContext(Barndominium(name="P"), DEFAULT)
    try:
        ctx.profile = get_profile("strict")  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("CheckContext must be frozen")
    assert ctx.graph == {} and ctx.by_id == {}  # the cached state still works


def test_validate_runs_every_check_in_order_with_the_callers_profile(monkeypatch):
    plan = compile_source('plan "P"\nenvelope 20 x 20\nroom a: living at 0,0 size 20 x 20\n').plan
    strict = get_profile("strict")
    seen: list[tuple[str, object, object]] = []

    def stub(name):
        def check(ctx, add):
            seen.append((name, ctx.plan, ctx.profile))
        return check

    monkeypatch.setattr(validation, "_SHELL_CHECKS", (stub("s1"), stub("s2")))
    monkeypatch.setattr(validation, "_PLAN_CHECKS", (stub("p1"), stub("p2"), stub("p3")))

    validation.validate(plan, strict)
    assert seen == [(n, plan, strict) for n in ("s1", "s2", "p1", "p2", "p3")]

    seen.clear()
    validation.validate(plan)
    assert [p for _, _, p in seen] == [DEFAULT] * 5


def test_an_empty_plan_stops_after_the_shell_checks(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(validation, "_SHELL_CHECKS", (lambda ctx, add: seen.append("shell"),))
    monkeypatch.setattr(validation, "_PLAN_CHECKS", (lambda ctx, add: seen.append("plan"),))
    report = validation.validate(Barndominium(name="Empty", envelope_width=20, envelope_length=20))
    assert seen == ["shell"]
    assert [i.code for i in report.issues] == ["EMPTY"]


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
    found = []
    _dq_kitchen_flow(_ctx(plan), found.append)

    codes = {i.code for i in found}
    assert codes == {"KITCHEN_FLOW"}, codes


def test_kitchen_passthrough_warns_when_kitchen_is_public_corridor():
    src = """\
plan "Kitchen corridor"
envelope 36 x 20
ceiling 9
room great:   great_room at 0,0  size 12 x 20
room kitchen: kitchen    at 12,0 size 12 x 20
room dining:  dining     at 24,0 size 12 x 20
open great - kitchen width 8
open kitchen - dining width 8
entry great south width 3 offset 4
window great south width 6 offset 4
window kitchen south width 4 offset 4
window dining south width 6 offset 4
"""
    plan = compile_source(src).plan
    found = []
    _dq_kitchen_passthrough(_ctx(plan), found.append)

    assert [i.code for i in found] == ["KITCHEN_PASSTHROUGH"]
    assert "only route" in found[0].message


def test_kitchen_passthrough_is_silent_when_public_rooms_have_bypass():
    src = """\
plan "Kitchen bypass"
envelope 36 x 24
ceiling 9
room great:   great_room at 0,0  size 12 x 20
room kitchen: kitchen    at 12,0 size 12 x 20
room dining:  dining     at 24,0 size 12 x 20
room hall:    hallway    at 0,20 size 36 x 4
open great - kitchen width 8
open kitchen - dining width 8
open great - hall width 4
open dining - hall width 4
entry great south width 3 offset 4
window great south width 6 offset 4
window kitchen south width 4 offset 4
window dining south width 6 offset 4
"""
    plan = compile_source(src).plan
    found = []
    _dq_kitchen_passthrough(_ctx(plan), found.append)

    assert found == []


def test_the_context_derives_shared_state_once():
    plan = compile_source(
        'plan "P"\nenvelope 20 x 10\nroom a: living at 0,0 size 10 x 10\n'
        "room b: kitchen at 10,0 size 10 x 10\ndoor a - b width 3\n"
    ).plan
    ctx = _ctx(plan)
    assert ctx.graph == validation.door_graph(plan) == {"a": {"b"}, "b": {"a"}}
    assert ctx.by_id == {"a": plan.rooms[0], "b": plan.rooms[1]}
    assert ctx.graph is ctx.graph and ctx.by_id is ctx.by_id
