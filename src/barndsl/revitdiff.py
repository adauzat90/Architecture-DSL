"""Report the drift between a Revit model export and the authored ``.barn`` plan.

The data behind ``barndsl revit-diff MODEL.json PLAN.barn`` — the last mile of
the agent↔architect loop. The agent builds a plan, the extension pushes it into
Revit, the architect *nudges* walls/doors/rooms there, and the extension exports
the edited model back out as a ``barndsl.revit/1`` exchange. ``revit-diff`` then
makes the difference visible: which rooms/openings/walls moved, were added, were
removed, or changed kind — so the authored source can be reconciled with what the
building actually became.

Design
------
Both sides are **lowered the same way** before anything is compared: whatever the
input (a :class:`~barndsl.elements.Barndominium`, a
:class:`~barndsl.compiler.CompileResult`, or an already-loaded exchange dict) is
normalised to a plan and re-lowered through
:func:`~barndsl.revit.to_revit_model` → ``to_dict``. That keeps the diff **pure
and Revit-free**, and — crucially — makes the model side (which came out of
Revit, where :func:`read_model` guesses room types and collapses rooms to their
bounding boxes) comparable *apples-to-apples* with the authored side, because
both pass through the identical lowering.

Matching is **id-first, then position**. Rooms carry stable ids; opening and wall
ids are canonical-but-positional (``o0``, ``w3``), so an element the architect
re-drew in Revit usually arrives with a *different* id. Ids present on both sides
are paired directly; the leftovers fall back to deterministic greedy
nearest-centroid pairing within a match radius, and those pairs are flagged
``matched by position``. Anything still unpaired is ``added`` (only on the model
side) or ``removed`` (only on the authored side).

Pure and deterministic; :func:`diff_plans` returns a JSON-able dict.
"""

from __future__ import annotations

import math
from typing import Any

from .compiler import CompileResult, compile_source
from .elements import Barndominium
from .revit import EXCHANGE_SCHEMA, exchange_to_plan, to_revit_model

#: Geometry tolerance (feet): a matched pair whose coordinates differ by more
#: than this is reported ``moved``/``resized``. Half a foot absorbs the
#: wall-thickness-scale noise a lossy Revit read introduces (rooms come back as
#: bounding boxes, not centreline rectangles) without swallowing a real nudge.
DEFAULT_TOLERANCE = 0.5

#: Nearest-centroid fallback radius (feet): when ids don't match, two elements
#: within this distance are treated as the *same* element the architect moved,
#: rather than an unrelated add + remove. Independent of the move tolerance —
#: one asks "is this the same element?", the other "did it move?".
POSITION_MATCH_RADIUS = 5.0


class DiffInputError(ValueError):
    """A diff input couldn't be read, or a ``.barn`` side didn't compile cleanly."""


# --- normalisation -----------------------------------------------------------


def _to_plan(side: Any) -> Barndominium:
    """Coerce an accepted input to a :class:`Barndominium`."""
    if isinstance(side, Barndominium):
        return side
    if isinstance(side, CompileResult):
        if side.plan is None:
            raise DiffInputError("input did not compile to a plan")
        return side.plan
    if isinstance(side, dict):
        # Raises RevitImportError (a ValueError) if it isn't a barndsl.revit/1 doc.
        return exchange_to_plan(side)
    raise TypeError("unsupported diff input: %r" % type(side).__name__)


def _to_exchange(side: Any) -> dict:
    """Lower any accepted input to a ``barndsl.revit/1`` exchange dict.

    Both sides go through the same ``to_revit_model → to_dict`` lowering so the
    comparison is on one canonical representation (see the module docstring).
    """
    return to_revit_model(_to_plan(side)).to_dict()


def _to_result(side: Any) -> CompileResult | None:
    """A :class:`CompileResult` for the score delta, or ``None`` if it won't compile."""
    if isinstance(side, CompileResult):
        return side
    from .emit import emit_dsl

    try:
        plan = _to_plan(side)
        return compile_source(emit_dsl(plan), name=plan.name)
    except Exception:
        return None


# --- entity extraction -------------------------------------------------------
#
# Each kind reduces to {id, geo (numeric fields), fields (categorical), centroid,
# label}. ``geo`` drives moved/resized; ``fields`` drives rekinded/changed;
# ``centroid`` drives the position fallback.


