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
import threading
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
    """Scripted `.messages.stream(...)` — both generation and critique stream now.

    `sources` are returned (fenced, like a real reply) one per generation call;
    `critiques` one per critique call, serialised to fenced JSON exactly as a real
    model emits under the `_CRITIQUE_JSON` instruction. Critique calls are told
    apart from generation by their system prompt (the critic is a "senior
    architect"). Prompts are recorded so tests can assert what the model was shown
    — `parse_prompts` holds the critique text (image block or not),
    `parse_contents`/`parse_systems` the raw content and system prompt.
    """

    def __init__(self, sources: list[str], critiques: list[CritiqueSpec] | None = None):
        self._sources = list(sources)
        self._critiques = list(critiques or [])
        self.stream_prompts: list[str] = []
        self.parse_prompts: list[str] = []
        self.parse_contents: list = []
        self.parse_systems: list[str] = []
        self.messages = self  # so client.messages.stream resolves here

    def stream(self, **kwargs):
        content = kwargs["messages"][0]["content"]
        system = kwargs.get("system", "")
        if "senior architect" in system:  # a critique call (see _CRITIQUE_SYSTEM)
            self.parse_contents.append(content)
            self.parse_systems.append(system)
            self.parse_prompts.append(_prompt_text(content))
            crit = self._critiques.pop(0)
            return _FakeStream("```json\n" + crit.model_dump_json() + "\n```")
        self.stream_prompts.append(content)
        return _FakeStream(f"```barn\n{self._sources.pop(0)}```")


class _FakeDeltaStream(_FakeStream):
    """An *iterable* stream — like the SDK's `MessageStream` — that yields a
    thinking delta then a text delta, so `write_source`/`critique` can pump a
    live activity feed. `get_final_message()` still returns the accumulated text,
    exactly as when the stream is consumed without iterating."""

    def __init__(self, text: str, thinking: str = ""):
        super().__init__(text)
        self._thinking = thinking

    def __iter__(self):
        if self._thinking:
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking=self._thinking),
            )
        yield SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=self._text),
        )


class _DeltaClient:
    """A `.messages.stream(...)` that streams thinking + text deltas for both the
    generation and the critique call, so `on_activity` sees a live feed on each."""

    def __init__(self, source: str, thinking: str = "", critique: CritiqueSpec | None = None):
        self._source = source
        self._thinking = thinking
        self._critique = critique
        self.messages = self

    def stream(self, **kwargs):
        if "senior architect" in kwargs.get("system", ""):  # the critique call
            crit = self._critique or _satisfied()
            reply = "```json\n" + crit.model_dump_json() + "\n```"
            return _FakeDeltaStream(reply, thinking="weighing the plan against the score")
        return _FakeDeltaStream(f"```barn\n{self._source}```", thinking=self._thinking)


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
    """broken → clean → broken again: the loop must hand back iteration 2. (Each
    intended-failed round scripts TWO broken sources — the write plus its
    in-round repair retry, which must also fail for the round to stay failed.)"""
    client = FakeClient(
        sources=[BROKEN, BROKEN, CLEAN, BROKEN, BROKEN],
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
    from barndsl.introspect import plan_summary, render_ascii_plan, summary_text

    result = compile_source(MEDIOCRE)
    text = render_feedback(result)
    assert "  living living L0 0,0 18 x 24 | south north west" in text
    assert "Adjacency (door edges): living-bed (swing 2.67)" in text
    assert "Unplaced footprint (L0): none" in text
    # the geometry pack rides verbatim — one source of truth with `barndsl inspect`
    assert summary_text(plan_summary(result.plan)) in text
    # the ASCII plan view is appended after it (the drawing the table hides)
    assert text.endswith(render_ascii_plan(result.plan))
    assert "PLAN VIEW (north up," in text


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

    # LLM never produces a plan (each round: the write + its failed in-round retry)
    client = FakeClient(sources=[BROKEN] * 4)
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
    client = FakeClient(sources=[BROKEN, BROKEN])  # the write + its failed retry
    result = _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=brief_text,
    )
    assert result.history[0].iteration == 0 and result.history[0].result.ok
    assert result.best_iteration == 0  # the seed beats the broken LLM round


def test_seed_degrades_gracefully_when_the_solver_yields_nothing():
    """An unusable solver spec leaves the loop exactly as it is without seeding.

    The seed brief has no bedroom-count signal, so it now reaches the LLM-brief
    fallback; the first scripted reply is not a parseable brief2, so that path
    returns None and the loop runs unseeded on the remaining rounds. The first
    scripted source is consumed by the (failed) brief-translation call, so the two
    LLM rounds draw the two that follow.
    """
    client = FakeClient(
        sources=["not a brief either", MEDIOCRE, CLEAN],
        critiques=[_unsatisfied(), _satisfied()],
    )
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


# -- prose-brief seeding (natural language, not the structured grammar) --------


def _room_type_counts(step):
    """{room-type value: count} for a scored solver-seed step's plan."""
    counts: dict[str, int] = {}
    for r in step.result.plan.rooms:
        counts[r.type.value] = counts.get(r.type.value, 0) + 1
    return counts


def test_prose_brief_seeds_a_compiling_scored_plan():
    """A natural-language brief the structured parser can't read now seeds the
    loop: the derived program compiles clean and carries the expected rooms."""
    from barndsl.agent import _solver_seed_step

    step = _solver_seed_step(
        "3 bed 2 bath barndominium around 2000 sqft with an open "
        "living-kitchen-dining core and a shop bay"
    )
    assert step is not None and step.iteration == 0 and step.result.ok
    assert step.score is not None and step.score.total > 0
    counts = _room_type_counts(step)
    # 3 bedrooms, 2 baths (ensuite + shared), the open core, and the shop bay the
    # brief explicitly asked for (kept because it is score-neutral, verified).
    assert counts.get("bedroom") == 3
    assert counts.get("bathroom") == 2
    assert counts.get("living") == 1 and counts.get("kitchen") == 1
    assert counts.get("dining") == 1  # mentioned + beds >= 3
    assert counts.get("shop") == 1


def test_prose_brief_area_scales_the_target_total():
    """The stated floor area scales every room, so a smaller sqft brief yields a
    smaller total program than an otherwise-identical larger one."""
    from barndsl.agent import _brief2_from_prose

    small = _brief2_from_prose("3 bed 2 bath 1400 sqft")
    big = _brief2_from_prose("3 bed 2 bath 2600 sqft")
    assert small is not None and big is not None
    small_total = sum(r.area for r in small.rooms)
    big_total = sum(r.area for r in big.rooms)
    assert big_total > small_total
    # The scale tracks the stated area (clamped to [0.7, 1.6]); 1400 lands near it.
    assert 1200 < small_total < 1700


def test_prose_one_bath_and_no_dining_for_a_small_brief():
    """A 2-bed 1-bath brief gets one shared bath (no ensuite) and no dining room
    (not mentioned, and under the 3-bed dining threshold)."""
    from barndsl.agent import _solver_seed_step

    step = _solver_seed_step("2 bed 1 bath cabin")
    assert step is not None and step.result.ok
    counts = _room_type_counts(step)
    assert counts.get("bedroom") == 2
    assert counts.get("bathroom") == 1
    assert "dining" not in counts


def test_prose_brief_without_a_bed_count_yields_no_candidates():
    """Nonsense (no bedroom count) is not a program: no prose brief, no seed."""
    from barndsl.agent import (
        _brief2_from_prose,
        _solver_candidate_sources,
        _solver_seed_step,
    )

    assert _brief2_from_prose("hello world") is None
    assert _solver_candidate_sources("hello world") == []
    assert _solver_seed_step("hello world") is None


def test_structured_brief_keeps_parse_brief2_precedence(monkeypatch):
    """A valid structured brief is parsed by parse_brief2 directly; the prose
    fallback is never consulted, so structured input keeps precedence."""
    import barndsl.agent as agent_mod

    structured = (
        'plan "Struct"\n'
        "room living: living area 360\n"
        "room kitchen: kitchen area 200\n"
        "room bed1: bedroom area 170\n"
        "room bath: bathroom area 70\n"
        "adjacent living kitchen bed1\n"
        "adjacent bed1 bath\n"
    )

    def _boom(_text):  # the prose fallback must not run for a structured brief
        raise AssertionError("_brief2_from_prose called for a structured brief")

    monkeypatch.setattr(agent_mod, "_brief2_from_prose", _boom)
    srcs = agent_mod._solver_candidate_sources(structured)
    assert srcs  # parse_brief2 handled it; the three v2 topologies produced DSL


# -- regression legibility in the feedback header -----------------------------


def test_failed_round_carries_its_own_source_and_diagnostics_for_repair():
    """CLEAN, then a broken round-2 write: the NEXT prompt — round 2's in-round
    repair retry — carries the FAILED source verbatim (so "reproduce it, fix
    line N" is coherent) paired with that source's OWN diagnostics — it does NOT
    revert to the best-valid source, which would invite a redesign."""
    client = FakeClient(
        sources=[CLEAN, BROKEN, CLEAN], critiques=[_unsatisfied(), _satisfied()]
    )
    _agent(client).design("a cottage", max_iterations=3, target_score=None)

    revision = client.stream_prompts[2]  # the prompt after round 2's broken write
    # Repair wording, not the old discard/best-valid revert.
    assert "Do NOT redesign" in revision
    assert "did not compile and was DISCARDED" not in revision
    # The prior shown is the broken source, and its error rides the feedback.
    assert 'plan "Broken"' in revision
    assert "BAD_NUMBER" in revision


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


