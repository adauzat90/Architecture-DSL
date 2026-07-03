"""The agent loop's mechanics — no API key, no network, a scripted fake client.

What the loop must guarantee (see `barndsl/agent.py`):

- **best iteration wins**: every step is scored with `design_score`, and
  `design()` returns the highest-scoring step — a regression on the final
  round is never returned;
- **structured feedback**: the revision prompt carries `render_feedback`'s
  compact score-headed diagnostic lines (score header + cause lines, one line
  per diagnostic, then the geometry pack), not the caret-art `report()`;
- **NO_PROGRAM nudge**: a source without a `program` statement earns a
  deterministic INFO that rides the feedback into the next round;
- **target-score gate**: the critic saying "satisfied" cannot end the loop
  while the score is below `target_score`;
- **multimodal critique**: when `_plan_png` yields bytes, the critique call
  carries a base64 PNG image block (and the system prompt says so); when it
  yields None — no plan, or cairosvg missing — the call stays text-only.
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


def _prompt_text(content) -> str:
    """The text of a user turn, whether it's a bare string or content blocks."""
    if isinstance(content, str):
        return content
    return "".join(b["text"] for b in content if b.get("type") == "text")


class FakeClient:
    """Scripted `.messages.stream(...)` / `.messages.parse(...)`.

    `sources` are returned (fenced, like a real reply) one per generation call;
    `critiques` one per critique call. Prompts are recorded so tests can assert
    what the model was actually shown — `parse_prompts` holds the critique text
    (image block or not), `parse_contents`/`parse_systems` the raw content and
    system prompt for the multimodal assertions.
    """

    def __init__(self, sources: list[str], critiques: list[CritiqueSpec] | None = None):
        self._sources = list(sources)
        self._critiques = list(critiques or [])
        self.stream_prompts: list[str] = []
        self.parse_prompts: list[str] = []
        self.parse_contents: list = []
        self.parse_systems: list[str] = []
        self.messages = self  # so client.messages.stream / .parse resolve here

    def stream(self, **kwargs):
        self.stream_prompts.append(kwargs["messages"][0]["content"])
        return _FakeStream(f"```barn\n{self._sources.pop(0)}```")

    def parse(self, **kwargs):
        content = kwargs["messages"][0]["content"]
        self.parse_contents.append(content)
        self.parse_systems.append(kwargs["system"])
        self.parse_prompts.append(_prompt_text(content))
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

    head, *rest = text.splitlines()
    assert head.startswith(f"Design score: {score.total:g}/100")
    assert "deductions:" in head and "warnings -" in head
    # the header is followed by one indented cause line per score detail …
    causes = rest[: len(score.details)]
    assert causes and all(c.startswith("  ") and ": " in c for c in causes)
    # … then one compact line per diagnostic (severity CODE (room) line N: msg
    # | hint: ...) up to the geometry pack.
    body = rest[len(score.details) :]
    geo_at = body.index("Rooms (id type level x,y w x l | exterior walls):")
    lines = body[:geo_at]
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


def test_render_feedback_carries_the_score_causes():
    result = compile_source(MEDIOCRE)
    score = design_score(result)
    text = render_feedback(result, score)
    assert score.details  # MEDIOCRE deducts continuous points, so causes exist
    for name, cause in score.details.items():
        assert f"  {name} -{score.components[name]:g}: {cause}" in text


# -- the geometry pack ----------------------------------------------------------


def test_render_feedback_appends_the_geometry_pack():
    from barndsl.introspect import plan_summary, summary_text

    result = compile_source(MEDIOCRE)
    text = render_feedback(result)
    assert "  living living L0 0,0 18 x 24 | south north west" in text
    assert "Adjacency (door edges): living-bed (swing 2.67)" in text
    assert "Unplaced footprint (L0): none" in text
    # verbatim the block `barndsl inspect` prints — one source of truth
    assert text.endswith(summary_text(plan_summary(result.plan)))


def test_render_feedback_without_a_plan_has_no_geometry_pack():
    result = compile_source(BROKEN)
    assert result.plan is None
    text = render_feedback(result)
    assert "Rooms (" not in text and "Free wall spans" not in text


def test_revision_prompt_includes_the_free_wall_spans():
    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied(), _satisfied()])
    _agent(client).design("a starter home", max_iterations=2, target_score=None)

    revision = client.stream_prompts[1]
    assert "Free wall spans" in revision and "-> exterior:" in revision


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


