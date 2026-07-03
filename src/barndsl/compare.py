"""Compare two compiled plans: score, takeoff, program, diagnostics.

The data behind ``barndsl compare A.barn B.barn`` — a scheme-A-vs-scheme-B
view built entirely from what the compiler already computes: the design
score with its per-component causes, the ``metrics()`` takeoff, and the
diagnostic multisets. Serves three audiences at once: an agent's best-of-N
selection gets explainable, the architect gets a client-ready side-by-side,
and CI gets plan-regression review ("this edit cost 6 points and introduced
DOOR_SWING_CLASH").

Pure and deterministic; :func:`compare_plans` returns a JSON-able dict.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .compiler import CompileResult
from .score import design_score

#: The takeoff lines worth comparing side by side (present-in-metrics only).
_METRIC_KEYS = (
    "footprint_sqft",
    "interior_sqft",
    "habitable_sqft",
    "unassigned_sqft",
    "bedroom_count",
    "bathroom_count",
    "exterior_wall_area_sqft",
    "roof_area_sqft",
    "beam_linear_ft",
    "post_count",
)


def _side(name: str, result: CompileResult) -> dict[str, Any]:
    score = design_score(result)
    out: dict[str, Any] = {
        "name": name,
        "score": score.total,
        "components": dict(score.components),
        "details": dict(score.details),
        "counts": dict(score.counts),
        "codes": dict(sorted(Counter(d.code for d in result.diagnostics).items())),
        "metrics": {},
    }
    if result.plan is not None:
        m = result.plan.metrics()
        out["metrics"] = {k: m[k] for k in _METRIC_KEYS if k in m}
    return out


def compare_plans(
    a: CompileResult, b: CompileResult, names: tuple[str, str] = ("a", "b")
) -> dict[str, Any]:
    """A deterministic side-by-side of two compiles.

    ``resolved`` are diagnostic codes present in A but gone (or rarer) in B;
    ``introduced`` the reverse — each mapping code → how many. ``deltas``
    carry B − A for the score and every shared metric.
    """
    side_a, side_b = _side(names[0], a), _side(names[1], b)
    codes_a = Counter(d.code for d in a.diagnostics)
    codes_b = Counter(d.code for d in b.diagnostics)
    resolved = {c: n for c, n in sorted((codes_a - codes_b).items())}
    introduced = {c: n for c, n in sorted((codes_b - codes_a).items())}
    metric_deltas = {
        k: round(side_b["metrics"][k] - side_a["metrics"][k], 2)
        for k in _METRIC_KEYS
        if k in side_a["metrics"] and k in side_b["metrics"]
    }
    return {
        "a": side_a,
        "b": side_b,
        "deltas": {"score": round(side_b["score"] - side_a["score"], 1), **metric_deltas},
        "resolved": resolved,
        "introduced": introduced,
    }


def comparison_text(cmp: dict[str, Any]) -> str:
    """The compact human rendering (one screen, no tables to scroll)."""
    a, b, deltas = cmp["a"], cmp["b"], cmp["deltas"]
    lines = [
        f"Score: {a['name']} {a['score']:g} vs {b['name']} {b['score']:g}"
        f"  (Δ {deltas['score']:+g})"
    ]
    comps = sorted(set(a["components"]) | set(b["components"]))
    for name in comps:
        pa, pb = a["components"].get(name, 0), b["components"].get(name, 0)
        if pa or pb:
            lines.append(f"  {name:<12} -{pa:g} vs -{pb:g}")
    if a["metrics"] or b["metrics"]:
        lines.append("Takeoff (B − A in parentheses):")
        for k in _METRIC_KEYS:
            if k in a["metrics"] and k in b["metrics"]:
                d = deltas.get(k, 0)
                lines.append(
                    f"  {k:<24} {a['metrics'][k]:g} vs {b['metrics'][k]:g}"
                    + (f"  ({d:+g})" if d else "")
                )
    if cmp["resolved"]:
        lines.append(
            "Resolved in %s: %s"
            % (b["name"], ", ".join(f"{c} x{n}" if n > 1 else c for c, n in cmp["resolved"].items()))
        )
    if cmp["introduced"]:
        lines.append(
            "Introduced in %s: %s"
            % (b["name"], ", ".join(f"{c} x{n}" if n > 1 else c for c, n in cmp["introduced"].items()))
        )
    if not cmp["resolved"] and not cmp["introduced"]:
        lines.append("Diagnostics: identical code sets.")
    return "\n".join(lines)
