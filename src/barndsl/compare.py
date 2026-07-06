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
from .cost import estimate_cost
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
    "counter_linear_ft",
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


def _cost_expected(result: CompileResult) -> float | None:
    """The expected total cost for a *cleanly* compiled side, or ``None``.

    A side that failed to compile — no plan, or any error-severity diagnostic —
    has no trustworthy takeoff, so the comparison omits the cost line rather than
    quote a number built on a half-parsed plan."""
    if not result.ok:
        return None
    try:
        return float(estimate_cost(result.plan)["total"]["expected"])
    except (ValueError, KeyError):
        return None


def compare_plans(
    a: CompileResult, b: CompileResult, names: tuple[str, str] = ("a", "b")
) -> dict[str, Any]:
    """A deterministic side-by-side of two compiles.

    Diagnostic codes are bucketed by how their *count* moved A → B:

    * ``resolved`` — present in A, **gone entirely** (count 0) in B.
    * ``introduced`` — brand new in B (absent from A).
    * ``fewer`` — present in both, count **dropped but still fires** (``[a, b]``).
    * ``more`` — present in both, count **rose** (``[a, b]``).

    ``resolved``/``introduced`` map code → count; ``fewer``/``more`` map code →
    ``[count_a, count_b]``. ``deltas`` carry B − A for the score and every shared
    metric. ``cost`` carries ``{a, b, delta}`` expected totals when *both* sides
    compiled cleanly (omitted otherwise — see :func:`_cost_expected`).
    """
    side_a, side_b = _side(names[0], a), _side(names[1], b)
    codes_a = Counter(d.code for d in a.diagnostics)
    codes_b = Counter(d.code for d in b.diagnostics)
    resolved = {c: n for c, n in sorted(codes_a.items()) if codes_b[c] == 0}
    introduced = {c: n for c, n in sorted(codes_b.items()) if codes_a[c] == 0}
    fewer = {
        c: [na, codes_b[c]]
        for c, na in sorted(codes_a.items())
        if 0 < codes_b[c] < na
    }
    more = {
        c: [codes_a[c], nb]
        for c, nb in sorted(codes_b.items())
        if codes_a[c] > 0 and nb > codes_a[c]
    }
    metric_deltas = {
        k: round(side_b["metrics"][k] - side_a["metrics"][k], 2)
        for k in _METRIC_KEYS
        if k in side_a["metrics"] and k in side_b["metrics"]
    }
    out: dict[str, Any] = {
        "a": side_a,
        "b": side_b,
        "deltas": {"score": round(side_b["score"] - side_a["score"], 1), **metric_deltas},
        "resolved": resolved,
        "introduced": introduced,
        "fewer": fewer,
        "more": more,
    }
    cost_a, cost_b = _cost_expected(a), _cost_expected(b)
    if cost_a is not None and cost_b is not None:
        out["cost"] = {"a": cost_a, "b": cost_b, "delta": round(cost_b - cost_a, 2)}
    return out


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
    if "cost" in cmp:
        c = cmp["cost"]
        lines.append(
            f"Cost: {_money(c['a'])} → {_money(c['b'])} ({_signed_money(c['delta'])})"
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
    if cmp.get("fewer"):
        lines.append(
            "Fewer in %s: %s"
            % (b["name"], ", ".join(f"{c} ({na} → {nb})" for c, (na, nb) in cmp["fewer"].items()))
        )
    if cmp.get("more"):
        lines.append(
            "More in %s: %s"
            % (b["name"], ", ".join(f"{c} ({na} → {nb})" for c, (na, nb) in cmp["more"].items()))
        )
    if not any(cmp.get(k) for k in ("resolved", "introduced", "fewer", "more")):
        lines.append("Diagnostics: identical code sets.")
    return "\n".join(lines)


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _signed_money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"