# -- the multimodal critic -------------------------------------------------------


def test_critique_attaches_the_plan_image_when_a_png_renders(monkeypatch):
    import base64

    import barndsl.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_plan_png", lambda result: b"not-really-a-png")
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)

    (content,) = client.parse_contents
    assert isinstance(content, list)
    image, text = content
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == b"not-really-a-png"
    assert text["type"] == "text" and "Design score:" in text["text"]
    # the system prompt now claims the image and demands a visual citation
    (system,) = client.parse_systems
    assert "image of this plan is attached" in system
    assert "`rationale` must cite what you see in the drawing" in system


def test_critique_stays_text_only_when_no_png_is_available(monkeypatch):
    import barndsl.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_plan_png", lambda result: None)
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)

    (content,) = client.parse_contents
    assert isinstance(content, str) and "Design score:" in content
    # …and the system prompt must NOT claim an image that isn't there.
    (system,) = client.parse_systems
    assert "attached" not in system


def test_plan_png_is_none_without_a_plan():
    from barndsl.agent import _plan_png

    assert compile_source(BROKEN).plan is None
    assert _plan_png(compile_source(BROKEN)) is None


def test_plan_png_is_none_when_cairosvg_is_missing(monkeypatch):
    import sys

    from barndsl.agent import _plan_png

    monkeypatch.setitem(sys.modules, "cairosvg", None)  # `import cairosvg` fails
    assert _plan_png(compile_source(CLEAN)) is None  # never raises


def test_plan_png_rasterises_the_rendered_svg_when_cairosvg_is_present(monkeypatch):
    import sys
    import types

    from barndsl.agent import _plan_png

    calls: dict = {}
    fake = types.ModuleType("cairosvg")

    def svg2png(bytestring=None, **kwargs):
        calls["svg"] = bytestring
        return b"png-bytes"

    fake.svg2png = svg2png
    monkeypatch.setitem(sys.modules, "cairosvg", fake)
    assert _plan_png(compile_source(CLEAN)) == b"png-bytes"
    assert b"<svg" in calls["svg"]  # it rasterised the real render


def test_plan_png_swallows_raster_failures(monkeypatch):
    import sys
    import types

    from barndsl.agent import _plan_png

    fake = types.ModuleType("cairosvg")

    def svg2png(**kwargs):
        raise RuntimeError("no cairo library")

    fake.svg2png = svg2png
    monkeypatch.setitem(sys.modules, "cairosvg", fake)
    assert _plan_png(compile_source(CLEAN)) is None


# -- solver seeding (candidate 0) ---------------------------------------------


def _solver_brief():
    """A fresh v2 solver brief whose best topology compiles clean (~84)."""
    from barndsl.layout2 import LayoutBrief2, RoomSpec2

    return LayoutBrief2(
        name="Solver Seed",
        rooms=[
            RoomSpec2("living", "living", area=360),
            RoomSpec2("kitchen", "kitchen", area=200),
            RoomSpec2("bed1", "bedroom", area=170),
            RoomSpec2("bath", "bathroom", area=70),
        ],
        adjacencies=[("living", "kitchen"), ("living", "bed1"), ("bed1", "bath")],
    )


def test_solver_seed_is_iteration_0_and_wins_when_the_llm_never_beats_it():
    """The best solver candidate is recorded as iteration 0; when every LLM round
    is worse (here: broken), best-iteration-wins hands the solver's plan back."""
    from barndsl.agent import _solver_seed_step

    seed = _solver_seed_step(_solver_brief())
    assert seed is not None and seed.iteration == 0 and seed.result.ok
    # The seed declares the program its plan delivers, so the floor is not
    # depressed by a self-inflicted missing-program deduction.
    assert any(l.strip().startswith("program ") for l in seed.source.splitlines())
    assert not any(d.code == "NO_PROGRAM" for d in seed.result.infos)
    assert not any(
        d.code == "PROGRAM_MISMATCH" for d in seed.result.warnings
    )

    client = FakeClient(sources=[BROKEN, BROKEN])  # LLM never produces a plan
    result = _agent(client).design(
        "a small barndo", max_iterations=2, target_score=None,
        seed_with_solver=_solver_brief(),
    )

    assert len(result.history) == 3  # seed + 2 LLM rounds
    assert result.history[0].iteration == 0
    assert result.best_iteration == 0
    assert result.result.ok and result.source == result.history[0].source
    assert result.score.total == seed.score.total