def test_repair_round_revises_the_failed_source_not_the_best_valid():
    """After a broken write the loop repairs the broken source in place (here in
    the in-round retry); it does NOT swap in the best VALID source (that would
    let the model redesign, the convergence regression repair mode fixes)."""
    client = FakeClient(
        sources=[CLEAN, BROKEN, CLEAN], critiques=[_unsatisfied(), _satisfied()]
    )
    _agent(client).design("a cottage", max_iterations=3, target_score=None)

    revision = client.stream_prompts[2]
    # The failed source is the prior, NOT the earlier clean plan.
    assert "banana" in revision  # the broken source is carried forward
    assert 'plan "Stillwater Cottage"' not in revision
    assert "Do NOT redesign" in revision


# -- refinement seed (seed_source) --------------------------------------------


def test_seed_source_refines_the_current_plan_in_the_first_prompt():
    """`seed_source` primes round 1 as a revision of the caller's plan: it rides
    the first prompt as the "previous" DSL plus its own diagnostics."""
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    result = _agent(client).design(
        "make the kitchen bigger", max_iterations=1, target_score=None, seed_source=MEDIOCRE
    )

    first = client.stream_prompts[0]
    assert "Your previous DSL:" in first
    assert 'plan "Mediocre"' in first  # the seed source is shown as the prior
    assert "Design score:" in first  # …with its diagnostics as feedback
    # It is NOT recorded as a competing iteration 0 — refinement may reshape the
    # design, and must not be vetoed by best-iteration-wins.
    assert [s.iteration for s in result.history] == [1]


def test_seed_source_default_is_todays_fresh_generation():
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)
    assert "Your previous DSL:" not in client.stream_prompts[0]


# -- cancellation + phase hook ------------------------------------------------


def test_cancel_stops_the_loop_between_rounds():
    """Once cancellation is set after a step, the next round never starts."""
    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied(), _satisfied()])
    stopped = threading.Event()

    result = _agent(client).design(
        "a home",
        max_iterations=3,
        target_score=None,
        cancel=stopped.is_set,
        on_step=lambda _step: stopped.set(),
    )
    assert [s.iteration for s in result.history] == [1]  # round 2 never ran
    assert result.best_iteration == 1
    assert result.termination_reason == "cancelled"


def test_cancel_before_the_first_round_returns_an_empty_result():
    client = FakeClient(sources=[CLEAN])
    result = _agent(client).design(
        "x", max_iterations=2, target_score=None, cancel=lambda: True
    )
    assert result.history == [] and result.iterations == 0
    assert result.best_iteration == 0  # nothing recorded, but no exception
    assert result.termination_reason == "cancelled"


def test_on_phase_narrates_writing_compiling_and_critiquing():
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    phases: list[tuple[str, int]] = []
    _agent(client).design(
        "a cottage", max_iterations=1, target_score=None,
        on_phase=lambda phase, rnd: phases.append((phase, rnd)),
    )
    assert ("writing", 1) in phases
    assert ("compiling", 1) in phases
    assert ("critiquing", 1) in phases  # CLEAN builds a plan, so the critic runs


# -- the live activity feed (on_activity) -------------------------------------
# The writing and critiquing LLM calls stream; `_pump_activity` forwards each
# text/thinking delta to `on_activity` *before* `get_final_message()` (which
# still returns the full reply), so a UI can watch the agent think and write.


def test_write_source_streams_thinking_and_text_to_on_activity():
    client = _DeltaClient(CLEAN, thinking="the living wing should face south")
    seen: list[tuple[str, str]] = []
    src = BarndoAgent(client=client).write_source(
        "a cottage", on_activity=lambda channel, delta: seen.append((channel, delta))
    )
    channels = {c for c, _ in seen}
    assert channels == {"thinking", "text"}
    # both channels arrived intact…
    assert "".join(d for c, d in seen if c == "thinking") == "the living wing should face south"
    assert "plan" in "".join(d for c, d in seen if c == "text")
    # …and the accumulated reply is still parsed into the final source
    assert 'plan "' in src and compile_source(src).ok


def test_write_source_without_on_activity_does_not_iterate_the_stream():
    """Back-compat: the stream is only iterated when a caller asks for activity,
    so today's callers (CLI, the non-iterable FakeClient) are untouched. `_FakeStream`
    has no `__iter__`, so an unconditional iteration would raise here."""
    src = _agent(FakeClient(sources=[CLEAN])).write_source("a cottage")
    assert 'plan "' in src


def test_design_forwards_phase_round_channel_delta_to_on_activity():
    client = _DeltaClient(CLEAN, thinking="north-south axis", critique=_satisfied())
    events: list[tuple[str, int, str]] = []
    BarndoAgent(client=client).design(
        "a cottage", max_iterations=1, target_score=None,
        on_activity=lambda phase, rnd, channel, delta: events.append((phase, rnd, channel)),
    )
    seen = set(events)
    assert ("writing", 1, "thinking") in seen
    assert ("writing", 1, "text") in seen
    # the critic streams its reasoning too, tagged to the critiquing phase
    assert ("critiquing", 1, "thinking") in seen


def test_pump_activity_swallows_a_broken_stream():
    """A stream that dies mid-iteration must never abort the design — the feed is
    best-effort; `get_final_message()` remains the source of the result."""
    from barndsl.agent import _pump_activity

    class _Boom:
        def __iter__(self):
            raise RuntimeError("stream died")

    calls: list = []
    _pump_activity(_Boom(), lambda channel, delta: calls.append((channel, delta)))
    assert calls == []  # no crash, nothing forwarded


def test_cancel_closes_an_active_generation_stream():
    class _ClosableDeltaStream(_FakeDeltaStream):
        def __init__(self, text: str):
            super().__init__(text)
            self.closed = False

        def close(self):
            self.closed = True

    class _Client:
        def __init__(self):
            self.messages = self
            self.stream_obj = _ClosableDeltaStream(f"```barn\n{CLEAN}```")

        def stream(self, **_kwargs):
            return self.stream_obj

    stopped = threading.Event()
    client = _Client()
    result = BarndoAgent(client=client).design(
        "a cottage",
        max_iterations=1,
        target_score=None,
        cancel=stopped.is_set,
        on_activity=lambda *_args: stopped.set(),
    )

    assert client.stream_obj.closed is True
    assert result.termination_reason == "cancelled"
    assert result.history == []


def test_cancel_after_compile_does_not_start_the_critic():
    stopped = threading.Event()
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])

    def on_phase(phase: str, _round: int) -> None:
        if phase == "compiling":
            stopped.set()

    result = _agent(client).design(
        "a cottage",
        max_iterations=1,
        target_score=None,
        cancel=stopped.is_set,
        on_phase=on_phase,
    )

    assert result.termination_reason == "cancelled"
    assert client.parse_prompts == []


# -- availability probe -------------------------------------------------------


def test_agent_availability_true_when_anthropic_and_key_present(monkeypatch):
    from barndsl.agent import agent_availability

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
    available, reason = agent_availability()
    assert available is True and reason is None


# -- streamed, fence-tolerant critique ----------------------------------------
# The critique is a streamed reply (a non-streaming call is refused by the SDK at
# high max_tokens on a slow reasoning model). The model is told to emit a JSON
# object, which we parse fence-tolerantly — so it works the same on native
# Anthropic and on compat gateways (DeepSeek) that fence their JSON. A reply with
# no parseable JSON degrades to an unsatisfied verdict rather than crashing.


class _FakeStreamClient:
    """A `.messages.stream(...)` that always returns ``reply`` as the message text,
    standing in for a gateway that streams a (possibly fenced) critique."""

    def __init__(self, reply: str):
        self._reply = reply
        self.messages = self

    def stream(self, **kwargs):
        return _FakeStream(self._reply)


FENCED_CRITIQUE = (
    '```json\n{"satisfied": false, "assessment": "Tight but workable.", '
    '"rationale": "WINDOW_EGRESS on bed2.", "suggestions": ["Widen the bed2 window"]}\n```'
)


def test_json_object_from_text_strips_a_fence_and_ignores_prose():
    from barndsl.agent import _json_object_from_text

    assert _json_object_from_text(FENCED_CRITIQUE).startswith('{"satisfied"')
    # embedded in prose, no fence
    wrapped = 'Here is my review:\n{"satisfied": true, "suggestions": []}\nThanks!'
    assert _json_object_from_text(wrapped) == '{"satisfied": true, "suggestions": []}'
    # pure prose has no object
    assert _json_object_from_text("Looking at this plan, it feels like a home.") is None


def test_critique_from_text_parses_fenced_json_and_rejects_prose():
    from barndsl.agent import _critique_from_text

    crit = _critique_from_text(FENCED_CRITIQUE)
    assert crit is not None and crit.satisfied is False
    assert crit.suggestions == ["Widen the bed2 window"]
    assert _critique_from_text("no json here at all") is None


def test_critique_parses_fenced_json_from_a_stream():
    """The DeepSeek path: the streamed reply fences its JSON; the critic reads it."""
    agent = BarndoAgent(client=_FakeStreamClient(FENCED_CRITIQUE))
    crit = agent.critique(compile_source(MEDIOCRE))
    assert crit.satisfied is False
    assert crit.suggestions == ["Widen the bed2 window"]
    assert "skipped" not in crit.assessment  # a real critique, not the degraded fallback


