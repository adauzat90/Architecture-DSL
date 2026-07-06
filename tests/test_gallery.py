"""The worked-example gallery: a curated set of verified-clean plans.

Each `examples/gallery/*.barn` is meant to compile **0 errors / 0 warnings /
0 infos** — a known-good plan an author or agent can copy and adapt. These tests
recompile every gallery plan and assert it stays clean, so the gallery can't
silently rot as the rules evolve (a new check that a gallery plan violates fails
here, forcing either the plan or the check to be reconsidered).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from barndsl import compile_file, compile_source, emit_dsl
from barndsl.validation import Severity

GALLERY = Path(__file__).resolve().parent.parent / "examples" / "gallery"
PLANS = sorted(GALLERY.glob("*.barn"))


def test_gallery_is_non_empty():
    assert PLANS, f"no gallery plans found in {GALLERY}"


@pytest.mark.parametrize("path", PLANS, ids=lambda p: p.stem)
def test_gallery_plan_is_pristine(path: Path):
    """Every gallery plan compiles with zero *unaccepted* diagnostics of any
    severity. A plan may carry a documented `# barndsl: accept CODE` pragma for a
    non-suppressible checklist reminder (two_story's STAIR_HANDRAIL — a rail the
    DSL can't draw); an accepted diagnostic is a deliberate, audited clean choice,
    so it doesn't count against pristineness."""
    result = compile_file(str(path))
    assert result.plan is not None, result.report(path.name)
    active = [d for d in result.diagnostics if not getattr(d, "accepted", False)]
    assert not [d for d in active if d.severity is Severity.ERROR], result.report(path.name)
    assert not [d for d in active if d.severity is Severity.WARNING], result.report(path.name)
    assert not [d for d in active if d.severity is Severity.INFO], result.report(path.name)


@pytest.mark.parametrize("path", PLANS, ids=lambda p: p.stem)
def test_gallery_plan_round_trips_clean(path: Path):
    """Emitting and recompiling a gallery plan stays clean and stable."""
    plan = compile_file(str(path)).plan
    src = emit_dsl(plan)
    again = compile_source(src, name=plan.name)
    assert again.plan is not None and not again.errors, again.report(path.name)
    # The emit is a fixed point: emitting the recompiled plan reproduces it.
    assert emit_dsl(again.plan) == src