def _rooms(ex: dict) -> dict[str, dict]:
    out = {}
    for r in ex.get("rooms", []):
        x, y, w, l = r["x"], r["y"], r["width"], r["length"]
        out[r["id"]] = {
            "id": r["id"],
            "geo": {"x": x, "y": y, "width": w, "length": l},
            "fields": {"kind": r.get("type"), "level": r.get("level", 0)},
            "centroid": (x + w / 2.0, y + l / 2.0),
            "label": r.get("name") or r["id"],
        }
    return out


def _openings(ex: dict, windows: bool) -> dict[str, dict]:
    out = {}
    for o in ex.get("openings", []):
        is_window = o.get("category") == "window"
        if is_window != windows:
            continue
        loc = o.get("location", [0.0, 0.0])
        geo = {"x": loc[0], "y": loc[1], "width": o.get("width", 0.0),
               "height": o.get("height", 0.0)}
        if windows:
            geo["sill"] = o.get("sill", 0.0)
        out[o["id"]] = {
            "id": o["id"],
            "geo": geo,
            "fields": {
                "kind": o.get("kind"),
                "level": o.get("level", 0),
                "host_wall": o.get("host_wall"),
                "exterior": o.get("exterior"),
            },
            "centroid": (loc[0], loc[1]),
            "label": o["id"],
        }
    return out


def _walls(ex: dict) -> dict[str, dict]:
    out = {}
    for w in ex.get("walls", []):
        s = w.get("start", [0.0, 0.0])
        e = w.get("end", [0.0, 0.0])
        out[w["id"]] = {
            "id": w["id"],
            "geo": {"x1": s[0], "y1": s[1], "x2": e[0], "y2": e[1]},
            "fields": {
                "kind": w.get("kind"),
                "level": w.get("level", 0),
                "exterior": w.get("exterior"),
            },
            "centroid": ((s[0] + e[0]) / 2.0, (s[1] + e[1]) / 2.0),
            "label": w["id"],
        }
    return out


# --- the diff ----------------------------------------------------------------


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _geo_delta(old: dict, new: dict, tol: float) -> dict:
    """Per-field ``{from, to, delta}`` for numeric fields differing beyond ``tol``."""
    changes = {}
    for k, ov in old.items():
        o = float(ov)
        n = float(new.get(k, o))
        if abs(n - o) > tol:
            changes[k] = {"from": round(o, 4), "to": round(n, 4), "delta": round(n - o, 4)}
    return changes


def _field_delta(old: dict, new: dict) -> dict:
    """Per-field ``{from, to}`` for categorical fields that differ."""
    changes = {}
    for k, o in old.items():
        n = new.get(k)
        if o != n:
            changes[k] = {"from": o, "to": n}
    return changes


def _at(ent: dict) -> list[float]:
    return [round(ent["centroid"][0], 4), round(ent["centroid"][1], 4)]


