"""CLI `design`: solver-seeding wiring and step reporting.

These tests never touch the network. `_cmd_design` imports `BarndoAgent` and
`agent_availability` from `barndsl.agent` *at call time*, so monkeypatching those
names on the module is enough to swap in a fake agent — the API is never reached.
The fake's `design()` records the kwargs the CLI passes (so we can assert what
`seed_with_solver` was set to) and replays a scripted set of steps through the
supplied `on_step`, so the printed step lines are exercised for real.
"""

from __future__ import annotations

import barndsl.agent as agent_mod
from barndsl import compile_source
from barndsl.agent import CritiqueSpec, DesignStep
from barndsl.cli import main
from barndsl.scaffold import starter_dsl
from barndsl.score import design_score

# A verified-clean plan (0/0/0) to back the fake steps with a real CompileResult.
CLEAN = starter_dsl("Seed CLI Test")


class _FakeResult:
    """The minimal DesignResult surface `_cmd_design` reads after the loop.

    plan is None on purpose so the command skips metrics/SVG output — the tests
    are about the seed wiring and step reporting, not rendering.
    """

    def __init__(self, steps: list[DesignStep]) -> None:
        self.history = steps
        self.iterations = len(steps)
        self.best_iteration = steps[-1].iteration if steps else 0
        self.result = compile_source(CLEAN, name=None)
        self.source = self.result.source
        self.score = design_score(self.result)
        self.plan = None  # skip _print_metrics/save_svg


class _FakeAgent:
    """Stands in for BarndoAgent: no client, no API. Records design() kwargs and
    replays scripted steps through on_step so the CLI's reporting runs for real."""

    calls: list[dict] = []

    def __init__(self, *, steps: list[DesignStep] | None = None, model=None, **_):
        self._steps = steps if steps is not None else [
            DesignStep(
                1, CLEAN, compile_source(CLEAN, name=None), _satisfied(),
                design_score(compile_source(CLEAN, name=None)),
            )
        ]

    def design(self, brief, **kwargs):
        _FakeAgent.calls.append({"brief": brief, **kwargs})
        on_step = kwargs.get("on_step")
        if on_step is not None:
            for step in self._steps:
                on_step(step)
        return _FakeResult(self._steps)


def _satisfied() -> CritiqueSpec:
    return CritiqueSpec(satisfied=True, assessment="Looks good.", rationale="clean")


def _install_fake_agent(monkeypatch, steps=None) -> None:
    """Wire the fake agent + an always-available probe onto barndsl.agent."""
    _FakeAgent.calls = []

    def _factory(*args, **kwargs):
        return _FakeAgent(steps=steps, **kwargs)

    monkeypatch.setattr(agent_mod, "agent_availability", lambda: (True, None))
    monkeypatch.setattr(agent_mod, "BarndoAgent", _factory)


# -- (a) default run seeds the solver with the brief text ------------------


def test_design_defaults_to_seeding_the_solver_with_the_brief(monkeypatch, capsys):
    _install_fake_agent(monkeypatch)
    rc = main(["design", "a small cottage", "--out", "unused.svg"])
    assert rc == 0
    capsys.readouterr()  # drain
    assert len(_FakeAgent.calls) == 1
    call = _FakeAgent.calls[0]
    # The same brief string the command assembled rides along as the solver seed.
    assert call["seed_with_solver"] == "a small cottage"
    assert call["brief"] == "a small cottage"


# -- (b) --no-seed-solver disables seeding ---------------------------------


def test_no_seed_solver_passes_a_falsy_seed(monkeypatch, capsys):
    _install_fake_agent(monkeypatch)
    rc = main(["design", "a small cottage", "--no-seed-solver", "--out", "unused.svg"])
    assert rc == 0
    capsys.readouterr()
    call = _FakeAgent.calls[0]
    assert not call["seed_with_solver"]  # None / falsy — seeding off


# -- iteration 0 is labelled as the solver seed, not a model round ---------


