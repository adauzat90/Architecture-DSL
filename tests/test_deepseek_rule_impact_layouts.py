from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools import deepseek_rule_impact_layouts as layouts


def _fake_plan(room_types: list[str]):
    rooms = [
        SimpleNamespace(type=SimpleNamespace(value=room_type), area=100.0, level=0)
        for room_type in room_types
    ]
    return SimpleNamespace(rooms=rooms)


def _fake_step(iteration: int, source: str, room_types: list[str], score: float):
    result = SimpleNamespace(plan=_fake_plan(room_types), ok=True)
    return SimpleNamespace(
        iteration=iteration,
        source=source,
        result=result,
        score=SimpleNamespace(total=score),
        effective_total=score,
        critique=None,
    )


COMPACT_TYPES = [
    "foyer", "great_room", "flex", "safe_room", "mechanical", "storage",
    "kitchen", "dining", "laundry", "mudroom",
    "bedroom", "bedroom", "bathroom", "bathroom", "closet", "closet",
]


def _summary_plan(slug: str, score: float, severity: str) -> dict:
    diagnostic = {
        "severity": severity,
        "code": "ROOM_RULE",
        "room": "great",
        "line": 8,
        "message": "room rule changed",
        "hint": "review it",
    }
    return {
        "slug": slug,
        "ok": severity != "error",
        "score": score,
        "summary": {
            "errors": int(severity == "error"),
            "warnings": int(severity == "warning"),
            "infos": 0,
        },
        "codes": {"ROOM_RULE": 1},
        "room_types": {"great_room": 1},
        "coverage": {"ok": True, "required": ["great_room"], "missing": []},
        "acceptance": {"ok": True, "reasons": []},
        "diagnostics": [diagnostic],
    }


def _fake_compiled_artifact(path, brief, meta=None, **_kwargs):
    return {
        "slug": path.stem,
        "brief": brief,
        "barn": str(path),
        "svg": None,
        "ok": True,
        "score": 100.0,
        "summary": {"errors": 0, "warnings": 0, "infos": 0},
        "codes": {},
        "room_types": {},
        "coverage": {"ok": True, "required": [], "missing": [], "actual": []},
        "acceptance": {"ok": True, "reasons": []},
        "diagnostics": [],
        "agent": meta or {},
    }


def test_coverage_gate_selects_agent_candidate_over_generic_seed():
    seed = _fake_step(0, "generic solver seed", ["living", "bedroom"], 95.0)
    agent = _fake_step(1, "semantic agent plan", COMPACT_TYPES, 82.0)
    result = SimpleNamespace(history=[seed, agent], result=seed.result)

    selected, coverage, acceptance = layouts._select_covered_step("compact_safe_flex", result)

    assert selected is agent
    assert coverage["ok"] is True
    assert coverage["missing"] == []
    assert acceptance["ok"] is True


def test_coverage_gate_rejects_corpus_without_required_types():
    generic = _fake_step(1, "generic", ["living", "bedroom"], 90.0)
    result = SimpleNamespace(history=[generic], result=generic.result)

    with pytest.raises(RuntimeError, match="missing foyer"):
        layouts._select_covered_step("compact_safe_flex", result)


def test_acceptance_rejects_token_complete_but_low_quality_candidate():
    low = _fake_step(1, "token complete but weak", COMPACT_TYPES, 44.0)
    result = SimpleNamespace(history=[low], result=low.result)

    with pytest.raises(RuntimeError, match="below minimum 60"):
        layouts._select_covered_step("compact_safe_flex", result)


def test_acceptance_requires_requested_program_counts():
    one_bed = COMPACT_TYPES.copy()
    one_bed.remove("bedroom")
    candidate = _fake_step(1, "one bedroom short", one_bed, 90.0)
    result = SimpleNamespace(history=[candidate], result=candidate.result)

    with pytest.raises(RuntimeError, match="bedroom x2"):
        layouts._select_covered_step("compact_safe_flex", result)