def test_generation_prompt_includes_the_solver_seed_source():
    from barndsl.agent import _solver_seed_step

    seed = _solver_seed_step(_solver_brief())
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=_solver_brief(),
    )

    first = client.stream_prompts[0]
    assert "deterministic layout solver produced this" in first
    assert "room living: living at" in first  # the seed's DSL rides along
    assert seed.source.strip() in first


def test_seed_accepts_a_textual_brief():
    """A textual v2 brief is parsed and solved just like a LayoutBrief2."""
    brief_text = (
        'plan "Text Seed"\n'
        "room living: living area 360\n"
        "room kitchen: kitchen area 200\n"
        "room bed1: bedroom area 170\n"
        "room bath: bathroom area 70\n"
        "adjacent living kitchen bed1\n"
        "adjacent bed1 bath\n"
    )
    client = FakeClient(sources=[BROKEN])
    result = _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=brief_text,
    )
    assert result.history[0].iteration == 0 and result.history[0].result.ok
    assert result.best_iteration == 0  # the seed beats the broken LLM round


def test_seed_degrades_gracefully_when_the_solver_yields_nothing():
    """An unusable solver spec leaves the loop exactly as it is without seeding."""
    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied(), _satisfied()])
    result = _agent(client).design(
        "a starter home", max_iterations=2, target_score=None,
        seed_with_solver="this is not a valid brief statement",
    )
    # No iteration-0 step: the first recorded step is the first LLM round.
    assert all(s.iteration >= 1 for s in result.history)
    assert result.history[0].iteration == 1
    assert result.best_iteration == 2  # CLEAN, exactly as the unseeded loop


def test_seed_falsy_is_todays_behaviour():
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    result = _agent(client).design("a cottage", max_iterations=1, target_score=None)
    assert [s.iteration for s in result.history] == [1]


# -- regression legibility in the feedback header -----------------------------


def test_feedback_stays_coherent_after_a_failed_iteration():
    """CLEAN → BROKEN → CLEAN: after the broken round the prompt shows the best
    valid source paired with ITS OWN feedback, names the discarded attempt, and
    quotes the fatal diagnostics only under an attribution that says they belong
    to the discarded source — never captioned as feedback on the shown DSL."""
    client = FakeClient(
        sources=[CLEAN, BROKEN, CLEAN], critiques=[_unsatisfied(), _satisfied()]
    )
    _agent(client).design("a cottage", max_iterations=3, target_score=None)

    revision = client.stream_prompts[2]  # the prompt after the broken round 2
    assert "did not compile and was DISCARDED" in revision
    assert "best valid iteration (1" in revision
    # The fatal diagnostics appear, but only AFTER the attribution line.
    attribution = "Fatal diagnostics from the discarded attempt"
    assert attribution in revision
    assert revision.index("BAD_NUMBER") > revision.index(attribution)
    # The feedback block above the attribution describes the CLEAN source: no
    # NO_PROGRAM info (CLEAN declares a program) and no fatal-error score.
    described = revision[: revision.index(attribution)]
    assert "BAD_NUMBER" not in described
    assert "NO_PROGRAM" not in described


def test_regression_line_absent_when_render_feedback_has_no_best_prior():
    """Default call is unchanged — no best_prior, no REGRESSION line."""
    text = render_feedback(compile_source(BROKEN))
    assert "REGRESSION" not in text


def test_render_feedback_regression_line_for_a_lower_score():
    best = DesignStep(2, CLEAN, compile_source(CLEAN), None, design_score(compile_source(CLEAN)))
    worse = compile_source(MEDIOCRE)
    text = render_feedback(worse, best_prior=best)
    assert "REGRESSION" in text
    assert f"scored {best.score.total:g}" in text
    assert "iteration 2" in text


def test_revision_reverts_to_the_best_valid_source_after_a_broken_round():
    """After a broken round the loop revises from the best VALID source, not the
    broken one it just produced (a behaviour change from always-latest)."""
    client = FakeClient(
        sources=[CLEAN, BROKEN, CLEAN], critiques=[_unsatisfied(), _satisfied()]
    )
    _agent(client).design("a cottage", max_iterations=3, target_score=None)

    revision = client.stream_prompts[2]
    assert "banana" not in revision.split("Fatal diagnostics")[0]
    assert 'plan "Stillwater Cottage"' in revision  # CLEAN is carried forward
    # Nothing above the attribution claims the shown (valid) source failed.
    assert "does not compile" not in revision.split("Fatal diagnostics")[0]