def _diff_kind(model: dict[str, dict], authored: dict[str, dict], tol: float) -> dict:
    """Diff one kind. ``model`` is the (edited) Revit side, ``authored`` the source.

    ``added`` = only in the model, ``removed`` = only in the authored plan. Same
    element (by id, or by nearest position when ids differ) with geometry past
    ``tol`` → ``moved``; with categorical fields differing → ``changed``; neither
    → counted in ``unchanged``.
    """
    result: dict[str, Any] = {
        "added": [], "removed": [], "moved": [], "changed": [], "unchanged": 0
    }
    pairs: list[tuple[dict, dict, bool]] = []  # (model_ent, authored_ent, by_position)

    # 1. Stable-id matches.
    for k in sorted(set(model) & set(authored)):
        pairs.append((model[k], authored[k], False))
    model_left = [model[k] for k in sorted(model) if k not in authored]
    auth_left = [authored[k] for k in sorted(authored) if k not in model]

    # 2. Greedy nearest-centroid fallback (deterministic: sort candidates by
    #    distance, then by index, and claim each side once).
    radius = max(POSITION_MATCH_RADIUS, tol)
    candidates = []
    for mi, m in enumerate(model_left):
        for ai, a in enumerate(auth_left):
            d = _dist(m["centroid"], a["centroid"])
            if d <= radius:
                candidates.append((round(d, 6), mi, ai))
    candidates.sort()
    used_m: set[int] = set()
    used_a: set[int] = set()
    for _d, mi, ai in candidates:
        if mi in used_m or ai in used_a:
            continue
        used_m.add(mi)
        used_a.add(ai)
        pairs.append((model_left[mi], auth_left[ai], True))

    for mi, m in enumerate(model_left):
        if mi not in used_m:
            result["added"].append({"id": m["id"], "label": m["label"], "at": _at(m)})
    for ai, a in enumerate(auth_left):
        if ai not in used_a:
            result["removed"].append({"id": a["id"], "label": a["label"], "at": _at(a)})

    # 3. Classify matched pairs.
    for m, a, by_pos in pairs:
        geo = _geo_delta(a["geo"], m["geo"], tol)
        fields = _field_delta(a["fields"], m["fields"])
        if geo:
            entry = {"id": m["id"], "label": m["label"], "deltas": geo}
            if a["id"] != m["id"]:
                entry["authored_id"] = a["id"]
            if by_pos:
                entry["matched_by"] = "position"
            result["moved"].append(entry)
        if fields:
            entry = {"id": m["id"], "label": m["label"], "changes": fields}
            if a["id"] != m["id"]:
                entry["authored_id"] = a["id"]
            if by_pos:
                entry["matched_by"] = "position"
            result["changed"].append(entry)
        if not geo and not fields:
            result["unchanged"] += 1

    result["added"].sort(key=lambda e: e["id"])
    result["removed"].sort(key=lambda e: e["id"])
    result["moved"].sort(key=lambda e: e["id"])
    result["changed"].sort(key=lambda e: e["id"])
    return result


_KINDS = ("rooms", "doors", "windows", "walls")


