"""Developer/harness helpers for adding barndsl rules and DSL features.

These functions are intentionally stdlib-only and JSON-shaped so both the CLI
(`barndsl dev ...`) and external agent harnesses can use the same checks.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .compiler import DSL_REFERENCE, _KEYWORDS, compile_source, read_source_file
from .diagnostics import REGISTRY
from .profiles import load_profile
from .score import design_score

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ROOT = Path.cwd() if (Path.cwd() / "src" / "barndsl").exists() else _PACKAGE_ROOT
SRC = ROOT / "src"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _is_fragment_source(text: str) -> bool:
    return not any(line.strip().lower().startswith("plan ") for line in text.splitlines())


def _compile_path(path: Path, profile=None):
    text, base_dir = read_source_file(str(path))
    fragment = _is_fragment_source(text)
    return compile_source(
        text,
        profile=profile,
        fragment=fragment,
        base_dir=base_dir,
        self_path=str(path.resolve()),
    ), fragment


def _expand_paths(items: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in items:
        p = Path(item)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_dir():
            out.extend(sorted(p.rglob("*.barn")))
        else:
            out.extend(sorted(p.parent.glob(p.name)) if any(ch in p.name for ch in "*?") else [p])
    # Stable de-dupe, preserving order.
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        key = p.resolve() if p.exists() else p
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


# --- repo audit --------------------------------------------------------------


def _literal_issue_codes() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in (SRC / "barndsl").glob("*.py"):
        if path.name == "diagnostics.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        codes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name != "Issue" or len(node.args) < 2:
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", arg.value):
                    codes.add(arg.value)
        if codes:
            out[_rel(path)] = sorted(codes)
    return out


def repo_audit() -> dict[str, Any]:
    parser = set(_KEYWORDS)
    from .playground import _STATEMENT_KEYWORDS, _highlight_tokens
    from . import lsp

    playground = set(_STATEMENT_KEYWORDS)
    lsp_quickfix = set(getattr(lsp, "_QUICKFIX_STATEMENTS", ()))
    highlighted = set(_highlight_tokens().get("statements", ()))
    emitted_by_file = _literal_issue_codes()
    emitted = {c for codes in emitted_by_file.values() for c in codes}
    registered = set(REGISTRY)

    missing_pg = sorted(parser - playground)
    extra_pg = sorted(playground - parser)
    missing_ref = sorted(k for k in parser if not re.search(r"\b" + re.escape(k) + r"\b", DSL_REFERENCE))
    missing_registry = sorted(emitted - registered)
    quickfix_drift = sorted(playground ^ lsp_quickfix)
    highlight_drift = sorted(playground ^ highlighted)
    unexplained = sorted(
        code for code, info in REGISTRY.items()
        if not info.title.strip() or len(info.explanation.strip()) < 20
    )

    # Informational hygiene only: ignored artifacts should not fail an audit, but
    # showing a sample helps notice accidental tracked/generated files in sessions.
    pyc = [str(p.relative_to(ROOT)) for p in list((ROOT / "src").rglob("*.pyc"))[:10]]
    agent_artifacts = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob("agent-run-*"))

    problems: list[str] = []
    if missing_pg:
        problems.append("playground missing parser keywords: " + ", ".join(missing_pg))
    if extra_pg:
        problems.append("playground has non-parser keywords: " + ", ".join(extra_pg))
    if missing_ref:
        problems.append("DSL_REFERENCE missing keywords: " + ", ".join(missing_ref))
    if missing_registry:
        problems.append("diagnostic codes missing registry entries: " + ", ".join(missing_registry))
    if quickfix_drift:
        problems.append("LSP quickfix statement-head set drifts from playground: " + ", ".join(quickfix_drift))
    if highlight_drift:
        problems.append("playground highlight statement list drifts from exported statement list: " + ", ".join(highlight_drift))
    if unexplained:
        problems.append("registry entries with missing/too-short explanation: " + ", ".join(unexplained))

    return {
        "ok": not problems,
        "problems": problems,
        "statement_keywords": {
            "parser_count": len(parser),
            "playground_count": len(playground),
            "lsp_quickfix_count": len(lsp_quickfix),
            "missing_in_playground": missing_pg,
            "extra_in_playground": extra_pg,
            "missing_in_dsl_reference": missing_ref,
            "lsp_quickfix_drift": quickfix_drift,
            "highlight_drift": highlight_drift,
        },
        "diagnostics": {
            "emitted_literal_issue_codes": len(emitted),
            "registered_codes": len(registered),
            "missing_registry": missing_registry,
            "unexplained_registry": unexplained,
            "registered_not_seen_as_literal_issue": sorted(registered - emitted),
        },
        "artifacts": {"pyc_sample": pyc, "agent_run_root_artifacts": agent_artifacts},
    }


# --- rule probe --------------------------------------------------------------


def rule_probe(source: str, expect: dict[str, Any] | None = None, *, name: str | None = None, profile: str | None = None) -> dict[str, Any]:
    prof = load_profile(profile) if profile else None
    result = compile_source(source, name=name, profile=prof)
    payload = result.to_dict()
    diags = payload["diagnostics"]
    by_sev: dict[str, set[str]] = {"error": set(), "warning": set(), "info": set()}
    all_codes: set[str] = set()
    for d in diags:
        by_sev[d["severity"]].add(d["code"])
        all_codes.add(d["code"])
    failures: list[str] = []
    expect = expect or {}
    for sev in ("error", "warning", "info"):
        for code in expect.get(sev, []) or []:
            if code not in by_sev[sev]:
                failures.append(f"expected {sev} {code}")
    for code in expect.get("codes", []) or []:
        if code not in all_codes:
            failures.append(f"expected code {code}")
    for code in expect.get("absent", []) or []:
        if code in all_codes:
            failures.append(f"expected absent {code}")
    return {
        "ok": not failures,
        "compile_ok": result.ok,
        "counts": payload["counts"],
        "codes": dict(sorted(Counter(d["code"] for d in diags).items())),
        "failures": failures,
        "diagnostics": diags,
    }


# --- diagnostic diff / gallery gate -----------------------------------------


def _plan_summary(path: Path, profile=None) -> tuple[Counter, dict[str, Any]]:
    result, fragment = _compile_path(path, profile)
    codes = Counter(d.code for d in result.diagnostics)
    accepted = Counter(d.code for d in result.diagnostics if getattr(d, "accepted", False))
    score = None
    if result.plan is not None and not fragment:
        score = design_score(result).to_dict()
    return codes, {
        "ok": result.ok,
        "fragment": fragment,
        "counts": result.to_dict()["counts"],
        "codes": dict(sorted(codes.items())),
        "accepted": dict(sorted(accepted.items())),
        "score": score,
    }


def diagnostic_diff(before: list[str], after: list[str] | None = None, *, profile: str | None = None) -> dict[str, Any]:
    prof = load_profile(profile) if profile else None
    before_paths = _expand_paths(before)
    after_paths = _expand_paths(after or before)

    def collect(paths: list[Path]) -> tuple[Counter, dict[str, Any]]:
        total: Counter = Counter()
        files: dict[str, Any] = {}
        for p in paths:
            c, info = _plan_summary(p, prof)
            total.update(c)
            files[_rel(p)] = info
        return total, files

    b, bf = collect(before_paths)
    a, af = collect(after_paths)
    codes = sorted(set(b) | set(a))
    scores = [v["score"]["total"] for v in af.values() if v.get("score")]
    return {
        "before_files": len(before_paths),
        "after_files": len(after_paths),
        "after_fragments": sum(1 for v in af.values() if v.get("fragment")),
        "after_whole_plans": sum(1 for v in af.values() if not v.get("fragment")),
        "score": {
            "min": min(scores) if scores else None,
            "max": max(scores) if scores else None,
            "avg": round(sum(scores) / len(scores), 2) if scores else None,
        },
        "resolved": {c: b[c] for c in codes if b[c] and not a[c]},
        "introduced": {c: a[c] for c in codes if a[c] and not b[c]},
        "reduced": {c: [b[c], a[c]] for c in codes if 0 < a[c] < b[c]},
        "increased": {c: [b[c], a[c]] for c in codes if a[c] > b[c] > 0},
        "before": dict(sorted(b.items())),
        "after": dict(sorted(a.items())),
        "files": {"before": bf, "after": af},
    }


# --- feature checks ----------------------------------------------------------


_TEXT_SUFFIXES = {".py", ".md", ".barn", ".json", ".txt", ".ts"}


def _iter_text_files(paths: list[Path]):
    for root in paths:
        if not root.exists():
            continue
        files = root.rglob("*") if root.is_dir() else [root]
        for p in files:
            if p.is_dir() or p.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            if any(part in {".git", "__pycache__", "artifacts"} for part in p.parts):
                continue
            yield p


def _grep_mentions(paths: list[Path], needle: str) -> list[str]:
    out: list[str] = []
    low = needle.lower()
    for p in _iter_text_files(paths):
        try:
            if low in p.read_text(encoding="utf-8", errors="ignore").lower():
                out.append(_rel(p))
        except OSError:
            continue
    return sorted(out)


def _line_matches(paths: list[Path], needle: str, *, max_results: int = 50) -> list[dict[str, Any]]:
    """Return compact grep-like hits with paths relative to the repo root."""
    out: list[dict[str, Any]] = []
    low = needle.lower()
    for p in _iter_text_files(paths):
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            if low not in line.lower():
                continue
            out.append({"path": _rel(p), "line": i, "text": line.strip()[:240]})
            if len(out) >= max_results:
                return out
    return out


def _find_line(path: Path, needle: str) -> int | None:
    if not path.exists():
        return None
    low = needle.lower()
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        if low in line.lower():
            return i
    return None


def locate(query: str, *, max_results: int = 80) -> dict[str, Any]:
    """Locate likely implementation, docs and tests for a diagnostic/statement/feature.

    This is a repo-native companion to ``rg`` for agents: it normalizes common
    barndsl concepts (diagnostic codes, DSL statement heads, CLI/dev commands)
    and returns deterministic JSON with line-numbered breadcrumbs.
    """
    raw = query.strip()
    key = raw.lower()
    code = raw.upper()
    kinds: list[str] = []
    exact: dict[str, Any] = {}
    parser = SRC / "barndsl" / "compiler.py"
    diagnostics = SRC / "barndsl" / "diagnostics.py"
    cli = SRC / "barndsl" / "cli.py"
    module = SRC / "barndsl" / f"{key}.py"
    test_module = ROOT / "tests" / f"test_{key}.py"

    if code in REGISTRY:
        info = REGISTRY[code]
        kinds.append("diagnostic")
        exact["diagnostic"] = {
            "code": code,
            "severity": info.severity.value,
            "category": info.category,
            "owner": info.owner,
            "title": info.title,
            "registry": {"path": _rel(diagnostics), "line": _find_line(diagnostics, f'_c("{code}"')},
            "explain": f"barndsl explain {code}",
        }
    if key in _KEYWORDS:
        kinds.append("statement")
        exact["statement"] = {
            "keyword": key,
            "compiler_keywords": {"path": _rel(parser), "line": _find_line(parser, f'"{key}"')},
            "dsl_reference": {"path": _rel(parser), "line": _find_line(parser, f"{key} ") or _find_line(parser, f"{key}:")},
            "playground": "src/barndsl/playground.py:_STATEMENT_KEYWORDS derives from compiler._KEYWORDS",
            "lsp": "src/barndsl/lsp.py:_QUICKFIX_STATEMENTS derives from playground._STATEMENT_KEYWORDS",
        }
    command_line = _find_line(cli, f'add_parser("{key}"') or _find_line(cli, f"add_parser('{key}'")
    top_commands = {"compile", "build", "score", "inspect", "demo", "layout", "design", "revit", "revit-import", "fmt", "schedule", "new", "dxf", "ifc", "gltf", "view3d", "serve", "lsp", "elevation", "section", "watch", "compare", "revit-diff", "cost", "packet", "revit-log", "explain", "dev", "profiles"}
    dev_commands = {"audit", "rule-probe", "diag-diff", "gallery-gate", "lsp-smoke", "doctor", "feature-check", "locate", "diag-matrix", "fixtures", "impact", "export-parity", "rule-scaffold", "feature-scaffold"}
    if command_line and key in top_commands:
        kinds.append("cli_command")
        exact["cli_command"] = {"path": _rel(cli), "line": command_line, "run": f"barndsl {key} --help"}
    if command_line and key in dev_commands:
        kinds.append("dev_command")
        exact["dev_command"] = {"path": _rel(cli), "line": command_line, "run": f"barndsl dev {key} --help"}
    if module.exists():
        kinds.append("module")
        exact["module"] = {"path": _rel(module)}
    if test_module.exists():
        exact["test_module"] = {"path": _rel(test_module)}

    source_roots = [SRC / "barndsl", ROOT / "tools", ROOT / ".pi" / "extensions"]
    matches = {
        "source": _line_matches(source_roots, raw, max_results=max_results),
        "tests": _line_matches([ROOT / "tests"], raw, max_results=max_results // 2),
        "docs": _line_matches([ROOT / "README.md", ROOT / "docs", ROOT / ".pi" / "README.md"], raw, max_results=max_results // 2),
        "examples": _line_matches([ROOT / "examples"], raw, max_results=max_results // 4),
    }
    suggestions: list[str] = []
    if code in REGISTRY:
        suggestions.append(f"Use `barndsl explain {code}` for the human-facing rule rationale, then inspect source/tests hits.")
    if key in _KEYWORDS:
        suggestions.append(f"Use `barndsl dev feature-check {key}` after changing the statement wiring.")
    if not any(matches.values()) and not exact:
        suggestions.append("No direct hits found; try a shorter synonym or run `rg <term> src tests docs examples`.")
    if not kinds:
        kinds.append("text")
    return {"ok": bool(exact or any(matches.values())), "query": raw, "kind": kinds, "exact": exact, "matches": matches, "suggestions": suggestions}


# --- diagnostic matrix / fixture catalog ------------------------------------


def _literal_emitters_by_code() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, codes in _literal_issue_codes().items():
        for code in codes:
            out.setdefault(code, []).append(path)
    return {code: sorted(paths) for code, paths in sorted(out.items())}


def diagnostic_matrix(paths: list[str] | None = None, *, max_hits: int = 6) -> dict[str, Any]:
    """Return an actionable matrix for every registered diagnostic code.

    The matrix is intentionally heuristic/static for speed: literal ``Issue``
    emitters, test/doc/example mentions, and current example impact are enough to
    tell a maintainer where to start before tightening a rule.
    """
    paths = paths or ["examples"]
    diag_path = SRC / "barndsl" / "diagnostics.py"
    emitters = _literal_emitters_by_code()
    impact = diagnostic_diff(paths)
    accepted_total: Counter = Counter()
    impacted_files: dict[str, list[str]] = {}
    for path, info in impact["files"]["after"].items():
        for code, count in (info.get("codes") or {}).items():
            impacted_files.setdefault(code, []).extend([path] * int(count))
        accepted_total.update(info.get("accepted") or {})

    rows: list[dict[str, Any]] = []
    gap_counts: Counter = Counter()
    category_counts: Counter = Counter()
    doc_sources = [ROOT / "README.md", ROOT / ".agents"]
    docs_dir = ROOT / "docs"
    if docs_dir.exists():
        doc_sources.extend(sorted(p for p in docs_dir.rglob("*.md") if p.name not in {"DIAGNOSTIC_MATRIX.md", "FIXTURE_CATALOG.md"}))
    for code in sorted(REGISTRY):
        info = REGISTRY[code]
        test_hits = _line_matches([ROOT / "tests"], code, max_results=max_hits)
        doc_hits = _line_matches(doc_sources, code, max_results=max_hits)
        example_hits = _line_matches([ROOT / "examples"], code, max_results=max_hits)
        category_counts[info.category] += 1
        gaps: list[str] = []
        if not emitters.get(code):
            gaps.append("no_literal_emitter")
        if not test_hits:
            gaps.append("no_test_mention")
        if not doc_hits:
            gaps.append("no_doc_mention")
        if not example_hits and not impacted_files.get(code):
            gaps.append("no_example_mention_or_impact")
        for gap in gaps:
            gap_counts[gap] += 1
        rows.append({
            "code": code,
            "severity": info.severity.value,
            "category": info.category,
            "owner": info.owner,
            "title": info.title,
            "registry": {"path": _rel(diag_path), "line": _find_line(diag_path, f'_c("{code}"')},
            "emitters": emitters.get(code, []),
            "tests": test_hits,
            "docs": doc_hits,
            "examples": example_hits,
            "example_impact": sorted(set(impacted_files.get(code, []))),
            "accepted_example_count": int(accepted_total.get(code, 0)),
            "first_repro_hint": test_hits[0] if test_hits else None,
            "gaps": gaps,
        })
    return {
        "ok": True,
        "paths": paths,
        "count": len(rows),
        "gap_summary": dict(sorted(gap_counts.items())),
        "category_summary": dict(sorted(category_counts.items())),
        "example_summary": {k: impact[k] for k in ("after_files", "after_whole_plans", "after_fragments", "score")},
        "rows": rows,
    }


def _md_path(path: str) -> str:
    return path.replace("\\", "/")


def diagnostic_matrix_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        "# Diagnostic rule matrix",
        "",
        "Generated from `barndsl dev diag-matrix`. Use this as a navigation aid, not as a replacement for focused tests.",
        "",
        f"- Registered diagnostics: {matrix['count']}",
        f"- Example paths: {', '.join(matrix['paths'])}",
        f"- Gap summary: `{json.dumps(matrix['gap_summary'], sort_keys=True)}`",
        f"- Category summary: `{json.dumps(matrix.get('category_summary', {}), sort_keys=True)}`",
        "",
        "| Code | Sev | Category / owner | Registry | Emitters | Tests | Docs | Examples / impact | Gaps |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in matrix["rows"]:
        reg = row["registry"]
        registry = f"{_md_path(reg['path'])}:{reg['line']}" if reg.get("line") else _md_path(reg["path"])
        emitters = "<br>".join(_md_path(p) for p in row["emitters"][:4]) or "—"
        tests = "<br>".join(f"{_md_path(h['path'])}:{h['line']}" for h in row["tests"][:3]) or "—"
        docs = "<br>".join(f"{_md_path(h['path'])}:{h['line']}" for h in row["docs"][:2]) or "—"
        examples = sorted({*(_md_path(h["path"]) for h in row["examples"][:2]), *(_md_path(p) for p in row["example_impact"][:3])})
        ex = "<br>".join(examples) or "—"
        if row["accepted_example_count"]:
            ex += f"<br>accepted: {row['accepted_example_count']}"
        gaps = ", ".join(row["gaps"]) or "—"
        title = row["title"].replace("|", "\\|")
        owner = row.get("owner", "")
        lines.append(f"| `{row['code']}`<br>{title} | {row['severity']} | {row.get('category', 'quality')}<br>{owner} | {registry} | {emitters} | {tests} | {docs} | {ex} | {gaps} |")
    lines.append("")
    return "\n".join(lines)


def _file_features(path: Path, text: str, info: dict[str, Any]) -> list[str]:
    low = text.lower()
    features: list[str] = []
    if info.get("fragment"):
        features.append("fragment")
    else:
        features.append("whole_plan")
    if re.search(r"^\s*use\s+", text, re.MULTILINE):
        features.append("composed")
    if re.search(r"^\s*param\s+", text, re.MULTILINE):
        features.append("parametric_part")
    if "level 1" in low or re.search(r"^\s*stair\s+", text, re.MULTILINE):
        features.append("multi_level")
    if "shop" in low:
        features.append("shop")
    if "barndsl: accept" in low:
        features.append("accepted_diagnostics")
    if path.name == "lshape.barn":
        features.append("export_parity_candidate")
    return features


def fixture_catalog(paths: list[str] | None = None) -> dict[str, Any]:
    """Catalog high-value .barn fixtures/examples for tests and agent prompts."""
    paths = paths or ["examples"]
    expanded = [p for p in _expand_paths(paths) if p.suffix == ".barn"]
    diff = diagnostic_diff(paths)
    files: list[dict[str, Any]] = []
    by_rel = diff["files"]["after"]
    for p in expanded:
        rel = _rel(p)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            text = ""
        info = by_rel.get(rel, {})
        files.append({
            "path": rel,
            "fragment": bool(info.get("fragment")),
            "ok": bool(info.get("ok", False)),
            "features": _file_features(p, text, info),
            "codes": info.get("codes", {}),
            "accepted": info.get("accepted", {}),
            "score_total": (info.get("score") or {}).get("total"),
            "rooms": (info.get("score") or {}).get("areas", {}).get("rooms") if info.get("score") else None,
            "purpose": _fixture_purpose(rel),
        })

    def pick(role: str, rel: str, purpose: str) -> dict[str, Any] | None:
        path = ROOT / rel
        if not path.exists():
            return None
        return {"role": role, "path": rel, "purpose": purpose}

    roles = [
        {"role": "minimal_valid_inline", "kind": "inline", "purpose": "Smallest useful whole-plan smoke fixture for parser/diagnostic probes.", "source": 'plan "Minimal"\nenvelope 20 x 16\nceiling 9\nroom living: living at 0,0 size 20 x 16\nentry living south width 3\nwindow living south width 4 offset 6\n'},
        pick("canonical_example", "examples/cedar_ridge.barn", "Full hand-authored sample with house + shop geometry."),
        pick("gallery_export", "examples/gallery/lshape.barn", "Stable whole plan for render/export parity and scoring checks."),
        pick("composed_smoke", "examples/composed/cedar_ridge.barn", "Composition/stamped-id smoke fixture with accepted deviations."),
        pick("composed_nested_parametric", "examples/composed/cedar_ridge_v2.barn", "Nested use/parametric/multi-level composition fixture."),
        pick("multi_level", "examples/gallery/two_story.barn", "Two-level stair/loft/alarm fixture."),
        pick("fragment_part", "examples/composed/parts/master_suite.barn", "Headerless part fixture; compile as fragment."),
        pick("parametric_part", "examples/composed/parts/flex_bath.barn", "Part with params for `use ... with` coverage."),
        pick("shop_loft_part", "examples/composed/parts/shop_loft.barn", "Multi-level shop/loft fragment for composed export and validation."),
    ]
    roles = [r for r in roles if r]
    return {
        "ok": all(f["ok"] for f in files),
        "paths": paths,
        "roles": roles,
        "files": sorted(files, key=lambda f: f["path"]),
        "summary": {
            "files": len(files),
            "whole_plans": sum(1 for f in files if not f["fragment"]),
            "fragments": sum(1 for f in files if f["fragment"]),
            "features": dict(sorted(Counter(feat for f in files for feat in f["features"]).items())),
        },
    }


def _fixture_purpose(rel: str) -> str:
    name = rel.replace("\\", "/")
    if name.endswith("examples/cedar_ridge.barn"):
        return "canonical whole-plan example"
    if name.endswith("examples/gallery/lshape.barn"):
        return "export/render parity fixture"
    if name.endswith("examples/gallery/two_story.barn"):
        return "multi-level stair/loft fixture"
    if "/composed/" in name and "/parts/" not in name:
        return "composition host fixture"
    if "/composed/parts/" in name:
        return "headerless composition part fixture"
    if "/gallery/" in name:
        return "gallery quality fixture"
    return "example plan fixture"


def fixture_catalog_markdown(catalog: dict[str, Any]) -> str:
    lines = [
        "# Fixture catalog",
        "",
        "Generated from `barndsl dev fixtures`. Prefer these fixtures before inventing new large plans in tests.",
        "",
        "## Roles",
        "",
        "| Role | Path/source | Purpose |",
        "| --- | --- | --- |",
    ]
    for role in catalog["roles"]:
        loc = role.get("path") or "inline source"
        lines.append(f"| `{role['role']}` | {loc} | {role['purpose']} |")
    lines.extend(["", "## Files", "", "| Path | Kind | Features | Codes | Score | Purpose |", "| --- | --- | --- | --- | --- | --- |"])
    for f in catalog["files"]:
        kind = "fragment" if f["fragment"] else "whole"
        feats = ", ".join(f["features"]) or "—"
        codes = ", ".join(f"{k}:{v}" for k, v in sorted(f["codes"].items())) or "—"
        score = "—" if f["score_total"] is None else str(f["score_total"])
        lines.append(f"| {_md_path(f['path'])} | {kind} | {feats} | {codes} | {score} | {f['purpose']} |")
    lines.append("")
    return "\n".join(lines)


def feature_check(name: str, *, statement: bool = True) -> dict[str, Any]:
    """Check whether a DSL feature/statement appears wired across key surfaces.

    This is intentionally heuristic: it catches the high-value drift points that
    make agent edits brittle (parser keyword, docs reference, playground/LSP
    derived sets, and at least one test/doc mention) without requiring every
    model-only feature to be a parser statement.
    """
    raw = name.strip()
    key = raw.lower()
    from .playground import _STATEMENT_KEYWORDS, _highlight_tokens
    from . import lsp

    parser = set(_KEYWORDS)
    playground = set(_STATEMENT_KEYWORDS)
    highlighted = set(_highlight_tokens().get("statements", ()))
    lsp_quickfix = set(getattr(lsp, "_QUICKFIX_STATEMENTS", ()))
    test_mentions = _grep_mentions([ROOT / "tests"], key)
    doc_mentions = _grep_mentions([ROOT / "README.md", ROOT / "docs"], key)

    checks = [
        {"name": "parser_keyword", "ok": (not statement) or key in parser, "details": key},
        {"name": "dsl_reference", "ok": (not statement) or bool(re.search(r"\b" + re.escape(key) + r"\b", DSL_REFERENCE)), "details": "src/barndsl/compiler.py:DSL_REFERENCE"},
        {"name": "playground_statement", "ok": (not statement) or key in playground, "details": "src/barndsl/playground.py"},
        {"name": "playground_highlight", "ok": (not statement) or key in highlighted, "details": "highlight token export"},
        {"name": "lsp_quickfix_statement", "ok": (not statement) or key in lsp_quickfix, "details": "src/barndsl/lsp.py"},
        {"name": "tests_mention", "ok": bool(test_mentions), "details": test_mentions[:12]},
        {"name": "docs_mention", "ok": bool(doc_mentions), "details": doc_mentions[:12]},
    ]
    missing = [c["name"] for c in checks if not c["ok"]]
    suggestions = []
    if "parser_keyword" in missing:
        suggestions.append(f"Add `{key}` to compiler._KEYWORDS and parser dispatch, or rerun with statement=False for a model-only feature.")
    if "dsl_reference" in missing:
        suggestions.append("Document syntax/examples in DSL_REFERENCE.")
    if any(x in missing for x in ("playground_statement", "playground_highlight", "lsp_quickfix_statement")):
        suggestions.append("Run barndsl dev audit; statement surfaces should derive from compiler keywords.")
    if "tests_mention" in missing:
        suggestions.append("Add focused parser/validation/export/LSP tests for the feature.")
    if "docs_mention" in missing:
        suggestions.append("Mention the feature in README.md or docs/*.md.")
    return {"ok": not missing, "name": raw, "statement": statement, "checks": checks, "missing": missing, "suggestions": suggestions}


# --- LSP smoke ---------------------------------------------------------------


def lsp_smoke(path: str | None = None, *, strict_composed: bool = False) -> dict[str, Any]:
    from . import lsp
    from .lsp import code_actions, completions, definition, diagnostics, formatting, hover, path_to_uri, symbols

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, details: Any = None, *, category: str = "basic") -> None:
        checks.append({"name": name, "ok": bool(ok), "category": category, "details": details})

    simple = 'plan "P"\nenvelope 24 x 24\nceiling 9\nroom living: living at 0,0 size 12 x 12\nentry living south width 3\n'
    res = lsp.compile_document(simple, None)
    record("simple_compile", res.plan is not None, res.summary())
    ds = diagnostics(res, "untitled:lsp-smoke")
    record("diagnostics", any(d.get("code") == "NAT_LIGHT" for d in ds), [d.get("code") for d in ds[:8]])
    items = completions("", 0, 0, res, None)
    labels = {i["label"] for i in items}
    record("completion_line_start", {"room", "door", "window", "envelope"} <= labels, sorted(labels)[:20])
    h = hover(simple, res, 3, simple.splitlines()[3].index("living"))
    record("hover_room", h is not None, h)
    record("formatting", bool(formatting('PLAN   "P"\n')), None)
    full_range = {"start": {"line": 0, "character": 0}, "end": {"line": len(simple.splitlines()), "character": 0}}
    actions = code_actions(simple, res, "untitled:lsp-smoke", full_range)
    record("code_actions", bool(actions), [a.get("title") for a in actions[:5]])
    record("symbols", bool(symbols(res)), symbols(res)[:1])

    composed_path = Path(path) if path else ROOT / "examples" / "composed" / "cedar_ridge.barn"
    if not composed_path.is_absolute():
        composed_path = ROOT / composed_path
    if composed_path.exists():
        text = composed_path.read_text(encoding="utf-8")
        uri = path_to_uri(str(composed_path))
        cres = lsp.compile_document(text, lsp.base_dir_for(uri))
        room_ids = [r.id for r in (cres.plan.rooms if cres.plan else [])]
        stamped = [rid for rid in room_ids if "." in rid]
        record("composed_compile", cres.plan is not None, {"summary": cres.summary(), "stamped_count": len(stamped)}, category="composed")
        record("composed_stamped_ids", bool(stamped), stamped[:10], category="composed")
        if "m.bed" in text:
            line = next(i for i, ln in enumerate(text.splitlines()) if "m.bed" in ln)
            col = text.splitlines()[line].index("m.bed")
            locs = definition(text, cres, line, col, uri, lsp.base_dir_for(uri))
            record("definition_stamped_id", len(locs) >= 2, locs, category="composed")
    else:
        record("composed_fixture_present", False, str(composed_path), category="composed")

    basic_ok = all(c["ok"] for c in checks if c["category"] == "basic")
    composed_ok = all(c["ok"] for c in checks if c["category"] == "composed")
    return {"ok": basic_ok and (composed_ok or not strict_composed), "basic_ok": basic_ok, "composed_ok": composed_ok, "strict_composed": strict_composed, "checks": checks}


# --- export parity -----------------------------------------------------------


def export_parity(path: str, prefix: str | None = None) -> dict[str, Any]:
    """Smoke-test the main geometry/export artifacts for one plan."""
    rel = Path(path)
    if not rel.is_absolute():
        rel = ROOT / rel
    art = ROOT / ".pi" / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    stem = prefix or rel.stem
    svg_out = art / f"{stem}.svg"
    glb_out = art / f"{stem}.glb"
    ifc_out = art / f"{stem}.ifc"
    packet_out = art / f"{stem}.html"
    jobs = [
        ("svg", svg_out, [sys.executable, "-m", "barndsl.cli", "build", str(rel), "--out", str(svg_out), "--format", "svg"]),
        ("gltf", glb_out, [sys.executable, "-m", "barndsl.cli", "gltf", str(rel), "--out", str(glb_out)]),
        ("ifc", ifc_out, [sys.executable, "-m", "barndsl.cli", "ifc", str(rel), "--out", str(ifc_out)]),
        ("packet", packet_out, [sys.executable, "-m", "barndsl.cli", "packet", str(rel), "--out", str(packet_out)]),
    ]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(SRC)}
    results: list[dict[str, Any]] = []
    for kind, out_path, args in jobs:
        proc = subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        results.append({
            "kind": kind,
            "exit": proc.returncode,
            "out": _rel(out_path),
            "exists": out_path.exists(),
            "bytes": out_path.stat().st_size if out_path.exists() else None,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        })
    return {"ok": all(r["exit"] == 0 and r["exists"] for r in results), "path": _rel(rel), "results": results}


# --- doctor -----------------------------------------------------------------


def doctor(
    *,
    paths: list[str] | None = None,
    lsp_strict: bool = True,
    run_impact: bool = False,
    export_plan: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Run the default maintainability confidence stack.

    The stack is intentionally JSON-shaped and deterministic so humans, CI and
    agent harnesses can share one command. Export parity is opt-in because it is
    slower and creates artifacts; use ``export_plan`` when geometry/export code
    changed.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, details: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "details": details})

    audit = repo_audit()
    add("audit", audit["ok"], {"problems": audit["problems"], "statement_keywords": audit["statement_keywords"], "missing_registry": audit["diagnostics"]["missing_registry"]})

    gallery = diagnostic_diff(paths or ["examples"], profile=profile)
    gallery_files = gallery["files"]["after"]
    gallery_bad = {p: info["counts"] for p, info in gallery_files.items() if not info.get("ok")}
    add("gallery_gate", not gallery_bad, {"files": gallery["after_files"], "whole_plans": gallery["after_whole_plans"], "fragments": gallery["after_fragments"], "score": gallery["score"], "bad_files": gallery_bad})

    lsp = lsp_smoke(strict_composed=lsp_strict)
    add("lsp_smoke", lsp["ok"], {"basic_ok": lsp["basic_ok"], "composed_ok": lsp["composed_ok"], "strict_composed": lsp["strict_composed"]})

    impact = impact_tests(run=run_impact)
    impact_details = {k: impact.get(k) for k in ("changed", "targets", "exit") if k in impact}
    if run_impact:
        impact_details["stdout_tail"] = (impact.get("stdout") or "")[-4000:]
        impact_details["stderr_tail"] = (impact.get("stderr") or "")[-4000:]
    add("impact_tests" if run_impact else "impact_targets", int(impact.get("exit", 0) or 0) == 0, impact_details)

    if export_plan:
        export = export_parity(export_plan, prefix="doctor_" + Path(export_plan).stem)
        add("export_parity", export["ok"], {"path": export["path"], "results": [{"kind": r["kind"], "exit": r["exit"], "bytes": r["bytes"]} for r in export["results"]]})

    ok = all(c["ok"] for c in checks)
    next_steps: list[str] = []
    if not audit["ok"]:
        next_steps.append("Fix repo-audit problems first; they usually indicate wiring drift.")
    if checks[1]["ok"] is False:
        next_steps.append("Compile the listed gallery bad_files directly with `barndsl compile --json`.")
    if not lsp["ok"]:
        next_steps.append("Run `barndsl dev lsp-smoke --strict-composed` and inspect the failed checks.")
    if run_impact and int(impact.get("exit", 0) or 0) != 0:
        next_steps.append("Fix failing impact pytest targets before broader gates.")
    if export_plan and not checks[-1]["ok"]:
        next_steps.append("Inspect .pi/artifacts export outputs and exporter stderr tails.")
    if not next_steps:
        next_steps.append("No immediate harness issues found. For geometry/export changes, rerun with --export-plan.")
    return {"ok": ok, "checks": checks, "next_steps": next_steps}


# --- impact tests ------------------------------------------------------------


def impact_targets(changed: list[str]) -> list[str]:
    files = [p.replace("\\", "/") for p in changed]
    targets: set[str] = set()
    if not files:
        return ["tests/test_compiler.py"]
    mapping = [
        (("src/barndsl/validation.py", "src/barndsl/diagnostics.py", "src/barndsl/profiles.py"), ["tests/test_design_quality.py", "tests/test_compiler.py", "tests/test_profiles.py", "tests/test_metamorphic.py"]),
        (("src/barndsl/compiler.py",), ["tests/test_compiler.py", "tests/test_recovery.py", "tests/test_compose.py", "tests/test_compose_v2.py", "tests/test_metamorphic.py"]),
        (("src/barndsl/lsp.py",), ["tests/test_lsp.py"]),
        (("src/barndsl/playground.py",), ["tests/test_playground.py", "tests/test_playground_agent.py"]),
        (("src/barndsl/edits.py",), ["tests/test_edits.py", "tests/test_playground.py"]),
        (("src/barndsl/render.py", "src/barndsl/views.py"), ["tests/test_render_swing.py", "tests/test_phase13.py", "tests/test_playground.py"]),
        (("src/barndsl/gltf.py",), ["tests/test_gltf.py"]),
        (("src/barndsl/ifc.py",), ["tests/test_ifc.py"]),
        (("src/barndsl/revit.py",), ["tests/test_revit.py", "tests/test_revit_roundtrip.py"]),
        (("src/barndsl/fixtures.py",), ["tests/test_fixture_rules.py", "tests/test_fixtures.py"]),
        (("src/barndsl/score.py",), ["tests/test_agent_loop.py", "tests/test_compare.py"]),
        (("src/barndsl/cli.py",), ["tests/test_cli_features.py", "tests/test_devtools.py"]),
        (("src/barndsl/devtools.py",), ["tests/test_devtools.py", "tests/test_cli_features.py"]),
    ]
    for f in files:
        if f.startswith("tests/") and f.endswith(".py"):
            targets.add(f)
        for prefixes, ts in mapping:
            if any(f.startswith(pref) for pref in prefixes):
                targets.update(ts)
        if f.endswith(".barn"):
            targets.update(["tests/test_gallery.py", "tests/test_compiler.py"])
    if any(f.startswith(".pi/") or f.startswith("tools/pi_barndsl") for f in files):
        targets.update(["tests/test_devtools.py", "tests/test_compiler.py"])
    return sorted(targets or {"tests/test_compiler.py"})


def changed_files_from_git() -> list[str]:
    try:
        proc = subprocess.run(["git", "status", "--short"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append(path)
    return out


def impact_tests(changed: list[str] | None = None, *, run: bool = False, quiet: bool = True) -> dict[str, Any]:
    changed = changed if changed is not None and changed else changed_files_from_git()
    targets = impact_targets(changed)
    out: dict[str, Any] = {"changed": changed, "targets": targets}
    if run:
        args = [sys.executable, "-m", "pytest", *( ["-q"] if quiet else [] ), *targets]
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(SRC)}
        proc = subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        out.update({"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
    return out


# --- scaffold ---------------------------------------------------------------


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def rule_scaffold(code: str, severity: str = "info", test_file: str | None = None, *, write: bool = False) -> dict[str, Any]:
    code = code.upper()
    slug = _snake(code)
    tfile = Path(test_file or f"tests/test_{slug}.py")
    skeleton = f'''"""Tests for {code}."""

from barndsl.compiler import compile_source


def _codes(src: str, severity: str = "{severity}") -> set[str]:
    r = compile_source(src)
    return {{d.code for d in getattr(r, severity + "s")}}


def test_{slug}_fires():
    src = """