def test_critique_fails_closed_when_there_is_no_json():
    """Pure prose degrades without crashing, but never counts as approval."""
    result = compile_source(MEDIOCRE)
    agent = BarndoAgent(client=_FakeStreamClient("Looking at this plan, it reads as a home."))
    crit = agent.critique(result)
    assert crit.satisfied is False
    assert "skipped" in crit.assessment


def test_critique_prompt_instructs_json_output():
    """The critique prompt must ask for JSON so non-Anthropic gateways emit it."""
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)
    assert client.parse_prompts, "the critic should have been called"
    assert '"satisfied"' in client.parse_prompts[0]
    assert "JSON object" in client.parse_prompts[0]


def test_agent_availability_reports_a_missing_key(monkeypatch):
    from barndsl.agent import agent_availability

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    available, reason = agent_availability()
    assert available is False
    assert reason is not None and "ANTHROPIC_API_KEY" in reason


def test_agent_availability_reports_a_missing_dependency(monkeypatch):
    import sys

    from barndsl.agent import agent_availability

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")  # key present, so the extra is the gap
    monkeypatch.setitem(sys.modules, "anthropic", None)  # `import anthropic` fails
    available, reason = agent_availability()
    assert available is False
    assert reason is not None and "barndsl[agent]" in reason


# -- environment-backed configuration knobs -----------------------------------
# model / max_tokens / target_score / max_iterations each have a built-in default
# overridable by a BARNDSL_* env var; an explicit argument always wins.


def test_resolvers_use_defaults_then_env_then_explicit(monkeypatch):
    from barndsl import agent as A

    for var in (A.MODEL_ENV_VAR, A.MAX_TOKENS_ENV_VAR,
                A.TARGET_SCORE_ENV_VAR, A.MAX_ITERATIONS_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    # defaults
    assert A.resolve_model() == A.DEFAULT_MODEL
    assert A.resolve_max_tokens() == A.DEFAULT_MAX_TOKENS
    assert A.resolve_target_score() == A.DEFAULT_TARGET_SCORE
    assert A.resolve_max_iterations() == A.DEFAULT_MAX_ITERATIONS
    # env overrides
    monkeypatch.setenv(A.MODEL_ENV_VAR, "deepseek-v4-pro")
    monkeypatch.setenv(A.MAX_TOKENS_ENV_VAR, "48000")
    monkeypatch.setenv(A.TARGET_SCORE_ENV_VAR, "75")
    monkeypatch.setenv(A.MAX_ITERATIONS_ENV_VAR, "6")
    assert A.resolve_model() == "deepseek-v4-pro"
    assert A.resolve_max_tokens() == 48000
    assert A.resolve_target_score() == 75.0
    assert A.resolve_max_iterations() == 6
    # explicit argument beats the env
    assert A.resolve_model("claude-opus-4-8") == "claude-opus-4-8"
    assert A.resolve_max_tokens(9000) == 9000


def test_resolvers_fall_back_on_malformed_env(monkeypatch):
    from barndsl import agent as A

    monkeypatch.setenv(A.MAX_TOKENS_ENV_VAR, "lots")
    monkeypatch.setenv(A.TARGET_SCORE_ENV_VAR, "high")
    monkeypatch.setenv(A.MAX_ITERATIONS_ENV_VAR, "0")  # non-positive → default
    assert A.resolve_max_tokens() == A.DEFAULT_MAX_TOKENS
    assert A.resolve_target_score() == A.DEFAULT_TARGET_SCORE
    assert A.resolve_max_iterations() == A.DEFAULT_MAX_ITERATIONS


def test_agent_reads_model_and_max_tokens_from_env(monkeypatch):
    from barndsl import agent as A

    monkeypatch.setenv(A.MODEL_ENV_VAR, "deepseek-v4-pro")
    monkeypatch.setenv(A.MAX_TOKENS_ENV_VAR, "40000")
    ag = BarndoAgent(client=FakeClient(sources=[CLEAN]))
    assert ag.model == "deepseek-v4-pro"
    assert ag.max_tokens == 40000


def test_design_default_iterations_come_from_env(monkeypatch):
    """With no max_iterations arg, the loop runs $BARNDSL_MAX_ITERATIONS rounds."""
    from barndsl import agent as A

    monkeypatch.setenv(A.MAX_ITERATIONS_ENV_VAR, "2")
    monkeypatch.setenv(A.TARGET_SCORE_ENV_VAR, "100")  # never satisfied → runs the full cap
    client = FakeClient(sources=[MEDIOCRE, MEDIOCRE])
    # no max_iterations / target_score passed → both resolve from the env
    result = _agent(client).design("a starter home", critique=False)
    assert result.iterations == 2


def test_design_explicit_none_target_score_still_disables_the_gate(monkeypatch):
    """`target_score=None` must stay 'no gate' even with $BARNDSL_TARGET_SCORE set."""
    from barndsl import agent as A

    monkeypatch.setenv(A.TARGET_SCORE_ENV_VAR, "100")  # would force more rounds if it applied
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    result = _agent(client).design("a cottage", max_iterations=3, target_score=None)
    # CLEAN compiles and the critic is satisfied; with the gate disabled the loop
    # finishes on round 1 instead of grinding to the env's target of 100.
    assert result.iterations == 1


# -- prompting for compat-gateway models (the DeepSeek pass) -------------------


def test_the_embedded_example_plan_compiles_perfect():
    """The few-shot in the system prompt must never rot against the grammar."""
    from barndsl.agent import _EXAMPLE_PLAN
    from barndsl.score import design_score

    result = compile_source(_EXAMPLE_PLAN)
    assert result.plan is not None
    assert not result.diagnostics, [d.code for d in result.diagnostics]
    assert design_score(result).total == 100.0


def test_the_lead_hall_spine_example_compiles_with_no_errors_or_warnings():
    """The lead worked example (_EXAMPLE_HALL_SPINE, "Birch Hollow") carries the
    added `zone`/`suite`/`require` declarations, so it must still compile with zero
    errors AND zero warnings (infos tolerated). No exact score is asserted here —
    the score is being recalibrated concurrently — only the diagnostic counts."""
    from barndsl.agent import _EXAMPLE_HALL_SPINE
    from barndsl.issues import Severity

    result = compile_source(_EXAMPLE_HALL_SPINE)
    assert result.plan is not None
    bad = [
        d
        for d in result.diagnostics
        if d.severity in (Severity.ERROR, Severity.WARNING)
        and not getattr(d, "accepted", False)
    ]
    assert not bad, [(d.severity.name, d.code) for d in bad]


def test_embedded_lshape_example_pins_the_gallery_file_and_compiles_clean():
    """The `wing` few-shot is a byte-for-byte copy of examples/gallery/lshape.barn
    (a test-pinned clean plan) — the pin makes the copy unable to drift — and it
    compiles with zero diagnostics. Read the gallery file as UTF-8 (its real
    encoding; the em-dashes in its comments are multibyte) so the comparison is
    portable off a cp1252 Windows locale, matching how Python parses the source."""
    from barndsl.agent import _EXAMPLE_LSHAPE

    assert _EXAMPLE_LSHAPE == (GALLERY / "lshape.barn").read_text(encoding="utf-8")
    result = compile_source(_EXAMPLE_LSHAPE)
    assert result.plan is not None
    assert not result.diagnostics, [d.code for d in result.diagnostics]


def test_embedded_two_story_example_pins_the_gallery_file_and_compiles_clean():
    """The `level`/`stair` few-shot is a byte-for-byte copy of
    examples/gallery/two_story.barn. It carries a documented `accept
    STAIR_HANDRAIL` pragma (a rail the DSL can't draw), so its one diagnostic is
    accepted, not active — assert no *unaccepted* diagnostic survives."""
    from barndsl.agent import _EXAMPLE_TWO_STORY

    assert _EXAMPLE_TWO_STORY == (GALLERY / "two_story.barn").read_text(encoding="utf-8")
    result = compile_source(_EXAMPLE_TWO_STORY)
    assert result.plan is not None
    active = [d for d in result.diagnostics if not getattr(d, "accepted", False)]
    assert not active, [d.code for d in active]


def test_generate_system_teaches_the_anchor_rule_and_shows_the_example():
    from barndsl.agent import (
        _EXAMPLE_HALL_SPINE,
        _EXAMPLE_LSHAPE,
        _EXAMPLE_TWO_STORY,
        _GENERATE_SYSTEM,
    )
    from barndsl.compiler import DSL_REFERENCE

    assert "ANCHOR RULE" in _GENERATE_SYSTEM
    assert "TILE, THEN CONNECT" in _GENERATE_SYSTEM
    # Furnishing: the craft block rides both prompts (the critic can ask for a
    # bed the generator knows how to place), and the lead example models it.
    from barndsl.agent import _CRITIQUE_SYSTEM

    assert "FURNISH THE KEY ROOMS" in _GENERATE_SYSTEM
    assert "FURNISH THE KEY ROOMS" in _CRITIQUE_SYSTEM
    assert "fixture bed_queen in master" in _EXAMPLE_HALL_SPINE
    # The hall-spine plan leads and the L-shaped `wing` plan follows; the
    # two-story example is deliberately NOT in the prompt (kept as a pinned
    # constant only), to make room for the design-process block.
    assert _EXAMPLE_HALL_SPINE in _GENERATE_SYSTEM
    assert _EXAMPLE_LSHAPE in _GENERATE_SYSTEM
    assert _EXAMPLE_TWO_STORY not in _GENERATE_SYSTEM
    # The strict output contract: exactly one fenced block, complete source.
    assert "exactly ONE ```barn code block" in _GENERATE_SYSTEM
    # Ordering: worked examples precede the rules -> process -> craft, the full
    # grammar comes LAST under its header, and the program mandate + output
    # contract sit at the very end (contract-last is deliberate).
    order = [
        _GENERATE_SYSTEM.index(_EXAMPLE_HALL_SPINE),
        _GENERATE_SYSTEM.index(_EXAMPLE_LSHAPE),
        _GENERATE_SYSTEM.index("HOW AN ARCHITECT THINKS"),
        _GENERATE_SYSTEM.index("DESIGN PROCESS (follow when drafting"),
        _GENERATE_SYSTEM.index("HOW TO PLACE ROOMS"),
        _GENERATE_SYSTEM.index("FURNISH THE KEY ROOMS"),
        _GENERATE_SYSTEM.index("FULL GRAMMAR REFERENCE"),
        _GENERATE_SYSTEM.index(DSL_REFERENCE),
        _GENERATE_SYSTEM.index("You MUST declare the brief"),
        _GENERATE_SYSTEM.index("OUTPUT FORMAT (strict)"),
    ]
    assert order == sorted(order), order


def test_critique_system_shares_the_vocabulary_without_the_grammar():
    """The critic judges livability off the compiled diagnostics, so it drops the
    full grammar (it doesn't need it) and instead shares the generator's rulebook
    — _DESIGN_RULES + _PLACEMENT_CRAFT — so its suggestions land in the same
    terms. 'senior architect' stays in the lead-in (the loop keys the critique
    call on it)."""
    from barndsl.agent import _CRITIQUE_SYSTEM, _DESIGN_RULES, _PLACEMENT_CRAFT
    from barndsl.compiler import DSL_REFERENCE

    assert "senior architect" in _CRITIQUE_SYSTEM
    assert DSL_REFERENCE not in _CRITIQUE_SYSTEM
    assert _DESIGN_RULES in _CRITIQUE_SYSTEM
    assert _PLACEMENT_CRAFT in _CRITIQUE_SYSTEM


def test_design_rules_list_the_quality_codes_the_score_dings():
    """The QUALITY CODES block names real diagnostic codes (paraphrased) so the
    generator can avoid them proactively — guard that every code exists in
    diagnostics.py so a renamed/removed code can't leave a phantom in the prompt."""
    from barndsl.agent import _DESIGN_RULES
    from barndsl.diagnostics import REGISTRY

    codes = (
        "WET_GROUP", "NO_CLOSET", "BED_SOUND", "HALL_DEADEND", "DOOR_CENTERED",
        "MASTER_ENSUITE", "PRIVATE_PASSTHROUGH", "GARAGE_BEDROOM",
    )
    for code in codes:
        assert code in _DESIGN_RULES, code
        assert code in REGISTRY, f"{code} not a real diagnostic code"


def test_extract_source_trims_prose_around_unfenced_dsl():
    """A gateway model that ignores the fence still yields compilable source."""
    from barndsl.agent import _extract_source

    reply = (
        "Here is the plan you asked for:\n"
        "\n"
        'plan "Test"\n'
        "envelope 30 x 20\n"
        "room living: living at 0,0 size 30 x 20\n"
        "\n"
        "This design provides an open living space with good flow.\n"
    )
    src = _extract_source(reply)
    assert src.startswith('plan "Test"')
    assert "good flow" not in src


def test_extract_source_still_prefers_the_fence_and_whole_text_fallback():
    from barndsl.agent import _extract_source

    fenced = "prose\n```barn\nplan \"A\"\nenvelope 30 x 20\n```\nmore prose"
    assert _extract_source(fenced) == 'plan "A"\nenvelope 30 x 20\n'
    # No fence, no statement keyword anywhere: the old whole-text behaviour.
    assert _extract_source("nothing at all") == "nothing at all\n"


def test_write_source_retries_once_on_an_empty_reply_then_raises():
    import pytest

    class EmptyClient:
        def __init__(self, texts):
            self._texts = list(texts)
            self.calls = 0
            self.messages = self

        def stream(self, **kwargs):
            self.calls += 1
            return _FakeStream(self._texts.pop(0))

    # Empty then a real reply: the retry rescues the round.
    client = EmptyClient(["", "```barn\nplan \"B\"\nenvelope 30 x 20\n```"])
    agent = BarndoAgent(client=client)
    src = agent.write_source("a cabin")
    assert client.calls == 2
    assert 'plan "B"' in src

    # Empty twice: fail loudly with the gateway hint, not a blank plan.
    client = EmptyClient(["", "  "])
    agent = BarndoAgent(client=client)
    with pytest.raises(RuntimeError, match="BARNDSL_MODEL"):
        agent.write_source("a cabin")
    assert client.calls == 2


def test_fold_critique_caps_suggestions_at_five():
    from barndsl.agent import _fold_critique

    result = compile_source('plan "T"\nenvelope 30 x 20\n')
    before = len(result.diagnostics)
    crit = _unsatisfied(*[f"suggestion {i}" for i in range(12)])
    _fold_critique(result, crit)
    design = [d for d in result.diagnostics if d.code == "DESIGN"]
    assert len(design) == 5
    assert len(result.diagnostics) == before + 5


def test_critique_prompt_asks_for_at_most_five_suggestions():
    from barndsl.agent import _CRITIQUE_JSON

    assert "AT MOST 5" in _CRITIQUE_JSON


def test_revision_prompt_pins_smallest_change_and_full_source():
    # A compiling prior (MEDIOCRE) keeps round 2 in *refine* mode — a failed prior
    # would force repair mode instead (see the repair-mode tests).
    client = FakeClient(
        sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied("a"), _satisfied()]
    )
    _agent(client).design("a cottage", max_iterations=2, target_score=None)
    revision = client.stream_prompts[1]
    assert "COMPLETE revised source" in revision
    assert "smallest revision" in revision


