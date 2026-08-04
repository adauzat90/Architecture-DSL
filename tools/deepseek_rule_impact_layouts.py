"""Generate and recompile DeepSeek-agent layout variants for rule-impact review.

This is a small harness around the built-in :class:`barndsl.agent.BarndoAgent`.
It asks a DeepSeek-compatible Anthropic endpoint for several plans that exercise
semantic room types, writes ``.barn`` + ``.svg`` artifacts, then records a compact
``summary.json`` you can diff after changing compiler/validation rules. Generated
plans must compile, meet per-brief room counts/area, clear ``--min-score``, and
receive critic approval unless ``--no-critique`` is explicit. A complete run is
staged and atomically published, so a mid-run provider failure cannot mix corpora.

Typical workflow::

    # 1) Generate a stable set of candidate plans once.
    python tools/deepseek_rule_impact_layouts.py --out .pi/artifacts/rule-layouts \
        --summary-name before.json

    # 2) Change compiler / validation rules, then recompile the SAME plans.
    python tools/deepseek_rule_impact_layouts.py --out .pi/artifacts/rule-layouts \
        --reuse-existing --summary-name after.json --compare-summary .pi/artifacts/rule-layouts/before.json

Environment:
    * ``ANTHROPIC_API_KEY`` must be set for the Anthropic SDK, even when using a
      DeepSeek/OpenRouter/etc. Anthropic-compatible gateway.
    * ``ANTHROPIC_BASE_URL`` may point at that compatible gateway.
    * ``BARNDSL_MODEL`` can override the default model (``deepseek-v4-pro``).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:  # Optional; available when installed with barndsl[agent].
    from dotenv import find_dotenv, load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    find_dotenv = None  # type: ignore[assignment]
    load_dotenv = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for search_root in (ROOT, SRC):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

from barndsl import compile_source, render_svg  # noqa: E402
from barndsl.score import design_score  # noqa: E402
from barndsl.validation import Issue, Severity  # noqa: E402


DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_OUT = Path(".pi/artifacts/deepseek-rule-impact-layouts")
DEFAULT_MIN_SCORE = 60.0

# Briefs intentionally include the new semantic room types so changes to their
# validation rules show up as diagnostic deltas on recompilation.
BRIEF_PREFIX = (
    "Important DSL requirement: use the exact room type tokens requested, not older aliases. "
    "For example write `room foyer: foyer`, `room great: great_room`, "
    "`room storm: safe_room`, `room mech: mechanical`, and `room store: storage`. "
    "For any `shop` or `garage`, include an exterior `door <room> <wall> overhead`. "
    "Keep foyers compact, not long hallway spines. Keep safe rooms compact and interior. "
    "Do not center every door and window by habit: back doors near circulation corners, "
    "place bedroom windows around bed walls, and vary window offsets for views/daylight/privacy. "
    "Do not use 6-10 ft single swing `door` statements between kitchen/dining/living; "
    "use `open ... width ...` for cased public flow or `double`/`french` for real leaf pairs. "
    "Keep kitchen appliances and counters on solid wall runs, not across those openings. "
    "Do not make the kitchen the only route between great/living and dining; "
    "provide a direct public-room opening or hall bypass so traffic avoids the work triangle. "
)


BRIEFS: dict[str, str] = {
    "compact_safe_flex": (
        "Design a compact one-story 2 bed / 2 bath barndominium around 1600 sq ft. "
        "Use room types foyer, great_room, kitchen, dining, flex, safe_room, "
        "mechanical, storage, bedrooms, bathrooms, closets, laundry, and mudroom. "
        "Keep the safe_room interior with no windows, put mechanical off service "
        "circulation, and make the flex room bedroom-ready."
    ),
    "family_rec_room": (
        "Design a family 3 bed / 2.5 bath barndominium around 2200 sq ft with a "
        "clear foyer entry, great_room connected to kitchen/dining, a rec_room, "
        "safe_room, mechanical room, storage room, pantry, closets, and a back door. "
        "Buffer the rec_room from bedrooms and keep private rooms off a hallway."
    ),
    "shop_house": (
        "Design a 2 bed / 2 bath shop-house barndominium around 1900 sq ft plus a "
        "large attached shop/garage bay. Include foyer, mudroom, great_room, kitchen, "
        "dining, flex office, safe_room, mechanical, storage, laundry, pantry, and "
        "closets. Avoid routing bedrooms through the shop and keep the safe_room "
        "inside the dwelling core."
    ),
    "loft_rec_variant": (
        "Design a two-level barndominium concept with a vaulted great_room, kitchen, "
        "dining, foyer, downstairs primary bedroom, upstairs loft or rec_room, flex "
        "guest room, safe_room, mechanical room, storage, laundry, pantry, and closets. "
        "Make room adjacencies and exterior windows satisfy the compiler where possible."
    ),
}

# Each group means "at least one of these room types must be present". Most are
# singletons; the loft brief deliberately permits either ``loft`` or ``rec_room``.
# The harness records and gates this coverage so a high-scoring generic solver
# plan cannot silently replace the semantic-rule corpus the briefs request.
ROOM_TYPE_REQUIREMENTS: dict[str, tuple[frozenset[str], ...]] = {
    "compact_safe_flex": tuple(
        frozenset({room_type})
        for room_type in ("foyer", "great_room", "flex", "safe_room", "mechanical", "storage")
    ),
    "family_rec_room": tuple(
        frozenset({room_type})
        for room_type in ("foyer", "great_room", "rec_room", "safe_room", "mechanical", "storage")
    ),
    "shop_house": tuple(
        frozenset({room_type})
        for room_type in (
            "foyer",
            "great_room",
            "flex",
            "safe_room",
            "mechanical",
            "storage",
        )
    )
    + (frozenset({"shop", "garage"}),),
    "loft_rec_variant": (
        frozenset({"foyer"}),
        frozenset({"great_room"}),
        frozenset({"loft", "rec_room"}),
        frozenset({"flex"}),
        frozenset({"safe_room"}),
        frozenset({"mechanical"}),
        frozenset({"storage"}),
    ),
}

# Machine-checkable brief acceptance.  Unlike ``ROOM_TYPE_REQUIREMENTS`` (which
# exists to ensure the corpus exercises the semantic validators), these groups
# prove that the candidate actually delivered the requested program.  Each tuple
# is ``(acceptable room types, minimum count)``; grouped bathroom/shop options
# keep the checks aligned with the DSL's aggregate program semantics.
BRIEF_COUNT_REQUIREMENTS: dict[
    str, tuple[tuple[frozenset[str], int], ...]
] = {
    "compact_safe_flex": (
        (frozenset({"bedroom"}), 2),
        (frozenset({"bathroom", "half_bath"}), 2),
        (frozenset({"closet"}), 2),
        *((frozenset({kind}), 1) for kind in (
            "foyer", "great_room", "kitchen", "dining", "flex", "safe_room",
            "mechanical", "storage", "laundry", "mudroom",
        )),
    ),
    "family_rec_room": (
        (frozenset({"bedroom"}), 3),
        (frozenset({"bathroom", "half_bath"}), 3),
        (frozenset({"closet"}), 3),
        *((frozenset({kind}), 1) for kind in (
            "foyer", "great_room", "kitchen", "dining", "rec_room", "safe_room",
            "mechanical", "storage", "pantry",
        )),
    ),
    "shop_house": (
        (frozenset({"bedroom"}), 2),
        (frozenset({"bathroom", "half_bath"}), 2),
        (frozenset({"closet"}), 2),
        (frozenset({"shop", "garage"}), 1),
        *((frozenset({kind}), 1) for kind in (
            "foyer", "mudroom", "great_room", "kitchen", "dining", "flex",
            "safe_room", "mechanical", "storage", "laundry", "pantry",
        )),
    ),
    "loft_rec_variant": (
        (frozenset({"bedroom"}), 1),
        (frozenset({"bathroom", "half_bath"}), 1),
        (frozenset({"closet"}), 1),
        (frozenset({"loft", "rec_room"}), 1),
        *((frozenset({kind}), 1) for kind in (
            "foyer", "great_room", "kitchen", "dining", "flex", "safe_room",
            "mechanical", "storage", "laundry", "pantry",
        )),
    ),
}

# Approximate conditioned-area bands from the prose briefs.  Shop/garage rooms
# are excluded so the shop-house's "1900 sq ft plus a bay" is measured correctly.
LIVING_AREA_RANGES: dict[str, tuple[float, float]] = {
    "compact_safe_flex": (1360.0, 1840.0),
    "family_rec_room": (1870.0, 2530.0),
    "shop_house": (1615.0, 2185.0),
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_") or "plan"


def _load_env() -> None:
    if find_dotenv is not None and load_dotenv is not None:
        # Keep the API key and compatible endpoint as one authoritative pair,
        # matching barndsl.cli. An ambient base URL must not capture a key loaded
        # from the project file.
        load_dotenv(find_dotenv(usecwd=True), override=True)


def _requirement_label(options: frozenset[str]) -> str:
    return " or ".join(sorted(options))


def _coverage_for_types(slug: str, room_types: Iterable[str]) -> dict[str, Any]:
    actual = set(room_types)
    requirements = ROOM_TYPE_REQUIREMENTS.get(slug, ())
    required = [_requirement_label(options) for options in requirements]
    missing = [
        _requirement_label(options)
        for options in requirements
        if actual.isdisjoint(options)
    ]
    return {
        "ok": not missing,
        "required": required,
        "missing": missing,
        "actual": sorted(actual),
    }


def _plan_room_types(plan: Any) -> set[str]:
    if plan is None:
        return set()
    return {room.type.value for room in plan.rooms}


def _brief_acceptance(
    slug: str,
    plan: Any,
    *,
    compile_ok: bool,
    score: float,
    min_score: float,
    critique: Any = None,
    require_critique: bool = False,
) -> dict[str, Any]:
    counts = Counter(room.type.value for room in plan.rooms) if plan is not None else Counter()
    requirements = BRIEF_COUNT_REQUIREMENTS.get(slug, ())
    missing: list[str] = []
    required: list[dict[str, Any]] = []
    for options, minimum in requirements:
        label = _requirement_label(options)
        actual = sum(counts[option] for option in options)
        required.append({"types": label, "minimum": minimum, "actual": actual})
        if actual < minimum:
            missing.append(f"{label} x{minimum} (actual {actual})")

    conditioned_area: float | None = None
    area_range = LIVING_AREA_RANGES.get(slug)
    area_ok = True
    if plan is not None and area_range is not None:
        conditioned_area = round(
            sum(
                float(room.area)
                for room in plan.rooms
                if getattr(room, "level", 0) == 0
                and room.type.value not in {"shop", "garage"}
            ),
            3,
        )
        area_ok = area_range[0] <= conditioned_area <= area_range[1]

    review_ok: bool | None = None
    if require_critique:
        review_ok = bool(
            critique is not None
            and not getattr(critique, "skipped", False)
            and getattr(critique, "satisfied", False)
            and not getattr(critique, "blocking_issues", [])
        )

    reasons: list[str] = []
    if not compile_ok:
        reasons.append("candidate does not compile cleanly")
    if score < min_score:
        reasons.append(f"score {score:g} is below minimum {min_score:g}")
    if missing:
        reasons.append("program shortfall: " + ", ".join(missing))
    if not area_ok and conditioned_area is not None and area_range is not None:
        reasons.append(
            f"conditioned area {conditioned_area:g} is outside "
            f"{area_range[0]:g}-{area_range[1]:g} sq ft"
        )
    if review_ok is False:
        if critique is None:
            reasons.append("architectural critique is missing")
        elif getattr(critique, "skipped", False):
            reasons.append("architectural critique was skipped/degraded")
        elif getattr(critique, "blocking_issues", []):
            reasons.append("architectural critique found blocking issues")
        else:
            reasons.append("architectural critique did not approve the plan")

    return {
        "ok": not reasons,
        "compile_ok": compile_ok,
        "score": round(score, 3),
        "min_score": min_score,
        "required_counts": required,
        "missing": missing,
        "conditioned_area": conditioned_area,
        "area_range": list(area_range) if area_range is not None else None,
        "area_ok": area_ok,
        "critique_required": require_critique,
        "critique_ok": review_ok,
        "reasons": reasons,
    }


def _acceptance_feedback_issues(
    slug: str,
    result: Any,
    score: Any,
    critique: Any,
    *,
    min_score: float,
    require_critique: bool,
) -> list[Issue]:
    """Fold harness-only gates into the agent's diagnostic feedback stream."""
    plan = getattr(result, "plan", None)
    total = float(getattr(score, "total", -1.0))
    coverage = _coverage_for_types(slug, _plan_room_types(plan))
    acceptance = _brief_acceptance(
        slug,
        plan,
        compile_ok=bool(getattr(result, "ok", False)),
        score=total,
        min_score=min_score,
        critique=critique,
        require_critique=require_critique,
    )
    messages: list[str] = []
    if coverage["missing"]:
        messages.append(
            "Missing required semantic room type(s): "
            + ", ".join(coverage["missing"])
            + ". Use exact room type tokens from the brief."
        )
    for reason in acceptance["reasons"]:
        # Compile errors already appear as concrete compiler diagnostics; saying
        # the critic is missing on a broken round is just noise because critique
        # intentionally runs only after an error-free compile.
        if reason == "candidate does not compile cleanly":
            continue
        if reason == "architectural critique is missing" and not getattr(result, "ok", False):
            continue
        messages.append("Candidate cannot be selected: " + reason + ".")
    if not messages:
        return []
    return [
        Issue(
            Severity.INFO,
            "BRIEF_ACCEPTANCE",
            message,
            hint=(
                "Revise the next complete source so it satisfies the brief gate; "
                "these harness checks are deterministic and must pass before the "
                "candidate can be kept."
            ),
        )
        for message in messages[:6]
    ]


