"""The agent loop's mechanics — no API key, no network, a scripted fake client.

What the loop must guarantee (see `barndsl/agent.py`):

- **best iteration wins**: every step is scored with `design_score`, and
  `design()` returns the highest-scoring step — a regression on the final
  round is never returned;
- **structured feedback**: the revision prompt carries `render_feedback`'s
  compact score-headed diagnostic lines, not the caret-art `report()`;
- **NO_PROGRAM nudge**: a source without a `program` statement earns a
  deterministic INFO that rides the feedback into the next round;
- **target-score gate**: the critic saying "satisfied" cannot end the loop
  while the score is below `target_score`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from barndsl import compile_source
from barndsl.agent import (
    BarndoAgent,
    CritiqueSpec,
    DesignResult,
    DesignStep,
    render_feedback,
)
from barndsl.score import design_score

GALLERY = Path(__file__).resolve().parent.parent / "examples" / "gallery"

#: A curated 0-error/0-warning/0-info plan (with a `program` line) — the
#: "clean, high-score" iteration in the scripted scenarios.
CLEAN = (GALLERY / "cottage.barn").read_text()

# Compiles OK but with warnings/infos — a mediocre middle rung on the score
# ladder (no `program` line, so it also earns the NO_PROGRAM nudge).
MEDIOCRE = """\
plan "Mediocre"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,0 size 12 x 24
door living - bed width 2.67
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""

# Doesn't compile to a plan at all — a hard regression (score 0).
BROKEN = 'plan "Broken"\nenvelope banana\n'


# -- the fake Anthropic client ------------------------------------------------


class _FakeStream:
    """Stands in for `client.messages.stream(...)` — a context manager whose
    `get_final_message()` yields one text block, like the SDK's."""

    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


class FakeClient:
    """Scripted `.messages.stream(...)` / `.messages.parse(...)`.

    `sources` are returned (fenced, like a real reply) one per generation call;
    `critiques` one per critique call. Prompts are recorded so tests can assert
    what the model was actually shown.
    """

    def __init__(self, sources: list[str], critiques: list[CritiqueSpec] | None = None):
        self._sources = list(sources)
        self._critiques = list(critiques or [])
        self.stream_prompts: list[str] = []
        self.parse_prompts: list[str] = []
        self.messages = self  # so client.messages.stream / .parse resolve here

    def stream(self, **kwargs):
        self.stream_prompts.append(kwargs["messages"][0]["content"])
        return _FakeStream(f"```barn\n{self._sources.pop(0)}```")

    def parse(self, **kwargs):
        self.parse_prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(parsed_output=self._critiques.pop(0))


def _agent(client: FakeClient) -> BarndoAgent:
    return BarndoAgent(client=client)


def _unsatisfied(*suggestions: str) -> CritiqueSpec:
    return CritiqueSpec(
        satisfied=False,
        assessment="Needs work.",
        rationale="Deductions remain on the score.",
        suggestions=list(suggestions) or ["Tighten the plan."],
    )


def _satisfied() -> CritiqueSpec:
    return CritiqueSpec(
        satisfied=True,
        assessment="Good plan.",
        rationale="Compiles 0/0/0; only continuous components deduct.",
        suggestions=[],
    )


# -- keep the best iteration, not the last ------------------------------------


def test_design_returns_the_best_scoring_iteration_not_the_last():
    """broken → clean → broken again: the loop must hand back iteration 2."""
    client = FakeClient(
        sources=[BROKEN, CLEAN, BROKEN],
        critiques=[_unsatisfied("Add a porch.")],  # only iter 2 has a plan to review
    )
    result = _agent(client).design("a small cottage", max_iterations=3, target_score=None)

    assert result.iterations == 3 and len(result.history) == 3
    assert result.best_iteration == 2
    assert result.source == result.history[1].source
    assert result.result.ok
    # every step got a score, and the winner strictly beats the regression
    scores = [s.score.total for s in result.history]
    assert all(s.score is not None for s in result.history)
    assert scores[1] > scores[0] and scores[1] > scores[2]
    assert result.score.total == scores[1]


def test_best_iteration_ties_go_to_the_later_step():
    history = [
        DesignStep(1, "a\n", compile_source(MEDIOCRE), None, design_score(compile_source(MEDIOCRE))),
        DesignStep(2, "b\n", compile_source(MEDIOCRE), None, design_score(compile_source(MEDIOCRE))),
    ]
    dr = DesignResult(history[1].source, history[1].result, history)
    assert history[0].score.total == history[1].score.total  # genuinely a tie
    assert dr.best_iteration == 2


def test_design_result_score_recomputes_when_steps_are_unscored():
    result = compile_source(MEDIOCRE)
    dr = DesignResult(MEDIOCRE, result, [DesignStep(1, MEDIOCRE, result)])
    assert dr.best_iteration == 1
    assert dr.score.total == design_score(result).total