# -- transient-API-error retry + best-so-far on failure -----------------------
# write_source's per-attempt stream call retries a transient network/API error
# (APIConnectionError, RateLimitError, a 5xx APIStatusError) with bounded
# backoff; a 4xx propagates. If generation ultimately raises, design() returns
# the best step so far when any round completed, and re-raises only on round 1.


class _FakeError(Exception):
    """A stand-in for a retryable anthropic exception (avoids constructing the
    real SDK types, which want an httpx request/response). `status_code` lets a
    test model a 5xx (retry) vs a 4xx (propagate) APIStatusError."""

    def __init__(self, message: str = "boom", status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _patch_retryable(monkeypatch):
    """Make `_FakeError` the (only) retryable type and neuter the backoff sleep,
    so the transient-retry path runs instantly against the fakes."""
    import barndsl.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_retryable_api_errors", lambda: (_FakeError,))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


class _FlakyClient:
    """A `.messages.stream(...)` that raises a scripted exception (or None for a
    normal reply) on each generation call. Critique calls always succeed with a
    satisfied verdict, so a test can isolate generation reliability."""

    def __init__(self, sources, faults, critique: CritiqueSpec | None = None):
        self._sources = list(sources)
        self._faults = list(faults)  # one per generation call: exc instance or None
        self._critique = critique or _satisfied()
        self.gen_calls = 0
        self.messages = self

    def stream(self, **kwargs):
        if "senior architect" in kwargs.get("system", ""):
            reply = "```json\n" + self._critique.model_dump_json() + "\n```"
            return _FakeStream(reply)
        self.gen_calls += 1
        fault = self._faults.pop(0) if self._faults else None
        if fault is not None:
            raise fault
        return _FakeStream(f"```barn\n{self._sources.pop(0)}```")


def test_transient_api_error_is_retried_then_succeeds(monkeypatch):
    """One APIConnectionError-like fault then a good reply: the retry rescues the
    write, and the design completes normally."""
    _patch_retryable(monkeypatch)
    # First generation call raises, its retry returns CLEAN.
    client = _FlakyClient(sources=[CLEAN], faults=[_FakeError("connection reset")])
    result = _agent(client).design("a cottage", max_iterations=1, target_score=None)

    assert client.gen_calls == 2  # the initial call + one retry
    assert result.iterations == 1 and result.result.ok
    assert result.source == result.history[0].source


def test_4xx_status_error_propagates_without_retry(monkeypatch):
    """A 4xx APIStatusError is the caller's bug (bad request), not a transient
    blip — it must propagate immediately, not be retried."""
    import pytest

    _patch_retryable(monkeypatch)
    client = _FlakyClient(sources=[CLEAN], faults=[_FakeError("bad request", status_code=400)])
    with pytest.raises(_FakeError):
        _agent(client).design("a cottage", max_iterations=1, target_score=None)
    assert client.gen_calls == 1  # no retry on a 4xx


def test_persistent_failure_after_a_good_round_returns_best_so_far(monkeypatch):
    """Round 1 succeeds (MEDIOCRE), round 2's generation fails on every retry:
    design must hand back the best completed iteration, not raise."""
    _patch_retryable(monkeypatch)
    # Round 1: normal MEDIOCRE (fault None). Round 2: a persistent fault that
    # raises on all 3 attempts (initial + 2 retries), so the retry loop exhausts.
    err = _FakeError("gateway down", status_code=503)
    client = _FlakyClient(
        sources=[MEDIOCRE],
        faults=[None, err, err, err],
        critique=_unsatisfied(),  # keeps the loop going past round 1
    )
    result = _agent(client).design("a home", max_iterations=3, target_score=None)

    # Round 1 landed; round 2 raised after its retries and broke the loop.
    assert [s.iteration for s in result.history] == [1]
    assert result.best_iteration == 1
    assert result.source == result.history[0].source
    # 3 attempts on round 2 (initial + 2 retries), plus the 1 good round-1 call.
    assert client.gen_calls == 4


def test_persistent_failure_on_round_one_reraises(monkeypatch):
    """A generation failure that survives its retries on the very first round —
    with no history to fall back on — must re-raise, not swallow."""
    import pytest

    _patch_retryable(monkeypatch)
    client = _FlakyClient(
        sources=[], faults=[_FakeError("down", status_code=500)] * 3
    )
    with pytest.raises(_FakeError):
        _agent(client).design("a home", max_iterations=3, target_score=None)
    assert client.gen_calls == 3  # initial + 2 retries, then it gave up


def test_retryable_errors_tuple_is_empty_without_anthropic(monkeypatch):
    """The retryable tuple resolves lazily and degrades to () when anthropic is
    absent, so catching it never requires the optional extra."""
    import sys

    from barndsl.agent import _retryable_api_errors

    monkeypatch.setitem(sys.modules, "anthropic", None)  # `import anthropic` fails
    assert _retryable_api_errors() == ()


# -- truncation detection -----------------------------------------------------
# A reply cut off at the token cap (stop_reason == "max_tokens") must never be
# silently treated as complete: retry once, and if still truncated fold a
# deterministic TRUNCATED info into that round's feedback (after scoring).


class _TruncStream(_FakeStream):
    """A `_FakeStream` whose final message reports a `stop_reason`, so
    write_source can see a max_tokens truncation."""

    def __init__(self, text: str, stop_reason: str | None = None):
        super().__init__(text)
        self._stop_reason = stop_reason

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason=self._stop_reason,
        )