def diff_plans(
    model: Any,
    authored: Any,
    names: tuple[str, str] = ("model", "authored"),
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Diff the (edited) Revit ``model`` against the ``authored`` plan.

    Each side is a :class:`Barndominium`, a :class:`CompileResult`, or a loaded
    ``barndsl.revit/1`` exchange dict — normalised and re-lowered the same way.
    Returns a deterministic, JSON-able dict: per-kind ``added``/``removed``/
    ``moved``/``changed`` plus an ``unchanged`` count, a one-line ``summary``,
    the ``drift`` flag the CLI exits on, and (when both sides compile) the score
    delta from :func:`barndsl.compare.compare_plans`.
    """
    tol = float(tolerance)
    model_ex = _to_exchange(model)
    auth_ex = _to_exchange(authored)

    kinds = {
        "rooms": _diff_kind(_rooms(model_ex), _rooms(auth_ex), tol),
        "doors": _diff_kind(_openings(model_ex, False), _openings(auth_ex, False), tol),
        "windows": _diff_kind(_openings(model_ex, True), _openings(auth_ex, True), tol),
        "walls": _diff_kind(_walls(model_ex), _walls(auth_ex), tol),
    }

    totals = {"added": 0, "removed": 0, "moved": 0, "changed": 0, "unchanged": 0}
    for kd in kinds.values():
        for bucket in ("added", "removed", "moved", "changed"):
            totals[bucket] += len(kd[bucket])
        totals["unchanged"] += kd["unchanged"]
    drift = any(totals[b] for b in ("added", "removed", "moved", "changed"))

    counts = {
        "rooms": len(model_ex.get("rooms", [])),
        "doors": len(_openings(model_ex, False)),
        "windows": len(_openings(model_ex, True)),
        "walls": len(model_ex.get("walls", [])),
    }
    summary = (
        "%d moved, %d added, %d removed, %d changed across "
        "%d rooms / %d doors / %d windows / %d walls"
        % (
            totals["moved"], totals["added"], totals["removed"], totals["changed"],
            counts["rooms"], counts["doors"], counts["windows"], counts["walls"],
        )
    )

    out: dict[str, Any] = {
        "names": {"model": names[0], "authored": names[1]},
        "tolerance": tol,
        "kinds": kinds,
        "totals": totals,
        "counts": counts,
        "drift": drift,
        "summary": summary,
        "score": _score_delta(model, authored),
    }
    return out


def _score_delta(model: Any, authored: Any) -> dict | None:
    """The design-score delta via :func:`compare_plans`, or ``None`` if either
    side doesn't compile to a scorable plan."""
    from .compare import compare_plans

    m_res, a_res = _to_result(model), _to_result(authored)
    if m_res is None or a_res is None or m_res.plan is None or a_res.plan is None:
        return None
    # compare_plans deltas are (second − first); pass authored first so the delta
    # reads "how the score changed from the authored plan to the Revit model".
    cmp = compare_plans(a_res, m_res, ("authored", "model"))
    return {
        "authored": cmp["a"]["score"],
        "model": cmp["b"]["score"],
        "delta": cmp["deltas"]["score"],
    }


# --- human rendering ---------------------------------------------------------


def _fmt_deltas(deltas: dict) -> str:
    return ", ".join(
        f"{k} {v['from']:g}→{v['to']:g} ({v['delta']:+g})" for k, v in sorted(deltas.items())
    )


def _fmt_changes(changes: dict) -> str:
    return ", ".join(f"{k} {v['from']}→{v['to']}" for k, v in sorted(changes.items()))


def diff_text(d: dict[str, Any]) -> str:
    """The compact human rendering of a :func:`diff_plans` result."""
    model, authored = d["names"]["model"], d["names"]["authored"]
    lines = [
        f"Drift: {model} vs {authored}  (tolerance {d['tolerance']:g} ft)",
        d["summary"],
    ]
    score = d.get("score")
    if score is not None:
        lines.append(
            f"Score: authored {score['authored']:g} → model {score['model']:g}"
            f"  (Δ {score['delta']:+g})"
        )

    for kind in _KINDS:
        kd = d["kinds"][kind]
        if not (kd["added"] or kd["removed"] or kd["moved"] or kd["changed"]):
            continue
        lines.append(f"{kind.capitalize()}:")
        for e in kd["moved"]:
            tag = " [by position]" if e.get("matched_by") == "position" else ""
            lines.append(f"  moved    {e['label']}: {_fmt_deltas(e['deltas'])}{tag}")
        for e in kd["changed"]:
            tag = " [by position]" if e.get("matched_by") == "position" else ""
            lines.append(f"  changed  {e['label']}: {_fmt_changes(e['changes'])}{tag}")
        for e in kd["added"]:
            lines.append(f"  added    {e['label']} at ({e['at'][0]:g},{e['at'][1]:g})")
        for e in kd["removed"]:
            lines.append(f"  removed  {e['label']} at ({e['at'][0]:g},{e['at'][1]:g})")

    if not d["drift"]:
        lines.append("No drift: the model matches the authored plan.")
    return "\n".join(lines)


# --- CLI input loading -------------------------------------------------------


def load_diff_input(path: str) -> Any:
    """Load a ``.barn`` source or a ``barndsl.revit/1`` ``.json`` exchange.

    Sniffs by extension, then by content (a leading ``{`` reads as JSON). Returns
    a :class:`CompileResult` for a ``.barn`` file or the exchange dict for JSON.
    Raises :class:`DiffInputError` on an unreadable file, a non-exchange JSON, or
    a ``.barn`` that errors or only half-parses (a diff against a broken plan is
    meaningless).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise DiffInputError(str(exc)) from exc

    lower = path.lower()
    is_json = lower.endswith(".json")
    is_barn = lower.endswith(".barn")
    if not is_json and not is_barn:
        is_json = text.lstrip().startswith("{")  # sniff by content

    if is_json:
        import json

        try:
            data = json.loads(text)
        except ValueError as exc:
            raise DiffInputError("not valid JSON: %s" % exc) from exc
        if not isinstance(data, dict) or data.get("schema") != EXCHANGE_SCHEMA:
            raise DiffInputError(
                "not a %s exchange (got schema %r)"
                % (EXCHANGE_SCHEMA, (data or {}).get("schema"))
            )
        return data

    import os

    result = compile_source(text, name=os.path.splitext(os.path.basename(path))[0])
    if result.plan is None or result.recovered or result.errors:
        raise DiffInputError("plan did not compile cleanly (fix errors first)")
    return result