plan "Probe"
envelope 24 x 24
ceiling 9
room living: living at 0,0 size 12 x 12
entry living south width 3
"""
    assert "{code}" in _codes(src)


def test_{slug}_does_not_fire_when_satisfied():
    src = """
plan "Probe"
envelope 24 x 24
ceiling 9
room living: living at 0,0 size 12 x 12
entry living south width 3
"""
    assert "{code}" not in _codes(src)
'''
    if write:
        path = ROOT / tfile
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(skeleton, encoding="utf-8")
    return {
        "kind": "rule",
        "code": code,
        "testFile": str(tfile),
        "testSkeleton": skeleton,
        "checklist": [
            f"Add REGISTRY entry for {code} in src/barndsl/diagnostics.py ({severity}).",
            "Add/extend a _validate_* function in src/barndsl/validation.py or the relevant domain module.",
            "Call the validator from validate() in deterministic order if it is a new function.",
            f"Fill in {tfile} with one firing and one non-firing plan.",
            "Run barndsl dev rule-probe, barndsl dev audit, targeted tests, gallery gate, and export parity if geometry changed.",
        ],
    }


def feature_scaffold(name: str, out: str | None = None, *, write: bool = True) -> dict[str, Any]:
    name = name.lower()
    checklist = [
        f"Add `{name}` to compiler._KEYWORDS and parser dispatch in src/barndsl/compiler.py.",
        "Update DSL_REFERENCE in src/barndsl/compiler.py with syntax and examples.",
        "Add/extend dataclasses in src/barndsl/elements.py if model state is needed.",
        "Update emit.py round-trip output for the new model field/statement.",
        "Update fmt.py only if tokenization/normalization needs special handling.",
        "Playground/LSP statement sets are derived; add context-specific completions/hovers if useful.",
        "Update edits.py if the feature should be direct-manipulable.",
        "Update render/views/gltf/ifc/revit/cost/schedule exports if geometry or takeoff changes.",
        "Add parser, validation, emit round-trip, LSP/playground, and export parity tests.",
        "Run barndsl dev audit, barndsl dev impact --run, gallery gate, and export parity.",
    ]
    md = "# Feature scaffold: `{}`\n\n{}\n".format(name, "\n".join(f"- [ ] {x}" for x in checklist))
    out_path = Path(out or f".pi/artifacts/feature-{_snake(name)}.md")
    if write:
        path = ROOT / out_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
    return {"kind": "feature", "name": name, "path": str(out_path), "markdown": md, "checklist": checklist}


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