class _TruncClient:
    """Scripts each generation call's (text, stop_reason); critique always
    succeeds. Lets a test drive first-truncation-then-clean, or double-truncation."""

    def __init__(self, replies, critique: CritiqueSpec | None = None):
        self._replies = list(replies)  # list of (text, stop_reason)
        self._critique = critique or _satisfied()
        self.gen_calls = 0
        self.messages = self

    def stream(self, **kwargs):
        if "senior architect" in kwargs.get("system", ""):
            reply = "```json\n" + self._critique.model_dump_json() + "\n```"
            return _FakeStream(reply)
        self.gen_calls += 1
        text, stop = self._replies.pop(0)
        return _TruncStream(text, stop)


def test_truncated_reply_is_retried_once_then_recovers():
    """A first reply cut off at the cap is retried; a clean second reply is used,
    and no TRUNCATED info is folded (the retry recovered)."""
    from barndsl.agent import BarndoAgent

    client = _TruncClient(
        replies=[
            (f"```barn\n{MEDIOCRE}```", "max_tokens"),  # truncated
            (f"```barn\n{CLEAN}```", "end_turn"),       # clean retry
        ]
    )
    result = BarndoAgent(client=client).design(
        "a cottage", max_iterations=1, target_score=None
    )
    assert client.gen_calls == 2  # the truncated write + its one retry
    codes = [d.code for d in result.history[0].result.diagnostics]
    assert "TRUNCATED" not in codes
    assert 'plan "Stillwater Cottage"' in result.source  # the CLEAN retry won


def test_double_truncation_folds_a_truncated_info_into_the_feedback():
    """When both the write and its retry are truncated, the source is still used
    but a TRUNCATED info rides that round's feedback into the next prompt."""
    from barndsl.agent import BarndoAgent

    client = _TruncClient(
        replies=[
            (f"```barn\n{MEDIOCRE}```", "max_tokens"),  # truncated
            (f"```barn\n{MEDIOCRE}```", "max_tokens"),  # retry also truncated
            (f"```barn\n{CLEAN}```", "end_turn"),       # round 2 recovers
        ],
        critique=_unsatisfied(),  # keep the loop past round 1
    )
    result = BarndoAgent(client=client).design(
        "a cottage", max_iterations=2, target_score=None
    )
    # 2 generation calls in round 1 (both truncated) + 1 in round 2.
    assert client.gen_calls == 3
    step1 = result.history[0]
    assert "TRUNCATED" in [d.code for d in step1.result.diagnostics]
    # The info is folded AFTER scoring, so it does not perturb the score contract.
    from barndsl.agent import _fold_program_nudge
    from barndsl.score import design_score

    fresh = compile_source(MEDIOCRE)
    _fold_program_nudge(fresh)
    assert step1.score.total == design_score(fresh).total  # TRUNCATED did not deduct


def test_truncated_reply_folds_message_text_reaching_the_next_prompt():
    """The TRUNCATED info's message (concise/cut-off wording) reaches the next
    revision prompt through the normal feedback channel."""
    from barndsl.agent import BarndoAgent

    prompts: list = []

    class _Recorder(_TruncClient):
        def stream(self, **kwargs):
            # Round 1 makes TWO generation calls (truncated write + its retry);
            # round 2 makes one. Record every generation prompt so the last one
            # is the round-2 revision that must carry round-1's folded feedback.
            if "senior architect" not in kwargs.get("system", ""):
                prompts.append(kwargs["messages"][0]["content"])
            return super().stream(**kwargs)

    rec = _Recorder(
        replies=[
            (f"```barn\n{MEDIOCRE}```", "max_tokens"),
            (f"```barn\n{MEDIOCRE}```", "max_tokens"),
            (f"```barn\n{CLEAN}```", "end_turn"),
        ],
        critique=_unsatisfied(),
    )
    BarndoAgent(client=rec).design("a cottage", max_iterations=2, target_score=None)
    revision = prompts[-1]  # the round-2 prompt carries round-1's folded feedback
    assert "info TRUNCATED" in revision
    assert "cut off at the output token cap" in revision


# -- the critique gate on `ok`, not just a plan -------------------------------


class _CritCountingClient(FakeClient):
    """A FakeClient that counts critique calls, to prove one was (not) made."""

    def __init__(self, sources, critiques=None):
        super().__init__(sources, critiques)
        self.critique_calls = 0

    def stream(self, **kwargs):
        if "senior architect" in kwargs.get("system", ""):
            self.critique_calls += 1
        return super().stream(**kwargs)


# A plan that partially recovers: it builds a plan (plan is not None) but has
# errors (overlapping rooms, no entry) — so result.ok is False. The critic must
# NOT be called on it (it scores 0 and can never be satisfied).
PARTIAL_WITH_ERRORS = """\
plan "Overlap"
envelope 30 x 24
ceiling 9
program 1 bed
room living: living at 0,0 size 20 x 24
room bed: bedroom at 10,0 size 20 x 24
window living south width 6 offset 8
window bed south width 4 offset 3
"""


def test_critique_not_called_when_plan_has_errors_even_though_plan_exists():
    """A partially-recovered plan (plan is not None) WITH errors is not ok, so it
    must never burn a critique call — it scores 0 and can't satisfy the critic."""
    partial = compile_source(PARTIAL_WITH_ERRORS)
    assert partial.plan is not None and not partial.ok  # the gated case

    # The in-round retry also returns the errored plan (equal badness keeps the
    # original) — and being a generation call, it must not tick the critic either.
    client = _CritCountingClient(
        sources=[PARTIAL_WITH_ERRORS, PARTIAL_WITH_ERRORS], critiques=[]
    )
    result = _agent(client).design("one bed", max_iterations=1, target_score=None)

    assert client.critique_calls == 0  # the ok-gate skipped the critic
    assert result.history[0].critique is None


def test_critique_still_called_on_a_clean_plan():
    """Sanity: the gate does NOT suppress the critic on a genuinely ok plan."""
    client = _CritCountingClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)
    assert client.critique_calls == 1


# -- CritiqueSpec.skipped ------------------------------------------------------


def test_critique_skipped_flag_true_on_the_degraded_paths():
    """Both degraded critique paths (no JSON / call failed) set skipped=True; a
    real parsed critique leaves it False, and the field is additive so a reply
    that omits it still validates."""
    # No parseable JSON -> fail-closed fallback with skipped=True.
    agent = BarndoAgent(client=_FakeStreamClient("Just prose, no JSON here."))
    crit = agent.critique(compile_source(MEDIOCRE))
    assert crit.skipped is True

    # A real parsed critique (fenced JSON) is not skipped.
    real = BarndoAgent(client=_FakeStreamClient(FENCED_CRITIQUE)).critique(
        compile_source(MEDIOCRE)
    )
    assert real.skipped is False

    # Backward compatible: a model reply that omits `skipped` still validates.
    parsed = CritiqueSpec.model_validate_json(
        '{"satisfied": true, "assessment": "ok", "rationale": "clean", "suggestions": []}'
    )
    assert parsed.skipped is False