def test_acceptance_requires_real_critic_approval_when_enabled():
    candidate = _fake_step(1, "unreviewed", COMPACT_TYPES, 90.0)
    candidate.critique = SimpleNamespace(
        satisfied=True, skipped=True, blocking_issues=[]
    )
    result = SimpleNamespace(history=[candidate], result=candidate.result)

    with pytest.raises(RuntimeError, match="critique was skipped"):
        layouts._select_covered_step(
            "compact_safe_flex", result, require_critique=True
        )


def test_acceptance_feedback_folds_harness_gates_as_issues():
    result = SimpleNamespace(
        plan=_fake_plan(["foyer", "great_room", "bedroom"]),
        ok=True,
    )
    score = SimpleNamespace(total=42.0)

    issues = layouts._acceptance_feedback_issues(
        "compact_safe_flex",
        result,
        score,
        critique=SimpleNamespace(satisfied=False, skipped=False, blocking_issues=[]),
        min_score=60,
        require_critique=True,
    )

    assert {issue.code for issue in issues} == {"BRIEF_ACCEPTANCE"}
    text = "\n".join(issue.message for issue in issues)
    assert "Missing required semantic room type" in text
    assert "score 42 is below minimum 60" in text
    assert "architectural critique did not approve" in text


def test_rejected_steps_are_preserved_for_debugging(tmp_path):
    rejected = tmp_path / "rejected"
    step = _fake_step(1, "plan x", ["foyer"], 12.0)
    step.result.diagnostics = [SimpleNamespace(code="ROOM_RULE")]
    result = SimpleNamespace(history=[step])

    layouts._write_rejected_steps(
        "compact_safe_flex",
        result,
        rejected,
        min_score=60,
        require_critique=False,
    )

    assert (rejected / "round1.barn").read_text(encoding="utf-8") == "plan x\n"
    steps = json.loads((rejected / "steps.json").read_text(encoding="utf-8"))
    assert steps[0]["diagnostic_codes"] == ["ROOM_RULE"]
    assert "flex" in steps[0]["coverage"]["missing"]


def test_dotenv_loader_uses_authoritative_cwd_file(monkeypatch):
    calls: dict[str, object] = {}

    def find_dotenv(*, usecwd):
        calls["usecwd"] = usecwd
        return "project.env"

    def load_dotenv(path, *, override):
        calls["path"] = path
        calls["override"] = override

    monkeypatch.setattr(layouts, "find_dotenv", find_dotenv)
    monkeypatch.setattr(layouts, "load_dotenv", load_dotenv)

    layouts._load_env()

    assert calls == {"usecwd": True, "path": "project.env", "override": True}


def test_compare_detects_per_plan_changes_when_global_code_counts_match(tmp_path):
    before = {
        "plans": [_summary_plan("variant", 53.0, "warning")],
        "totals": {"codes": {"ROOM_RULE": 1}},
    }
    after = {
        "plans": [_summary_plan("variant", 53.5, "error")],
        "totals": {"codes": {"ROOM_RULE": 1}},
    }
    before_path = tmp_path / "before.json"
    before_path.write_text(json.dumps(before), encoding="utf-8")

    comparison = layouts._compare(before_path, after)

    assert comparison["introduced"] == {}
    assert comparison["resolved"] == {}
    assert comparison["has_changes"] is True
    change = comparison["plans"]["variant"]
    assert change["score"] == {"before": 53.0, "after": 53.5}
    assert change["ok"] == {"before": True, "after": False}
    assert change["diagnostics"]["introduced"][0]["severity"] == "error"
    assert change["diagnostics"]["resolved"][0]["severity"] == "warning"


