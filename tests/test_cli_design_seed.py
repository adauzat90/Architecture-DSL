"""CLI `design`: solver-seeding wiring and step reporting.

These tests never touch the network. `_cmd_design` imports `BarndoAgent` and
`agent_availability` from `barndsl.agent` *at call time*, so monkeypatching those
names on the module is enough to swap in a fake agent — the API is never reached.
The fake's `design()` records the kwargs the CLI passes (so we can assert what
`seed_with_solver` was set to) and replays a scripted set of steps through the
supplied `on_step`, so the printed step lines are exercised for real.
"""

from __future__ import annotations

from types import SimpleNamespace

import barndsl.agent as agent_mod
from barndsl import compile_source
from barndsl.agent import (
    BarndoAgent,
    CritiqueSpec,
    DesignStep,
    _brief_from_llm,
    _solver_candidate_sources,
    _solver_seed,
)
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
        self.termination_reason = "completed"
        self.review_degraded = any(
            step.critique is not None and step.critique.skipped for step in steps
        )


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
        skipped=True,
    )
    step = DesignStep(
        1, CLEAN, compile_source(CLEAN, name=None), skipped,
        design_score(compile_source(CLEAN, name=None)),
    )
    _install_fake_agent(monkeypatch, steps=[step])
    assert main(["design", "a cottage", "--out", "unused.svg"]) == 1
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


# ============================================================================
# Multi-seed exposure: alternate starts + the LLM-brief fallback (deliverables
# 2 & 3). These drive a real BarndoAgent with a scripted fake client and inspect
# the first write prompt / the seed-resolution chain — no network.
# ============================================================================


class _PromptRecordingClient:
    """A minimal `.messages.stream(...)` fake for BarndoAgent.

    Records the first-turn content of every call (`prompts`) and returns scripted
    replies. Generation calls (system is NOT the critique's "senior architect")
    pop `gen_replies`; critique calls return a satisfied JSON verdict; the
    LLM-brief call (system marker "You translate briefs") pops `brief_replies`.
    Everything the model sees rides `prompts` so a test can assert on it.
    """

    def __init__(self, gen_replies, brief_replies=None):
        self._gen = list(gen_replies)
        self._brief = list(brief_replies or [])
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.messages = self

    def stream(self, **kwargs):
        system = kwargs.get("system", "")
        content = kwargs["messages"][0]["content"]
        text = content if isinstance(content, str) else "".join(
            b["text"] for b in content if b.get("type") == "text"
        )
        self.prompts.append(text)
        self.systems.append(system)
        if "You translate briefs" in system:  # the LLM-brief call
            reply = self._brief.pop(0)
        elif "senior architect" in system:  # a critique call
            reply = '{"satisfied": true, "assessment": "ok", "rationale": "ok", "suggestions": []}'
        else:  # a generation call
            reply = f"```barn\n{self._gen.pop(0)}```"
        return _OneShotStream(reply)


class _OneShotStream:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason="end_turn",
        )


# A textual v2 brief whose three engines (bands/slice/dual) all compile — small
# enough (4 rooms) that dual runs and produces a structurally different tiling.
MULTI_ENGINE_BRIEF = (
    'plan "Alt Seed"\n'
    "room living: living area 300\n"
    "room kitchen: kitchen area 200\n"
    "room bed1: bedroom area 170\n"
    "room bath: bathroom area 70\n"
    "adjacent living kitchen\n"
    "adjacent living bed1\n"
    "adjacent bed1 bath\n"
)


def _first_write_prompt(client) -> str:
    """The first GENERATION prompt. The solver seed is critiqued before round 1
    now, so ``prompts[0]`` can be the seed-review call — select by content."""
    return next(p for p in client.prompts if "Design brief:" in p)


def test_alternates_appear_in_first_prompt_when_multiple_engines_compile():
    # Several engines compile this brief, so the first write prompt carries an
    # ALTERNATE STARTS section with the runner-up engine sources.
    seed = _solver_seed(MULTI_ENGINE_BRIEF)
    assert seed is not None and len(seed.alternates) >= 1

    client = _PromptRecordingClient(gen_replies=[CLEAN])
    BarndoAgent(client=client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=MULTI_ENGINE_BRIEF,
    )
    first = _first_write_prompt(client)
    assert "ALTERNATE STARTS (structurally different, also sound" in first
    # Each retained alternate's engine label and source appear.
    for alt in seed.alternates:
        assert f"[{alt.engine} engine, score" in first


def test_alternates_capped_at_two():
    seed = _solver_seed(MULTI_ENGINE_BRIEF)
    assert seed is not None
    assert len(seed.alternates) <= 2
    # The prompt shows at most two alternate blocks.
    client = _PromptRecordingClient(gen_replies=[CLEAN])
    BarndoAgent(client=client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=MULTI_ENGINE_BRIEF,
    )
    assert _first_write_prompt(client).count(" engine, score ") <= 2


def test_alternates_from_different_engines_than_the_winner():
    seed = _solver_seed(MULTI_ENGINE_BRIEF)
    assert seed is not None
    engines = [a.engine for a in seed.alternates]
    assert len(engines) == len(set(engines))  # no duplicate engine


def test_no_alternate_section_when_only_one_engine_succeeds():
    # A large program (>9 rooms) skips dual and often breaks slice, leaving only
    # the bands winner — so no ALTERNATE STARTS section.
    seed = _solver_seed("4 bed 3 bath 2600 sqft with a shop and an office")
    assert seed is not None
    if seed.alternates:  # if diversity does survive here, the test is not meaningful
        return
    client = _PromptRecordingClient(gen_replies=[CLEAN])
    BarndoAgent(client=client).design(
        "a big barndo", max_iterations=1, target_score=None,
        seed_with_solver="4 bed 3 bath 2600 sqft with a shop and an office",
    )
    assert "ALTERNATE STARTS" not in _first_write_prompt(client)