class _FailingCritiqueClient:
    """A `.messages.stream(...)` that succeeds for generation but raises on the
    critique call, to exercise the call-failed degraded path."""

    def __init__(self, source: str):
        self._source = source
        self.messages = self

    def stream(self, **kwargs):
        if "senior architect" in kwargs.get("system", ""):
            raise RuntimeError("critic endpoint exploded")
        return _FakeStream(f"```barn\n{self._source}```")


def test_critique_call_failure_sets_skipped():
    agent = BarndoAgent(client=_FailingCritiqueClient(CLEAN))
    crit = agent.critique(compile_source(CLEAN))
    assert crit.skipped is True
    assert crit.satisfied is False
    assert "critique call failed" in crit.assessment


def test_skipped_critique_cannot_complete_the_loop():
    agent = BarndoAgent(client=_FailingCritiqueClient(CLEAN))
    result = agent.design("a cottage", max_iterations=1, target_score=None)

    assert result.termination_reason == "max_iterations"
    assert result.review_degraded is True
    assert result.history[0].critique is not None
    assert result.history[0].critique.satisfied is False


# -- the projected-score feedback line ----------------------------------------


def test_projected_score_line_appears_when_errors_gate_the_score():
    """When the errors component pins the total at 0, the header carries a
    projection with the errors deduction backed out, clamped to [0, 100]."""
    result = compile_source(PARTIAL_WITH_ERRORS)
    score = design_score(result)
    assert score.components["errors"] == 100.0 and score.total == 0.0

    text = render_feedback(result, score)
    head = text.splitlines()[0]
    assert head.startswith("Design score: 0/100 (errors block scoring; projected")
    # The projection equals 100 minus the non-errors deductions, clamped.
    others = sum(v for k, v in score.components.items() if k != "errors")
    projected = max(0.0, min(100.0, 100.0 - others))
    assert f"projected once errors are fixed: {projected:g}/100)" in head
    assert "deductions:" in head  # the existing deductions text is kept


def test_projected_score_absent_when_no_errors_gate():
    """A clean or merely-warned plan keeps the plain header — no projection."""
    text = render_feedback(compile_source(MEDIOCRE))
    head = text.splitlines()[0]
    assert "projected once errors are fixed" not in head
    assert head.startswith("Design score: ")


def test_projected_score_reaches_the_revision_prompt_after_a_broken_round():
    """The projection rides the feedback into the next prompt (best-so-far path
    uses the valid source's feedback, but a plain lower-scoring errored round
    surfaces the projection directly)."""
    # BROKEN has no plan; PARTIAL_WITH_ERRORS has a plan-with-errors. Drive a
    # round that produces the partial so the errored feedback shows the projection.
    client = FakeClient(
        sources=[PARTIAL_WITH_ERRORS, CLEAN], critiques=[_satisfied()]
    )
    _agent(client).design("one bed", max_iterations=2, target_score=None)
    revision = client.stream_prompts[1]
    assert "projected once errors are fixed" in revision


# -- the score-history feedback line ------------------------------------------


def test_score_history_line_from_render_feedback():
    """render_feedback adds a trajectory line when given >= 2 totals; fewer than
    two (or None) omits it."""
    result = compile_source(MEDIOCRE)
    text = render_feedback(result, score_history=[41.0, 68.0, 74.0])
    lines = text.splitlines()
    assert lines[1] == "Score history: 41 -> 68 -> 74 (this round)"
    # A single total (or None) draws no history line.
    assert "Score history:" not in render_feedback(result, score_history=[68.0])
    assert "Score history:" not in render_feedback(result)


def test_score_history_absent_at_round_two_without_a_seed():
    """Without a solver seed the round-2 prompt has only ONE scored step (round
    1), so the >= 2 threshold is not met and no history line is drawn yet."""
    client = FakeClient(sources=[MEDIOCRE, CLEAN], critiques=[_unsatisfied(), _satisfied()])
    _agent(client).design("a starter home", max_iterations=2, target_score=None)
    assert "Score history:" not in client.stream_prompts[1]


def test_score_history_appears_from_round_three_without_a_seed():
    """By round 3 two scored steps (rounds 1 and 2) precede the prompt, so the
    trajectory line appears with the true totals."""
    client = FakeClient(
        sources=[MEDIOCRE, MEDIOCRE, CLEAN],
        critiques=[_unsatisfied(), _unsatisfied(), _satisfied()],
    )
    result = _agent(client).design("a starter home", max_iterations=3, target_score=None)
    revision = client.stream_prompts[2]  # the round-3 prompt
    assert "Score history:" in revision
    t1 = result.history[0].score.total
    t2 = result.history[1].score.total
    assert f"{t1:g} -> {t2:g} (this round)" in revision


def test_score_history_shows_two_entries_with_a_solver_seed():
    """A solver seed is iteration 0, so by the round-2 prompt there are >= 2
    scored totals and the history line appears with the true trajectory."""
    client = FakeClient(
        sources=[MEDIOCRE, CLEAN],
        # The solver seed is critiqued too now, so it consumes the first one.
        critiques=[_unsatisfied(), _unsatisfied(), _satisfied()],
    )
    result = _agent(client).design(
        "a small barndo", max_iterations=2, target_score=None,
        seed_with_solver=_solver_brief(),
    )
    revision = client.stream_prompts[1]  # round-2 prompt
    assert "Score history:" in revision
    seed_total = result.history[0].score.total  # iteration 0
    round1_total = result.history[1].score.total
    assert f"{seed_total:g} -> {round1_total:g} (this round)" in revision


# -- blocking issues: critique authority + gating clamp -----------------------
# The critic gets a structured `blocking_issues` channel (room-level structural
# defects that make a plan unshippable regardless of score). When present, the
# loop clamps the step's *gating total* to BLOCKING_CLAMP (65) — the number used
# for BOTH the target gate and best-step selection — while the TRUE score object
# stays untouched. So a 99-point plan whose only path to the bedrooms runs
# through the shop can never end the loop nor out-rank a sound lower-scoring one.


def _blocking(*issues: str, suggestions: tuple[str, ...] = ()) -> CritiqueSpec:
    """An unsatisfied critique carrying blocking structural issues."""
    return CritiqueSpec(
        satisfied=False,
        assessment="Structurally broken.",
        rationale="A room-level defect the score can't see.",
        blocking_issues=list(issues) or ["The only path to the bedrooms runs through the shop."],
        suggestions=list(suggestions) or ["Swap the shop and bed wing; buffer with a mudroom."],
    )


def test_blocking_issues_parsed_from_critique_json():
    """The critique JSON's `blocking_issues` array round-trips through the parser."""
    from barndsl.agent import _critique_from_text

    text = (
        '{"satisfied": false, "assessment": "a", "rationale": "b", '
        '"blocking_issues": ["through-shop path to the bedrooms"], '
        '"suggestions": ["swap the wings"]}'
    )
    crit = _critique_from_text(text)
    assert crit is not None
    assert crit.blocking_issues == ["through-shop path to the bedrooms"]


def test_blocking_issues_default_to_empty_when_field_missing():
    """A reply that omits `blocking_issues` (backward compat) validates to []."""
    from barndsl.agent import _critique_from_text

    text = '{"satisfied": true, "assessment": "a", "rationale": "b", "suggestions": []}'
    crit = _critique_from_text(text)
    assert crit is not None and crit.blocking_issues == []


def test_blocking_issue_caps_gating_total_and_does_not_end_the_loop():
    """A plan whose TRUE score clears the target but whose critique reports a
    blocking issue does NOT satisfy the gate: its gating total is clamped to 65,
    below the target, so the loop keeps going instead of shipping the broken plan."""
    from barndsl.agent import BLOCKING_CLAMP

    # Round 1 CLEAN scores ~99 (> target 90) but is blocked; round 2 is satisfied.
    client = FakeClient(
        sources=[CLEAN, CLEAN],
        critiques=[_blocking(), _satisfied()],
    )
    result = _agent(client).design("a cottage", max_iterations=2, target_score=90.0)

    # The loop did NOT stop after round 1 despite its 99 true score.
    assert result.iterations == 2
    step1 = result.history[0]
    assert step1.score.total > 90.0  # true score really did clear the target
    assert step1.gating_total == BLOCKING_CLAMP  # clamped for gating
    assert step1.score.total > step1.gating_total  # true score untouched


def test_unblocked_lower_score_step_wins_best_selection_over_blocked_higher():
    """Best-step selection ranks on the gating total: an earlier blocked step with
    a higher TRUE score loses to a later unblocked step with a lower true score."""
    from barndsl.agent import BLOCKING_CLAMP, _best_step, _best_valid_step
    from barndsl.score import ScoreReport

    ok = compile_source(CLEAN)  # a real compiling plan for both steps
    blocked_high = DesignStep(
        1, CLEAN, ok, _blocking(), ScoreReport(total=99.0),
        gating_total=BLOCKING_CLAMP,  # clamped
    )
    clean_lower = DesignStep(
        2, CLEAN, ok, _satisfied(), ScoreReport(total=80.0),
        gating_total=80.0,
    )
    history = [blocked_high, clean_lower]

    # True scores would pick the blocked step; gating totals pick the sound one.
    assert blocked_high.score.total > clean_lower.score.total  # 99 > 80
    assert blocked_high.effective_total < clean_lower.effective_total  # 65 < 80
    assert _best_step(history) is clean_lower
    assert _best_valid_step(history) is clean_lower