# -- structured diagnostics feedback ------------------------------------------


def test_render_feedback_is_score_headed_structured_and_deterministic():
    result = compile_source(MEDIOCRE)
    score = design_score(result)
    text = render_feedback(result)

    head, *lines = text.splitlines()
    assert head.startswith(f"Design score: {score.total:g}/100")
    assert "deductions:" in head and "warnings -" in head
    # one compact line per diagnostic: severity CODE (room) line N: msg | hint: ...
    assert len(lines) == len(result.diagnostics)
    assert any(line.startswith("warning NAT_LIGHT (living) line 4:") for line in lines)
    assert any("| hint:" in line for line in lines)
    # token-lean: no caret art, no source echo
    assert "^" not in text and "room living:" not in text
    # deterministic: same compile, same text
    assert render_feedback(compile_source(MEDIOCRE)) == text


def test_render_feedback_on_a_clean_plan_reports_no_zero_components():
    result = compile_source(CLEAN)
    text = render_feedback(result)
    assert text.startswith("Design score: ")
    assert "errors -" not in text and "warnings -" not in text


def test_revision_prompt_carries_the_structured_feedback_not_the_caret_report():
    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied(), _satisfied()])
    _agent(client).design("a starter home", max_iterations=2, target_score=None)

    revision = client.stream_prompts[1]
    assert "Design score:" in revision
    assert "warning NAT_LIGHT (living) line 4:" in revision
    assert "maximising" in revision  # the prompt says the score is the objective
    assert "^" not in revision  # no caret rendering
    # the critic's folded suggestions travel the same channel
    assert "info DESIGN" in revision


# -- the NO_PROGRAM nudge ------------------------------------------------------


def test_missing_program_statement_earns_the_nudge_and_it_reaches_the_prompt():
    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied(), _satisfied()])
    result = _agent(client).design("one bed one bath", max_iterations=2, target_score=None)

    step1 = result.history[0]
    codes = [d.code for d in step1.result.diagnostics]
    assert "NO_PROGRAM" in codes
    assert "NO_PROGRAM" in client.stream_prompts[1]
    # deterministic and scored before the (model-driven) critique is folded:
    # the step's score equals a fresh compile + nudge, and the nudge costs points
    from barndsl.agent import _fold_program_nudge

    fresh = compile_source(MEDIOCRE)
    unnudged = design_score(fresh).total
    _fold_program_nudge(fresh)
    assert step1.score.total == design_score(fresh).total < unnudged


def test_program_statement_present_means_no_nudge():
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    result = _agent(client).design("a cottage", max_iterations=3, target_score=None)

    assert result.iterations == 1  # clean + satisfied + no gate → done first round
    assert all(d.code != "NO_PROGRAM" for d in result.history[0].result.diagnostics)


def test_generate_system_prompt_requires_a_program_line():
    from barndsl.agent import _GENERATE_SYSTEM

    assert "program" in _GENERATE_SYSTEM
    assert "`program <n> bed" in _GENERATE_SYSTEM


# -- the target-score gate -----------------------------------------------------


def test_satisfied_critic_cannot_finish_below_the_target_score():
    """MEDIOCRE compiles ok and the critic approves it — but its score is well
    under the gate, so the loop must keep revising."""
    assert compile_source(MEDIOCRE).ok
    assert design_score(compile_source(MEDIOCRE)).total < 80.0

    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_satisfied(), _satisfied()])
    result = _agent(client).design("a starter home", max_iterations=3, target_score=80.0)

    assert result.iterations == 2  # gated past round 1, done once CLEAN clears 80
    assert result.best_iteration == 2
    assert result.score.total >= 80.0


def test_target_score_none_disables_the_gate():
    client = FakeClient(sources=[MEDIOCRE], critiques=[_satisfied()])
    result = _agent(client).design("a starter home", max_iterations=3, target_score=None)
    assert result.iterations == 1  # ok + satisfied is enough with the gate off


def test_gate_exhausts_iterations_when_never_cleared():
    client = FakeClient(sources=[MEDIOCRE, MEDIOCRE], critiques=[_satisfied(), _satisfied()])
    result = _agent(client).design("a starter home", max_iterations=2, target_score=99.0)
    assert result.iterations == 2
    assert result.best_iteration == 2  # equal scores: the later step wins


def test_critique_prompt_is_anchored_to_the_score_and_asks_for_rationale():
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)

    (prompt,) = client.parse_prompts
    assert "Design score:" in prompt
    assert "rationale" in prompt
    # CritiqueSpec makes the evidence a required field of the structured output
    assert "rationale" in CritiqueSpec.model_fields
    assert CritiqueSpec.model_fields["rationale"].is_required()