def test_reuse_honors_only_and_uses_canonical_order(tmp_path, monkeypatch):
    for slug in layouts.BRIEFS:
        (tmp_path / f"{slug}.barn").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "stray.barn").write_text("not part of the corpus\n", encoding="utf-8")

    monkeypatch.setattr(layouts, "_load_env", lambda: None)
    monkeypatch.setattr(layouts, "_compile_artifacts", _fake_compiled_artifact)
    monkeypatch.setattr(layouts, "_write_review_html", lambda files, out: None)

    rc = layouts.main(
        [
            "--out",
            str(tmp_path),
            "--reuse-existing",
            "--only",
            "loft_rec_variant",
            "--summary-name",
            "only.json",
        ]
    )
    assert rc == 0
    only = json.loads((tmp_path / "only.json").read_text(encoding="utf-8"))
    assert [plan["slug"] for plan in only["plans"]] == ["loft_rec_variant"]

    rc = layouts.main(
        ["--out", str(tmp_path), "--reuse-existing", "--summary-name", "all.json"]
    )
    assert rc == 0
    all_plans = json.loads((tmp_path / "all.json").read_text(encoding="utf-8"))
    assert [plan["slug"] for plan in all_plans["plans"]] == [
        "compact_safe_flex",
        "family_rec_room",
        "loft_rec_variant",
        "shop_house",
    ]


def test_env_model_is_loaded_before_defaults_and_solver_seed_is_opt_in(tmp_path, monkeypatch):
    calls: list[dict] = []

    def load_env():
        monkeypatch.setenv("BARNDSL_MODEL", "dotenv-model")

    def run_agent(slug, brief, **kwargs):
        calls.append({"slug": slug, **kwargs})
        return f"plan {slug}\n", {"best_iteration": 1}

    monkeypatch.delenv("BARNDSL_MODEL", raising=False)
    monkeypatch.setattr(layouts, "_load_env", load_env)
    monkeypatch.setattr(layouts, "_agent_available", lambda: (True, None))
    monkeypatch.setattr(layouts, "_run_agent", run_agent)
    monkeypatch.setattr(layouts, "_compile_artifacts", _fake_compiled_artifact)
    monkeypatch.setattr(layouts, "_write_review_html", lambda files, out: None)

    rc = layouts.main(["--out", str(tmp_path), "--summary-name", "generated.json"])

    assert rc == 0
    assert {call["model"] for call in calls} == {"dotenv-model"}
    assert {call["seed_solver"] for call in calls} == {False}
    assert {call["min_score"] for call in calls} == {layouts.DEFAULT_MIN_SCORE}
    generated = json.loads((tmp_path / "generated.json").read_text(encoding="utf-8"))
    assert [plan["slug"] for plan in generated["plans"]] == sorted(layouts.BRIEFS)
    assert generated["schema_version"] == 2
    assert generated["run"]["mode"] == "generate"
    assert generated["provenance"]["prompt_sha256"]


def test_atomic_publish_replaces_the_complete_directory(tmp_path):
    destination = tmp_path / "corpus"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")

    layouts._publish_staged_directory(staging, destination)

    assert not staging.exists()
    assert not (destination / "old.txt").exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".corpus.backup-*"))


def test_failed_generation_does_not_modify_existing_corpus(tmp_path, monkeypatch):
    destination = tmp_path / "corpus"
    destination.mkdir()
    marker = destination / "baseline.txt"
    marker.write_text("stable baseline", encoding="utf-8")
    calls = {"n": 0}

    def run_agent(slug, brief, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider failed mid-run")
        return f"plan {slug}\n", {"best_iteration": 1}

    monkeypatch.setattr(layouts, "_load_env", lambda: None)
    monkeypatch.setattr(layouts, "_agent_available", lambda: (True, None))
    monkeypatch.setattr(layouts, "_run_agent", run_agent)

    with pytest.raises(RuntimeError, match="provider failed mid-run"):
        layouts.main(
            [
                "--out", str(destination),
                "--only", "compact_safe_flex",
                "--only", "family_rec_room",
            ]
        )

    assert marker.read_text(encoding="utf-8") == "stable baseline"
    assert not (destination / "compact_safe_flex.barn").exists()


def test_summary_and_review_names_cannot_escape_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(layouts, "_load_env", lambda: None)

    with pytest.raises(SystemExit):
        layouts.main(
            [
                "--out", str(tmp_path / "corpus"),
                "--reuse-existing",
                "--summary-name", "../escaped.json",
            ]
        )