def test_new_seed_intro_wording_present_and_old_absent():
    client = _PromptRecordingClient(gen_replies=[CLEAN])
    BarndoAgent(client=client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=MULTI_ENGINE_BRIEF,
    )
    first = _first_write_prompt(client)
    # The new, permissive wording.
    assert "Start from these bones and improve the DESIGN" in first
    assert "the room arrangement is yours to improve" in first
    # The old anchoring wording is gone.
    assert "do NOT start from scratch and do not regress its geometry" not in first


# --- deliverable 3: the LLM-brief fallback ----------------------------------


# A valid brief2 the fake LLM "writes" for a bedless prose brief.
LLM_BRIEF_REPLY = (
    'plan "LLM Seed"\n'
    "ceiling 10\n"
    "room living: living area 300\n"
    "room kitchen: kitchen area 200\n"
    "room bed1: bedroom area 170\n"
    "room bath: bathroom area 80\n"
    "adjacent living kitchen\n"
    "adjacent living bed1\n"
    "adjacent bed1 bath\n"
    "entry living\n"
)

# Prose with NO bedroom-count signal, so it reaches the LLM path (parse_brief2
# and the regex both fail).
BEDLESS_PROSE = "a cozy place to live with a spot to cook and a room to sleep in"


def test_llm_brief_used_when_regex_fails_produces_a_solver_seed():
    # The bedless prose reaches the LLM; its scripted brief2 parses and seeds the
    # loop, so the first write prompt carries the solver seed.
    client = _PromptRecordingClient(gen_replies=[CLEAN], brief_replies=[LLM_BRIEF_REPLY])
    result = BarndoAgent(client=client).design(
        BEDLESS_PROSE, max_iterations=1, target_score=None,
        seed_with_solver=BEDLESS_PROSE,
    )
    # Iteration 0 is the solver seed derived from the LLM brief.
    assert result.history[0].iteration == 0
    assert result.history[0].result.ok
    # The write prompt (neither the brief nor the critique call) shows the
    # deterministic-solver seed lead-in.
    gen_prompt = next(
        p
        for p, s in zip(client.prompts, client.systems)
        if "You translate briefs" not in s and "senior architect" not in s
    )
    assert "deterministic layout solver produced this" in gen_prompt
    # The LLM-brief call happened (its distinctive system marker was used).
    assert any("You translate briefs" in s for s in client.systems)


def test_invalid_llm_reply_falls_back_to_unseeded_without_raising():
    # The LLM returns junk that parse_brief2 rejects → no seed, no exception; the
    # loop runs unseeded (no iteration 0).
    client = _PromptRecordingClient(
        gen_replies=[CLEAN], brief_replies=["this is not a brief at all"]
    )
    result = BarndoAgent(client=client).design(
        BEDLESS_PROSE, max_iterations=1, target_score=None,
        seed_with_solver=BEDLESS_PROSE,
    )
    assert all(s.iteration >= 1 for s in result.history)  # no iteration 0
    assert result.history[0].iteration == 1


def test_regex_wins_when_it_can_parse_no_llm_brief_call():
    # A brief WITH a bedroom count is handled by the regex; the LLM-brief path is
    # never reached, so no "You translate briefs" call is made.
    client = _PromptRecordingClient(gen_replies=[CLEAN])
    BarndoAgent(client=client).design(
        "2 bed 1 bath cabin", max_iterations=1, target_score=None,
        seed_with_solver="2 bed 1 bath cabin",
    )
    assert not any("You translate briefs" in s for s in client.systems)


def test_llm_brief_skipped_when_no_client():
    # _solver_candidate_sources with client=None must not attempt the LLM path.
    assert _solver_candidate_sources(BEDLESS_PROSE) == []
    assert _solver_candidate_sources(BEDLESS_PROSE, client=None) == []


def test_brief_from_llm_returns_none_on_missing_client_or_empty_text():
    assert _brief_from_llm(BEDLESS_PROSE, client=None) is None
    assert _brief_from_llm("", client=object()) is None


# --- deliverable 3: the prose->brief template fix (no GARAGE_PASSTHROUGH) -----


def test_shop_brief_seed_has_no_garage_passthrough_warning():
    # The synthesized brief buffers the shop off a mudroom, off the public core —
    # never off the sleeping hall — so the deterministic seed compiles with NO
    # GARAGE_PASSTHROUGH warning.
    seed = _solver_seed(
        "3 bed 2 bath barndominium ~2000 sqft with open core and shop bay"
    )
    assert seed is not None and seed.step.result.ok
    codes = {d.code for d in seed.step.result.warnings}
    assert "GARAGE_PASSTHROUGH" not in codes


def test_shop_brief_adds_a_buffering_mudroom_off_the_public_core():
    from barndsl.agent import _brief2_from_prose

    brief = _brief2_from_prose(
        "3 bed 2 bath barndominium ~2000 sqft with open core and shop bay"
    )
    assert brief is not None
    ids = {r.id for r in brief.rooms}
    assert "mudroom" in ids and "shop" in ids
    adj = {frozenset(pair) for pair in brief.adjacencies}
    # shop doors into the mudroom; the mudroom into the kitchen (the public core).
    assert frozenset({"shop", "mudroom"}) in adj
    assert frozenset({"mudroom", "kitchen"}) in adj
    # The shop is NEVER wired off the sleeping hall.
    assert frozenset({"shop", "hall"}) not in adj