def test_effective_total_falls_back_to_true_score_when_unclamped():
    """A step with no stored gating_total ranks on its true score (and -1 when
    unscored), so callers that build steps directly keep the old semantics."""
    from barndsl.score import ScoreReport

    ok = compile_source(CLEAN)
    scored = DesignStep(1, CLEAN, ok, None, ScoreReport(total=77.0))
    unscored = DesignStep(2, CLEAN, ok, None, None)
    assert scored.effective_total == 77.0
    assert unscored.effective_total == -1.0


def test_blocking_issue_folds_into_next_round_feedback_with_marker():
    """The blocking issue rides the diagnostic channel into the next prompt,
    marked 'BLOCKING:' and stating the effective-score cap."""
    client = FakeClient(
        sources=[CLEAN, CLEAN],
        critiques=[_blocking("through-shop path to the bedrooms"), _satisfied()],
    )
    _agent(client).design("a cottage", max_iterations=2, target_score=None)

    revision = client.stream_prompts[1]
    assert "BLOCKING:" in revision
    assert "through-shop path to the bedrooms" in revision
    assert "caps this plan's effective score at 65" in revision


# -- restructure mode ---------------------------------------------------------
# The write path takes a `restructure` flag that swaps the revision instruction
# from "smallest local edit" to "reconsider the LAYOUT wholesale". design() turns
# it on for the NEXT round when the score plateaus, the critic blocks, or a
# suggestion repeats across two rounds.

REFINE_MARK = "smallest revision"
RESTRUCTURE_MARK = "Reconsider the LAYOUT"


def test_write_source_refine_vs_restructure_wording():
    """The flag swaps the revision instruction; refine is the default."""
    client = FakeClient(sources=[CLEAN, CLEAN])
    agent = _agent(client)
    agent.write_source("brief", prior=MEDIOCRE, diagnostics="fb")  # refine
    agent.write_source("brief", prior=MEDIOCRE, diagnostics="fb", restructure=True)
    assert REFINE_MARK in _prompt_text(client.stream_prompts[0])
    assert RESTRUCTURE_MARK not in _prompt_text(client.stream_prompts[0])
    assert RESTRUCTURE_MARK in _prompt_text(client.stream_prompts[1])
    assert "# concept:" in _prompt_text(client.stream_prompts[1])


def test_restructure_triggers_on_a_blocking_issue():
    """A blocking issue in round 1 puts round 2 into restructure mode."""
    client = FakeClient(
        sources=[CLEAN, CLEAN],
        critiques=[_blocking(), _satisfied()],
    )
    result = _agent(client).design("a cottage", max_iterations=2, target_score=None)
    assert RESTRUCTURE_MARK in _prompt_text(client.stream_prompts[1])
    assert result.history[1].restructured is True


def test_restructure_triggers_on_a_repeated_suggestion():
    """The same suggestion in two consecutive critiques (normalized) flips round 3
    into restructure mode."""
    client = FakeClient(
        sources=[MEDIOCRE, MEDIOCRE, CLEAN],
        critiques=[
            _unsatisfied("Widen the hall spine."),
            _unsatisfied("  widen   the HALL   spine.  "),  # same, differently cased/spaced
            _satisfied(),
        ],
    )
    _agent(client).design("a starter home", max_iterations=3, target_score=None)
    assert RESTRUCTURE_MARK in _prompt_text(client.stream_prompts[2])


def test_restructure_triggers_on_a_score_plateau():
    """Two consecutive sub-2-point score deltas plateau the loop into restructure.
    MEDIOCRE compiles to the same score every round; after rounds 1-3 (all
    MEDIOCRE) the totals are [44, 44, 44], so the round-4 write restructures.
    Distinct suggestions keep the *repeated-suggestion* trigger from firing, so
    this isolates the plateau path."""
    client = FakeClient(
        sources=[MEDIOCRE, MEDIOCRE, MEDIOCRE, CLEAN],
        critiques=[_unsatisfied("a"), _unsatisfied("b"), _unsatisfied("c"), _satisfied()],
    )
    result = _agent(client).design("a starter home", max_iterations=4, target_score=None)
    # The round-4 write follows three flat rounds: stream_prompts[3].
    assert RESTRUCTURE_MARK in _prompt_text(client.stream_prompts[3])
    assert result.history[3].restructured is True
    # Round 2 and 3 writes precede the plateau (only 1 then 2 totals): still refine.
    assert REFINE_MARK in _prompt_text(client.stream_prompts[1])


def test_big_score_jump_stays_in_refine_mode():
    """A large jump (MEDIOCRE ~44 -> CLEAN ~99) is not a plateau: round 2 keeps
    the refine wording and is not marked restructured."""
    client = FakeClient(
        sources=[MEDIOCRE, CLEAN, CLEAN],
        critiques=[_unsatisfied("a"), _unsatisfied("b"), _satisfied()],
    )
    result = _agent(client).design("a starter home", max_iterations=3, target_score=None)
    # Round 2 prompt (after the big jump into round 1's mediocre... actually the
    # jump lands at round 2's compile). The round-2 write follows round 1 (MEDIOCRE)
    # with only one prior scored delta, so no plateau yet: refine wording, unmarked.
    assert REFINE_MARK in _prompt_text(client.stream_prompts[1])
    assert result.history[1].restructured is False


# -- repair mode --------------------------------------------------------------
# When the PREVIOUS round failed to compile, the next write is a REPAIR round:
# the model reproduces its prior (failed) source and fixes only the erroring
# lines. Repair takes priority over both refine and restructure — a broken plan
# is fixed line by line, never redesigned. A failed round must NOT trigger
# restructure, even when its 0-score makes the raw history look like a plateau.

REPAIR_MARK = "Do NOT redesign"


def test_failed_round_puts_next_write_in_repair_mode():
    """BROKEN (write and in-round retry both fail) -> CLEAN: round 1 stays
    failed, so round 2's write carries the repair wording (and the error
    count), not the restructure wording."""
    client = FakeClient(sources=[BROKEN, BROKEN, CLEAN], critiques=[_satisfied()])
    result = _agent(client).design("a cottage", max_iterations=2, target_score=None)

    revision = _prompt_text(client.stream_prompts[2])
    assert REPAIR_MARK in revision
    assert RESTRUCTURE_MARK not in revision
    assert REFINE_MARK not in revision
    assert "FAILED to compile with 1 error(s)" in revision  # the count is named
    assert result.history[1].repaired is True
    assert result.history[1].restructured is False