def _write_rejected_steps(
    slug: str,
    result: Any,
    out: Path,
    *,
    min_score: float,
    require_critique: bool,
) -> None:
    """Persist every rejected agent round for post-mortem inspection."""
    out.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for step in getattr(result, "history", []):
        stem = f"round{getattr(step, 'iteration', len(records) + 1)}"
        source = getattr(step, "source", "") or ""
        (out / f"{stem}.barn").write_text(source.rstrip() + "\n", encoding="utf-8")
        step_result = getattr(step, "result", None)
        if step_result is not None:
            report = step_result.report(stem) if hasattr(step_result, "report") else str(step_result)
            (out / f"{stem}.report.txt").write_text(report, encoding="utf-8")
        score = getattr(getattr(step, "score", None), "total", -1.0)
        critique = getattr(step, "critique", None)
        plan = getattr(step_result, "plan", None) if step_result is not None else None
        records.append(
            {
                "iteration": getattr(step, "iteration", None),
                "ok": bool(getattr(step_result, "ok", False)),
                "score": score,
                "effective_total": getattr(step, "effective_total", score),
                "coverage": _coverage_for_types(slug, _plan_room_types(plan)),
                "acceptance": _brief_acceptance(
                    slug,
                    plan,
                    compile_ok=bool(getattr(step_result, "ok", False)),
                    score=float(score),
                    min_score=min_score,
                    critique=critique,
                    require_critique=require_critique,
                ),
                "diagnostic_codes": [
                    getattr(d, "code", "")
                    for d in getattr(step_result, "diagnostics", [])
                ] if step_result is not None else [],
                "critique": None if critique is None else {
                    "satisfied": getattr(critique, "satisfied", None),
                    "skipped": getattr(critique, "skipped", None),
                    "blocking_issues": list(getattr(critique, "blocking_issues", [])),
                    "assessment": getattr(critique, "assessment", None),
                    "rationale": getattr(critique, "rationale", None),
                    "suggestions": list(getattr(critique, "suggestions", [])),
                },
            }
        )
    (out / "steps.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


def _select_covered_step(
    slug: str,
    result: Any,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    require_critique: bool = False,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    accepted: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    rejected: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    for step in result.history:
        coverage = _coverage_for_types(slug, _plan_room_types(step.result.plan))
        score = step.score.total if step.score is not None else -1.0
        acceptance = _brief_acceptance(
            slug,
            step.result.plan,
            compile_ok=bool(step.result.ok),
            score=score,
            min_score=min_score,
            critique=getattr(step, "critique", None),
            require_critique=require_critique,
        )
        item = (step, coverage, acceptance)
        if step.result.plan is not None and coverage["ok"] and acceptance["ok"]:
            accepted.append(item)
        else:
            rejected.append(item)

    if not accepted:
        coverage = _coverage_for_types(slug, _plan_room_types(result.result.plan))
        missing = ", ".join(coverage["missing"])
        actual = ", ".join(coverage["actual"]) or "none"
        reasons: list[str] = []
        for _step, rejected_coverage, acceptance in rejected:
            reasons.extend(rejected_coverage["missing"])
            reasons.extend(acceptance["reasons"])
        detail = "; ".join(dict.fromkeys(reasons)) or "no candidate plan was produced"
        raise RuntimeError(
            f"{slug}: no agent candidate met semantic coverage and brief acceptance; "
            + (f"missing {missing} (actual: {actual}); " if missing else "")
            + f"{detail}. Increase --iterations, improve the brief, or lower --min-score."
        )

    # Rank only candidates that are clean, brief-compliant, critic-approved when
    # requested, and actually exercise the semantic rules this corpus reviews.
    return max(accepted, key=lambda item: item[0].effective_total)


def _agent_available() -> tuple[bool, str | None]:
    try:
        from barndsl.agent import agent_availability
    except Exception as exc:  # pragma: no cover - missing optional deps
        return False, f"agent import failed: {exc}"
    return agent_availability()


def _run_agent(
    slug: str,
    brief: str,
    *,
    model: str,
    iterations: int,
    target_score: float | None,
    min_score: float,
    critique: bool,
    seed_solver: bool,
    rejected_dir: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    from barndsl.agent import BarndoAgent

    def on_step(step) -> None:
        score = step.score.total if step.score is not None else None
        status = "ok" if step.result.ok else "issues"
        score_s = "?" if score is None else f"{score:.1f}"
        print(
            f"  {slug}: round {step.iteration} {status}, "
            f"score {score_s}, "
            f"{len(step.result.errors)}e/{len(step.result.warnings)}w/{len(step.result.infos)}i"
        )

    agent = BarndoAgent(model=model)
    result = agent.design(
        brief,
        max_iterations=iterations,
        critique=critique,
        on_step=on_step,
        target_score=target_score,
        seed_with_solver=brief if seed_solver else None,
        extra_feedback=lambda res, score, crit: _acceptance_feedback_issues(
            slug,
            res,
            score,
            crit,
            min_score=min_score,
            require_critique=critique,
        ),
    )
    try:
        selected, coverage, acceptance = _select_covered_step(
            slug,
            result,
            min_score=min_score,
            require_critique=critique,
        )
    except RuntimeError:
        if rejected_dir is not None:
            _write_rejected_steps(
                slug,
                result,
                rejected_dir,
                min_score=min_score,
                require_critique=critique,
            )
        raise
    selected_score = selected.score or design_score(selected.result)
    selected_critique = getattr(selected, "critique", None)
    meta = {
        # ``best_iteration`` remains the iteration whose source was persisted.
        # ``agent_best_iteration`` exposes when the coverage gate overruled the
        # agent's raw score winner.
        "best_iteration": selected.iteration,
        "agent_best_iteration": result.best_iteration,
        "iterations": result.iterations,
        "score": round(selected_score.total, 3),
        "coverage": coverage,
        "acceptance": acceptance,
        "termination_reason": getattr(result, "termination_reason", "unknown"),
        "review_degraded": bool(getattr(result, "review_degraded", False)),
        "selected_review": {
            "satisfied": getattr(selected_critique, "satisfied", None),
            "skipped": getattr(selected_critique, "skipped", None),
            "blocking_issues": list(getattr(selected_critique, "blocking_issues", [])),
        },
        "max_tokens": agent.max_tokens,
    }
    return selected.source, meta


def _compile_artifacts(
    path: Path,
    brief: str,
    meta: dict[str, Any] | None = None,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    published_dir: Path | None = None,
) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    result = compile_source(source, name=path.name)
    score = design_score(result)
    svg_path = path.with_suffix(".svg")
    if result.plan is not None:
        svg_path.write_text(render_svg(result.plan), encoding="utf-8")

    diagnostics = [
        {
            "severity": d.severity.value,
            "code": d.code,
            "room": d.room,
            "line": d.line,
            "message": d.message,
            "hint": d.hint,
        }
        for d in sorted(result.diagnostics, key=lambda d: (d.severity.value, d.code, d.line or 0))
    ]
    counts = Counter(d["code"] for d in diagnostics)
    room_types = Counter(r.type.value for r in result.plan.rooms) if result.plan is not None else Counter()
    coverage = _coverage_for_types(path.stem, room_types)
    acceptance = _brief_acceptance(
        path.stem,
        result.plan,
        compile_ok=result.ok,
        score=score.total,
        min_score=min_score,
    )
    public_barn = (published_dir / path.name) if published_dir is not None else path
    public_svg = (published_dir / svg_path.name) if published_dir is not None else svg_path
    return {
        "slug": path.stem,
        "brief": brief,
        "barn": str(public_barn),
        "svg": str(public_svg) if result.plan is not None else None,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "ok": result.ok,
        "score": round(score.total, 3),
        "summary": {
            "errors": len(result.errors),
            "warnings": len(result.warnings),
            "infos": len(result.infos),
        },
        "codes": dict(sorted(counts.items())),
        "room_types": dict(sorted(room_types.items())),
        "coverage": coverage,
        "acceptance": acceptance,
        "diagnostics": diagnostics,
        "agent": meta or {},
    }


def _write_review_html(files: list[Path], out: Path) -> None:
    try:
        from tools.design_review import build_html, design_data
    except Exception as exc:  # pragma: no cover - optional convenience
        print(f"warning: could not build review HTML: {exc}")
        return
    designs = [design_data(str(p)) for p in files]
    out.write_text(build_html(designs), encoding="utf-8")


def _totals(plans: list[dict[str, Any]]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for plan in plans:
        total.update(plan.get("codes", {}))
    return dict(sorted(total.items()))


def _count_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    b = Counter(before)
    a = Counter(after)
    keys = sorted(set(a) | set(b))
    return {
        "introduced": {k: a[k] for k in keys if b[k] == 0 and a[k] > 0},
        "resolved": {k: b[k] for k in keys if b[k] > 0 and a[k] == 0},
        "increased": {
            k: {"before": b[k], "after": a[k]}
            for k in keys
            if a[k] > b[k] > 0
        },
        "reduced": {
            k: {"before": b[k], "after": a[k]}
            for k in keys
            if 0 < a[k] < b[k]
        },
    }


def _has_count_delta(delta: dict[str, Any]) -> bool:
    return any(delta[field] for field in ("introduced", "resolved", "increased", "reduced"))


def _diagnostic_delta(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    def signatures(items: list[dict[str, Any]]) -> Counter[str]:
        return Counter(
            json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for item in items
        )

    def expand(delta: Counter[str]) -> list[dict[str, Any]]:
        expanded: list[dict[str, Any]] = []
        for signature in sorted(delta):
            item = json.loads(signature)
            occurrences = delta[signature]
            if occurrences > 1:
                item["occurrences"] = occurrences
            expanded.append(item)
        return expanded

    b = signatures(before)
    a = signatures(after)
    return {
        "introduced": expand(a - b),
        "resolved": expand(b - a),
    }


def _compare(before_path: Path, after: dict[str, Any]) -> dict[str, Any]:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    aggregate = _count_delta(
        before.get("totals", {}).get("codes", {}),
        after.get("totals", {}).get("codes", {}),
    )
    before_plans = {
        str(plan["slug"]): plan for plan in before.get("plans", []) if "slug" in plan
    }
    after_plans = {
        str(plan["slug"]): plan for plan in after.get("plans", []) if "slug" in plan
    }
    plan_changes: dict[str, Any] = {}
    for slug in sorted(set(before_plans) | set(after_plans)):
        before_plan = before_plans.get(slug)
        after_plan = after_plans.get(slug)
        change: dict[str, Any] = {}
        if before_plan is None or after_plan is None:
            change["presence"] = {
                "before": before_plan is not None,
                "after": after_plan is not None,
            }

        b_plan = before_plan or {}
        a_plan = after_plan or {}
        for field in ("ok", "score", "summary", "room_types", "coverage", "acceptance"):
            if b_plan.get(field) != a_plan.get(field):
                change[field] = {"before": b_plan.get(field), "after": a_plan.get(field)}

        codes = _count_delta(b_plan.get("codes", {}), a_plan.get("codes", {}))
        if _has_count_delta(codes):
            change["codes"] = codes

        diagnostics = _diagnostic_delta(
            b_plan.get("diagnostics", []), a_plan.get("diagnostics", [])
        )
        if diagnostics["introduced"] or diagnostics["resolved"]:
            change["diagnostics"] = diagnostics

        if change:
            plan_changes[slug] = change

    result = {
        "before": str(before_path),
        **aggregate,
        "plans": plan_changes,
    }
    result["has_changes"] = _has_count_delta(aggregate) or bool(plan_changes)
    return result


def _safe_artifact_name(value: str, label: str) -> str:
    """Require a filename, never a path that can escape the staged run."""
    candidate = Path(value)
    if not value or candidate.name != value or candidate.is_absolute() or value in {".", ".."}:
        raise ValueError(f"{label} must be a filename without directory components")
    return value


def _stage_output(destination: Path) -> Path:
    """Copy the current corpus into a same-volume staging directory."""
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )
    if destination.exists():
        if not destination.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f"output path is not a directory: {destination}")
        shutil.copytree(destination, staging, dirs_exist_ok=True)
    return staging


def _publish_staged_directory(staging: Path, destination: Path) -> None:
    """Atomically swap a complete staged corpus into ``destination``.

    The old directory is first renamed to a unique backup on the same volume.
    If publishing fails, it is restored before the exception escapes.  Readers
    therefore see either the complete old run or the complete new run, never the
    per-plan mixture produced by in-place writes.
    """
    destination = destination.resolve()
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
    had_destination = destination.exists()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if had_destination and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _git_provenance() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    status = git("status", "--short")
    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


def _endpoint_origin() -> str:
    raw = os.environ.get("ANTHROPIC_BASE_URL")
    if not raw:
        return "anthropic-default"
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        return "custom"
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _prompt_sha256() -> str:
    try:
        from barndsl.agent import _GENERATE_SYSTEM
    except Exception:
        system = ""
    else:
        system = _GENERATE_SYSTEM
    material = system + "\n" + BRIEF_PREFIX + json.dumps(BRIEFS, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    # Load before constructing argparse defaults so BARNDSL_MODEL from .env is
    # treated the same as an exported environment variable.
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="artifact directory")
    parser.add_argument("--model", default=os.environ.get("BARNDSL_MODEL", DEFAULT_MODEL))
    parser.add_argument("--iterations", type=int, default=2, help="agent refinement rounds per brief")
    parser.add_argument(
        "--target-score",
        type=float,
        default=None,
        help="score gate; omit to disable for faster variant generation",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help=f"minimum score for a corpus candidate (default: {DEFAULT_MIN_SCORE:g})",
    )
    parser.add_argument("--no-critique", action="store_true", help="skip critic calls")
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument(
        "--seed-solver",
        action="store_true",
        help="opt in to a deterministic iteration-0 seed (disabled by default for agent-authored variants)",
    )
    seed_group.add_argument(
        "--no-seed-solver",
        dest="seed_solver",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(seed_solver=False)
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(BRIEFS),
        help="run only this built-in brief slug; repeatable",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="do not call the agent; recompile existing .barn files in --out",
    )
    parser.add_argument("--summary-name", default="summary.json", help="summary JSON file name")
    parser.add_argument("--review-name", default="review.html", help="static review HTML file name")
    parser.add_argument("--compare-summary", type=Path, help="previous summary JSON to diff against")
    args = parser.parse_args(argv)

    if args.iterations <= 0:
        parser.error("--iterations must be positive")
    if args.min_score < 0 or args.min_score > 100:
        parser.error("--min-score must be between 0 and 100")
    try:
        summary_name = _safe_artifact_name(args.summary_name, "--summary-name")
        review_name = _safe_artifact_name(args.review_name, "--review-name")
    except ValueError as exc:
        parser.error(str(exc))

    destination = args.out.resolve()
    if destination == ROOT.resolve():
        parser.error("--out must be a dedicated artifact directory, not the repository root")

    selected = {k: BRIEF_PREFIX + BRIEFS[k] for k in (args.only or BRIEFS.keys())}
    staging = _stage_output(destination)
    try:
        barn_files: list[Path] = []
        agent_meta: dict[str, dict[str, Any]] = {}

        if args.reuse_existing:
            missing_files = [slug for slug in selected if not (staging / f"{slug}.barn").exists()]
            if missing_files:
                raise SystemExit(
                    f"missing expected .barn files in {destination}: {', '.join(sorted(missing_files))}; "
                    "run without --reuse-existing first"
                )
            # Only the built-in corpus slugs participate. Stray .barn artifacts in
            # the directory cannot silently pollute totals or before/after diffs.
            barn_files = [staging / f"{slug}.barn" for slug in selected]
            print(f"Recompiling {len(barn_files)} existing plan(s) in {destination}")
        else:
            ok, reason = _agent_available()
            if not ok:
                raise SystemExit(
                    "DeepSeek/agent unavailable: "
                    f"{reason}. Install barndsl[agent] and set ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL."
                )
            print(f"Generating {len(selected)} layout variant(s) with model {args.model}")
            for slug, brief in selected.items():
                print(f"\n== {slug} ==")
                source, meta = _run_agent(
                    slug,
                    brief,
                    model=args.model,
                    iterations=args.iterations,
                    target_score=args.target_score,
                    min_score=args.min_score,
                    critique=not args.no_critique,
                    seed_solver=args.seed_solver,
                    rejected_dir=staging / "rejected" / slug,
                )
                path = staging / f"{_slug(slug)}.barn"
                path.write_text(source.rstrip() + "\n", encoding="utf-8")
                barn_files.append(path)
                agent_meta[path.stem] = meta

        # Generation follows brief order while reuse follows the selected map;
        # normalize both so before/after JSON remains directly diffable.
        barn_files.sort(key=lambda path: path.stem)
        plans: list[dict[str, Any]] = []
        for path in barn_files:
            brief = selected.get(path.stem, "")
            plans.append(
                _compile_artifacts(
                    path,
                    brief,
                    agent_meta.get(path.stem),
                    min_score=args.min_score,
                    published_dir=destination,
                )
            )

        summary = {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "out": str(destination),
            "run": {
                "mode": "reuse" if args.reuse_existing else "generate",
                "iterations": args.iterations,
                "target_score": args.target_score,
                "min_score": args.min_score,
                "critique": not args.no_critique,
                "seed_solver": args.seed_solver,
                "selected_briefs": sorted(selected),
            },
            "provenance": {
                **_git_provenance(),
                "endpoint": _endpoint_origin(),
                "prompt_sha256": _prompt_sha256(),
            },
            "plans": plans,
            "totals": {"codes": _totals(plans)},
        }
        if args.compare_summary:
            summary["comparison"] = _compare(args.compare_summary, summary)

        staged_summary = staging / summary_name
        staged_summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        staged_review = staging / review_name
        _write_review_html(barn_files, staged_review)

        failures = [
            plan
            for plan in plans
            if not plan["coverage"]["ok"] or not plan["acceptance"]["ok"]
        ]
        if failures:
            failed_destination = destination.parent / (
                f"{destination.name}.failed-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            os.replace(staging, failed_destination)
            print("Corpus acceptance failures:", file=sys.stderr)
            for plan in failures:
                reasons = list(plan["acceptance"]["reasons"])
                reasons.extend(f"missing semantic type {item}" for item in plan["coverage"]["missing"])
                print(f"  {plan['slug']}: {'; '.join(reasons)}", file=sys.stderr)
            print(f"Failed run preserved at {failed_destination}", file=sys.stderr)
            return 1

        _publish_staged_directory(staging, destination)
        public_summary = destination / summary_name
        public_review = destination / review_name
        print(f"\nWrote {public_summary}")
        print(f"Wrote {public_review}")
        print("Diagnostic totals:")
        for code, count in summary["totals"]["codes"].items():
            print(f"  {code}: {count}")
        if "comparison" in summary:
            print("Comparison:")
            print(json.dumps(summary["comparison"], indent=2))
        return 0
    except Exception:
        if staging.exists():
            failed_destination = destination.parent / (
                f"{destination.name}.failed-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            os.replace(staging, failed_destination)
            print(f"Failed run preserved at {failed_destination}", file=sys.stderr)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