def test_iteration_zero_prints_as_the_solver_seed(monkeypatch, capsys):
    seed_step = DesignStep(
        0, CLEAN, compile_source(CLEAN, name=None), None,
        design_score(compile_source(CLEAN, name=None)),
    )
    round_step = DesignStep(
        1, CLEAN, compile_source(CLEAN, name=None), _satisfied(),
        design_score(compile_source(CLEAN, name=None)),
    )
    _install_fake_agent(monkeypatch, steps=[seed_step, round_step])
    assert main(["design", "a cottage", "--out", "unused.svg"]) == 0
    out = capsys.readouterr().out
    assert "solver seed:" in out
    assert "iteration 1:" in out
    # Iteration 0 must not be printed as "iteration 0" (would read as a model round).
    assert "iteration 0:" not in out


# -- (c) a degraded critique prints a one-line notice ----------------------


def test_skipped_critique_prints_a_notice(monkeypatch, capsys):
    skipped = CritiqueSpec(
        satisfied=False,
        assessment="(critique skipped: the critique call failed)",
        rationale="",
    )
    step = DesignStep(
        1, CLEAN, compile_source(CLEAN, name=None), skipped,
        design_score(compile_source(CLEAN, name=None)),
    )
    _install_fake_agent(monkeypatch, steps=[step])
    assert main(["design", "a cottage", "--out", "unused.svg"]) == 0
    out = capsys.readouterr().out
    assert "critique unavailable" in out
    assert "(critique skipped" in out


def test_normal_critique_prints_no_skipped_notice(monkeypatch, capsys):
    step = DesignStep(
        1, CLEAN, compile_source(CLEAN, name=None), _satisfied(),
        design_score(compile_source(CLEAN, name=None)),
    )
    _install_fake_agent(monkeypatch, steps=[step])
    assert main(["design", "a cottage", "--out", "unused.svg"]) == 0
    out = capsys.readouterr().out
    assert "critique unavailable" not in out


# -- (d) a brief the solver couldn't seed prints a visible note ------------


def _round_only_step() -> DesignStep:
    """A single model round with no iteration-0 solver seed in the history."""
    return DesignStep(
        1, CLEAN, compile_source(CLEAN, name=None), _satisfied(),
        design_score(compile_source(CLEAN, name=None)),
    )


def test_no_seed_note_prints_when_seeding_on_but_history_lacks_iteration_zero(
    monkeypatch, capsys
):
    # Seeding is on by default; the fake history has only iteration 1 (the solver
    # drafted nothing), so the "could not draft a seed" note must surface — the
    # bug this closes was that degradation was silent.
    _install_fake_agent(monkeypatch, steps=[_round_only_step()])
    assert main(["design", "a vague barndo idea", "--out", "unused.svg"]) == 0
    out = capsys.readouterr().out
    assert "could not draft a seed" in out


def test_no_seed_note_absent_with_no_seed_solver(monkeypatch, capsys):
    # Seeding was explicitly disabled, so a missing iteration 0 is expected, not a
    # degradation — the note must stay silent.
    _install_fake_agent(monkeypatch, steps=[_round_only_step()])
    assert main(
        ["design", "a vague barndo idea", "--no-seed-solver", "--out", "unused.svg"]
    ) == 0
    out = capsys.readouterr().out
    assert "could not draft a seed" not in out


def test_no_seed_note_absent_when_iteration_zero_present(monkeypatch, capsys):
    # The solver did seed (iteration 0 present), so seeding succeeded — no note.
    seed_step = DesignStep(
        0, CLEAN, compile_source(CLEAN, name=None), None,
        design_score(compile_source(CLEAN, name=None)),
    )
    _install_fake_agent(monkeypatch, steps=[seed_step, _round_only_step()])
    assert main(["design", "3 bed 2 bath 2000 sqft", "--out", "unused.svg"]) == 0
    out = capsys.readouterr().out
    assert "could not draft a seed" not in out