def test_repair_prompt_carries_the_previous_failed_source():
    """The repair prompt shows the failed source as the prior DSL, so "reproduce
    it and fix line N" is coherent — the whole point of repair over redesign."""
    client = FakeClient(sources=[BROKEN, BROKEN, CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=2, target_score=None)

    revision = _prompt_text(client.stream_prompts[2])  # round 2's write
    assert "Your previous DSL:" in revision
    assert 'plan "Broken"' in revision  # the failed source rides the prompt
    assert "envelope banana" in revision
    assert "BAD_NUMBER" in revision  # its own error rides the feedback


def test_failed_rounds_do_not_trigger_restructure_on_a_zero_plateau():
    """Three failed rounds (BROKEN) score 0,0,0 — a flat *raw* history. That must
    NOT read as a plateau: every round after a failure is a repair round, never
    a restructure round. Only a COMPILING plateau restructures. (Each failed
    round scripts two broken sources: the write plus its failed in-round retry.)"""
    client = FakeClient(
        sources=[BROKEN] * 6 + [CLEAN],
        critiques=[_satisfied()],  # only the final CLEAN round is critiqued
    )
    result = _agent(client).design("a cottage", max_iterations=4, target_score=None)

    # Rounds 2-4 follow a failed round: repair, never restructure.
    for prompt in client.stream_prompts[1:]:
        assert REPAIR_MARK in _prompt_text(prompt)
        assert RESTRUCTURE_MARK not in _prompt_text(prompt)
    assert all(not s.restructured for s in result.history)
    assert [s.repaired for s in result.history] == [False, True, True, True]


def test_compiling_plateau_still_restructures_despite_earlier_failures():
    """Failed rounds are excluded from the stagnation window, but a genuine
    plateau over COMPILING rounds still restructures. BROKEN then three MEDIOCRE
    (all score ~44) plateau the compiling scores, so the round-5 write
    restructures — the earlier 0 does not disturb the compiling-only window."""
    client = FakeClient(
        sources=[BROKEN, BROKEN, MEDIOCRE, MEDIOCRE, MEDIOCRE, CLEAN],
        critiques=[_unsatisfied("a"), _unsatisfied("b"), _unsatisfied("c"), _satisfied()],
    )
    result = _agent(client).design("a cottage", max_iterations=5, target_score=None)

    # Round 1 fails twice (write + in-round retry); round 2 is repair; rounds
    # 3-4 refine while the compiling window fills; round 5 restructures once
    # three compiling scores plateau. Prompts: [0] R1 write, [1] R1 retry,
    # [2] R2 repair, [3] R3, [4] R4, [5] R5 restructure.
    assert result.history[1].repaired is True
    assert RESTRUCTURE_MARK in _prompt_text(client.stream_prompts[5])
    assert result.history[4].restructured is True


def test_repair_takes_priority_over_restructure_in_the_prompt():
    """When both flags are set, the write prompt uses the repair instruction and
    drops the restructure one — repair wins."""
    client = FakeClient(sources=[CLEAN])
    agent = _agent(client)
    agent.write_source(
        "brief", prior=MEDIOCRE, diagnostics="fb", restructure=True, repair=2
    )
    prompt = _prompt_text(client.stream_prompts[0])
    assert REPAIR_MARK in prompt
    assert "FAILED to compile with 2 error(s)" in prompt
    assert RESTRUCTURE_MARK not in prompt


def test_write_source_repair_wording_is_off_by_default():
    """repair defaults to 0 (off): a plain revision stays in refine mode."""
    client = FakeClient(sources=[CLEAN])
    _agent(client).write_source("brief", prior=MEDIOCRE, diagnostics="fb")
    prompt = _prompt_text(client.stream_prompts[0])
    assert REPAIR_MARK not in prompt
    assert REFINE_MARK in prompt


# -- the in-round repair retry (salvage) ---------------------------------------
# A round whose write fails to compile gets ONE extra generation call in the
# same iteration — the repair-mode prompt on the just-failed source — before the
# round is recorded. Restructure rounds are the usual patient: moving rooms
# wholesale breaks a door offset or two, and under the old flow the redesign
# burned the iteration and the next repair round often reverted it. The retry
# replaces the write only when STRICTLY less broken (no plan at all is worse
# than any recovered plan; recovered plans rank by error count).

BROKEN2 = 'plan "Broken Two"\nenvelope banana\n'

# MEDIOCRE plus one/two doors to rooms that don't exist: the compiler recovers a
# plan but carries one/two errors — for ranking retries by error count.
ONE_ERROR = MEDIOCRE + "door living - ghost width 2.5\n"
TWO_ERRORS = MEDIOCRE + "door living - ghost width 2.5\ndoor bed - ghost2 width 2.5\n"


def test_failed_write_is_salvaged_in_the_same_round():
    """BROKEN then CLEAN in ONE round: the in-round retry fixes the compile, the
    step records the compiling source, and the iteration is not burnt."""
    client = FakeClient(sources=[BROKEN, CLEAN], critiques=[_satisfied()])
    result = _agent(client).design("a cottage", max_iterations=1, target_score=None)

    assert len(result.history) == 1
    step = result.history[0]
    assert step.result.ok and step.salvaged is True
    assert step.source == result.source and result.result.ok
    assert len(client.stream_prompts) == 2  # the write + exactly one retry
    # The retry prompt is repair mode on the just-failed source.
    retry = _prompt_text(client.stream_prompts[1])
    assert REPAIR_MARK in retry
    assert "FAILED to compile with 1 error(s)" in retry
    assert "envelope banana" in retry  # the failed source is the prior
    # The salvaged (compiling) plan still gets its critique.
    assert step.critique is not None and step.critique.satisfied


def test_salvage_retry_that_still_fails_keeps_the_round_failed():
    """When the retry is just as broken, the original write is kept, the round
    stays failed (salvaged=False), and the NEXT round opens in cross-round
    repair mode as before."""
    client = FakeClient(sources=[BROKEN, BROKEN2, CLEAN], critiques=[_satisfied()])
    result = _agent(client).design("a cottage", max_iterations=2, target_score=None)

    step1 = result.history[0]
    assert not step1.result.ok and step1.salvaged is False
    assert step1.source == BROKEN  # equal badness: the original is kept
    step2 = result.history[1]
    assert step2.repaired is True and step2.result.ok


def test_salvage_keeps_a_retry_that_is_strictly_less_broken():
    """A retry that still fails but with FEWER errors replaces the source — the
    next (cross-round) repair starts closer — yet the round is not marked
    salvaged and still counts as failed."""
    two, one = compile_source(TWO_ERRORS), compile_source(ONE_ERROR)
    assert two.plan is not None and len(two.errors) == 2
    assert one.plan is not None and len(one.errors) == 1

    client = FakeClient(sources=[TWO_ERRORS, ONE_ERROR])
    result = _agent(client).design("a cottage", max_iterations=1, target_score=None)
    step = result.history[0]
    assert step.source == ONE_ERROR
    assert not step.result.ok and step.salvaged is False


def test_compiling_write_spends_no_salvage_call():
    """A round whose write compiles makes exactly one generation call."""
    client = FakeClient(sources=[CLEAN], critiques=[_satisfied()])
    _agent(client).design("a cottage", max_iterations=1, target_score=None)
    assert len(client.stream_prompts) == 1


def test_salvage_write_error_keeps_the_failed_round():
    """An exception in the retry call (here: the script runs dry) is contained:
    the failed round is recorded exactly as before instead of crashing the loop."""
    client = FakeClient(sources=[BROKEN])
    result = _agent(client).design("a cottage", max_iterations=1, target_score=None)
    step = result.history[0]
    assert not step.result.ok and step.salvaged is False and step.source == BROKEN


def test_a_salvaged_restructure_round_keeps_its_mode_flag():
    """`restructured` says how the round's write was prompted; `salvaged` says
    its compile was rescued afterwards — both can be true on one step."""
    client = FakeClient(
        sources=[MEDIOCRE, BROKEN, CLEAN],
        critiques=[_blocking("The shop blocks the bedrooms."), _satisfied()],
    )
    result = _agent(client).design("a cottage", max_iterations=2, target_score=None)
    step2 = result.history[1]
    assert step2.restructured is True and step2.salvaged is True and step2.result.ok


# -- pure trigger helpers -----------------------------------------------------


def test_stagnating_detects_small_consecutive_deltas():
    from barndsl.agent import _stagnating

    assert _stagnating([80.0, 80.5, 81.0]) is True  # deltas 0.5, 0.5 < 2
    assert _stagnating([80.0, 90.0, 91.0]) is False  # first delta 10 >= 2
    assert _stagnating([80.0, 81.0, 91.0]) is False  # second delta 10 >= 2
    assert _stagnating([80.0, 81.0]) is False  # only one delta, need window+1
    assert _stagnating([]) is False


def test_stagnating_window_and_eps_are_configurable():
    from barndsl.agent import _stagnating

    # window=1 needs just one small delta; a tighter eps rejects a 1.5 delta.
    assert _stagnating([80.0, 81.0], window=1) is True
    assert _stagnating([80.0, 81.5], window=1, eps=1.0) is False


def test_repeated_suggestion_matches_normalized_across_rounds():
    from barndsl.agent import _repeated_suggestion

    a = _unsatisfied("Widen the hall spine.")
    b = _unsatisfied("  WIDEN the   hall spine. ")  # same normalized
    c = _unsatisfied("Add a porch.")
    assert _repeated_suggestion(a, b) is True
    assert _repeated_suggestion(a, c) is False
    assert _repeated_suggestion(None, b) is False  # a skipped critique never repeats
    assert _repeated_suggestion(a, None) is False


# -- the solver seed faces the critic too -------------------------------------
# Iteration 0 used to skip the critique entirely, so it competed on its TRUE
# score while every LLM round was clamped to BLOCKING_CLAMP on blocking issues —
# an unreviewed 79-point seed could out-rank a blocked true-84 round. The seed
# is now reviewed like any compiling round: it can be clamped, its review is
# folded into round 1's feedback, and a blocked seed licenses a restructure.


def test_solver_seed_is_critiqued_like_any_compiling_round():
    from barndsl.agent import BLOCKING_CLAMP

    client = FakeClient(
        sources=[CLEAN],
        critiques=[_blocking("bedrooms only reachable through the shop"),
                   _satisfied()],
    )
    result = _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=_solver_brief(),
    )
    seed = result.history[0]
    assert seed.iteration == 0
    assert seed.critique is not None and seed.critique.blocking_issues
    # The blocked seed's gating total is clamped exactly like an LLM round's.
    assert seed.effective_total == min(seed.score.total, BLOCKING_CLAMP)


def test_seed_is_not_critiqued_when_critique_is_off():
    client = FakeClient(sources=[CLEAN], critiques=[])
    result = _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None, critique=False,
        seed_with_solver=_solver_brief(),
    )
    assert result.history[0].critique is None  # and no critique was consumed


def test_blocked_seed_puts_round_one_in_restructure_mode():
    client = FakeClient(
        sources=[CLEAN],
        critiques=[_blocking("bedrooms only reachable through the shop"),
                   _satisfied()],
    )
    result = _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=_solver_brief(),
    )
    assert result.history[1].restructured is True
    prompt = _prompt_text(client.stream_prompts[0])
    assert RESTRUCTURE_MARK in prompt


def test_unblocked_seed_keeps_round_one_in_refine_mode():
    client = FakeClient(sources=[CLEAN], critiques=[_unsatisfied(), _satisfied()])
    result = _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=_solver_brief(),
    )
    assert result.history[1].restructured is False
    prompt = _prompt_text(client.stream_prompts[0])
    assert RESTRUCTURE_MARK not in prompt


def test_round_one_prompt_carries_the_seed_report_and_review():
    """The first write sees the seed's rendered feedback (score header) with the
    architect's review folded in — not just the bare seed source."""
    client = FakeClient(
        sources=[CLEAN],
        critiques=[_unsatisfied("Swap the shop and the bedroom wing."),
                   _satisfied()],
    )
    _agent(client).design(
        "a small barndo", max_iterations=1, target_score=None,
        seed_with_solver=_solver_brief(),
    )
    prompt = _prompt_text(client.stream_prompts[0])
    assert "Compiler feedback on it" in prompt
    assert "Design score:" in prompt
    assert "Swap the shop and the bedroom wing." in prompt
