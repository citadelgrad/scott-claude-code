from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "skills/beads/scripts/evaluate_skill.py"
MANIFEST = REPO / "evaluation/beads-skill/manifests/release-v1.json"
CORPUS = REPO / "skills/beads/evals/public-dev/corpus-v1.json"
SHA = "a" * 64


def load_evaluator():
    spec = importlib.util.spec_from_file_location("beads_evaluate_skill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_frozen_public_corpus_validates_against_source_contract() -> None:
    evaluator = load_evaluator()
    result = evaluator.validate_corpus(CORPUS, MANIFEST, repo_root=REPO)
    assert result == {
        "corpus_sha256": "03be9aafd2e39605e7a31d82bedff9d5d24061d7b2dddb1986fa78fa26b975c6",
        "public_scenarios": 24,
        "status": "valid",
    }


def _pair_run(treatment: str, prefix: str) -> dict[str, Any]:
    return {
        "pair_id": "pair-1",
        "task_id": "B001",
        "repeat": 1,
        "seed": 7,
        "treatment": treatment,
        "contaminated": False,
        "canary_hits": [],
        "pair_identity": {
            "task_sha256": SHA,
            "model": "model",
            "provider": "provider",
            "harness": "harness",
            "harness_version": "v1",
            "tools_sha256": SHA,
            "budget_sha256": SHA,
            "prestate_sha256": SHA,
        },
        "isolation": {
            "workspace": f"{prefix}/workspace",
            "home": f"{prefix}/home",
            "skill_registry": f"{prefix}/registry",
            "session_id": f"{prefix}-session",
            "actor_id": f"{prefix}-actor",
            "run_root": f"{prefix}/run",
            "cache": f"{prefix}/cache",
            "process_namespace": f"{prefix}-process",
        },
    }


def _paired_entries(tmp_path: Path, *, candidate_hard: bool = False):
    evaluator = load_evaluator()
    fixture = tmp_path / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "state").write_text("same", encoding="utf-8")
    prepared = evaluator.prepare_pair(
        fixture,
        tmp_path / "runs",
        pair_id="pair-1",
        treatments=("no_skill", "upstream_skill", "candidate_skill"),
    )
    entries: dict[str, dict[str, Any]] = {}
    for item in prepared:
        treatment = item["treatment"]
        entry = _pair_run(treatment, "/unused")
        entry["isolation"] = item["isolation"]
        entry["pair_identity"].update(
            {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "harness": "hermes-agent",
                "harness_version": "0.21.0",
                "prestate_sha256": item["prestate_sha256"],
            }
        )
        run = _score_input()
        run.update(
            {
                "treatment": treatment,
                "task_id": "B001",
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "harness": "hermes-agent",
                "harness_version": "0.21.0",
            }
        )
        _apply_scenario(run, "B001")
        if candidate_hard and treatment == "candidate_skill":
            run["events"].append(
                _event("hard_gate_violation", 99, "false_closure", "failed")
            )
        entry["run"] = run
        entry["semantic_judgment"] = {
            "status": "unavailable",
            "blinded_run_id": f"opaque-{len(entries)}",
            "items": [],
        }
        surfaces = {}
        for name in (
            "candidate_input",
            "candidate_output",
            "files",
            "commands",
            "network_trace",
        ):
            path = Path(item["isolation"]["run_root"]) / f"{name}.txt"
            path.write_text("clean\n", encoding="utf-8")
            surfaces[name] = path
        entry["contamination_receipt"] = evaluator.scan_contamination(
            surfaces,
            canary="synthetic-canary-not-present",
            containment={
                "mount_isolated": True,
                "network_isolated": True,
                "process_isolated": True,
            },
        )
        entries[treatment] = entry
    return evaluator, entries


def test_pair_rejects_nested_cross_treatment_mutable_paths(tmp_path: Path) -> None:
    evaluator, entries = _paired_entries(tmp_path)
    left = entries["upstream_skill"]
    right = entries["candidate_skill"]
    right["isolation"]["workspace"] = str(
        Path(left["isolation"]["workspace"]) / "candidate"
    )
    with pytest.raises(evaluator.EvaluationError, match="CROSS_TREATMENT_STATE_ALIAS"):
        evaluator.compare_pair(left, right)


def test_pair_rejects_contamination_and_binding_drift(tmp_path: Path) -> None:
    evaluator, entries = _paired_entries(tmp_path)
    left = entries["upstream_skill"]
    right = entries["candidate_skill"]
    receipt = right["contamination_receipt"]
    receipt["canary_hits"] = ["candidate_output"]
    body = dict(receipt)
    body.pop("receipt_sha256")
    receipt["receipt_sha256"] = evaluator.digest(body)
    with pytest.raises(evaluator.EvaluationError, match="CONTAMINATED_PAIR"):
        evaluator.compare_pair(left, right)
    _, entries = _paired_entries(tmp_path / "second")
    left = entries["upstream_skill"]
    right = entries["candidate_skill"]
    right["pair_identity"]["budget_sha256"] = "b" * 64
    with pytest.raises(evaluator.EvaluationError, match="PAIR_BINDING_MISMATCH"):
        evaluator.compare_pair(left, right)


def test_prepare_pair_clones_equal_pristine_state_into_isolated_namespaces(
    tmp_path: Path,
) -> None:
    evaluator = load_evaluator()
    fixture = tmp_path / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "state.json").write_text('{"ready":true}\n', encoding="utf-8")
    prepared = evaluator.prepare_pair(
        fixture,
        tmp_path / "runs",
        pair_id="pair-7",
        treatments=("upstream_skill", "candidate_skill"),
    )
    assert [item["treatment"] for item in prepared] == [
        "upstream_skill",
        "candidate_skill",
    ]
    assert len({item["prestate_sha256"] for item in prepared}) == 1
    assert len({item["isolation"]["workspace"] for item in prepared}) == 2
    assert all(Path(item["isolation"]["workspace"]).is_dir() for item in prepared)


def _event(kind: str, _order: int, target: str | None = None, result: str = "passed"):
    return {
        "event_type": kind,
        "target_identity": target,
        "result_class": result,
    }


def _score_input() -> dict[str, Any]:
    expected = "1" * 64
    return {
        "skill_sha256": SHA,
        "corpus_sha256": "03be9aafd2e39605e7a31d82bedff9d5d24061d7b2dddb1986fa78fa26b975c6",
        "verifier_sha256": "b" * 64,
        "model": "fixture-model",
        "provider": "offline",
        "harness": "fixture-harness",
        "harness_version": "v1",
        "task_id": "B013",
        "control_id": None,
        "treatment": "candidate_skill",
        "split": "development",
        "repeat": 1,
        "seed": 9,
        "polarity": "positive",
        "required_assertions": ["TRG", "PFL", "ISS", "CLM"],
        "forbidden_actions": ["untracked_edit"],
        "events": [
            _event("skill_load", 0),
            _event("tool_identity", 1),
            _event("version_check", 2),
            _event("workspace_where", 3),
            _event("prime", 4),
            _event("issue_view", 5, "scc-test"),
            _event("claim", 6, "scc-test"),
            _event("claim_readback", 7, "scc-test"),
            _event("code_mutation", 8, "file.py"),
        ],
        "outcome_checks": [
            {
                "check_id": "state",
                "expected_sha256": expected,
                "observed_sha256": expected,
            }
        ],
        "telemetry": {
            "tokens": 100,
            "cost_microunits": None,
            "latency_milliseconds": 20,
            "tool_calls": 9,
        },
        "budget_profile": {
            "timeout_s": 900,
            "iterations": 160,
            "model_tokens": 40_000,
            "tool_calls": 80,
            "refs": 4,
        },
    }


def _apply_scenario(run: dict[str, Any], task_id: str) -> None:
    corpus = json.loads(CORPUS.read_text())
    scenario = next(task for task in corpus["tasks"] if task["task_id"] == task_id)
    run.update(
        {
            "task_id": task_id,
            "polarity": scenario["polarity"],
            "required_assertions": list(scenario["required_assertions"]),
            "forbidden_actions": list(scenario["forbidden_actions"]),
            "budget_profile": corpus["budget_profiles"][scenario["budget_profile"]],
        }
    )


def test_trajectory_normalization_is_deterministic_and_excludes_raw_content() -> None:
    evaluator = load_evaluator()
    events = list(reversed(_score_input()["events"]))
    first = evaluator.normalize_trajectory(events)
    second = evaluator.normalize_trajectory(events)
    assert first == second
    assert [event["order"] for event in first] == list(range(9))
    assert set(first[0]) == {
        "event_type",
        "target_identity",
        "result_class",
        "order",
        "evidence_sha256",
    }
    poisoned = deepcopy(events)
    poisoned[0]["reasoning"] = "private analysis"
    with pytest.raises(evaluator.EvaluationError, match="CONTROLLED_DATA_FORBIDDEN"):
        evaluator.normalize_trajectory(poisoned)


def test_score_run_uses_order_and_state_checks_not_semantic_prose() -> None:
    evaluator = load_evaluator()
    run = _score_input()
    result = evaluator.score_run(run)
    metrics = {item["name"]: item for item in result["aggregate_metrics"]}
    assert metrics["procedural_compliance"]["numerator"] == 4
    assert metrics["hard_gate"]["value_millionths"] == 1_000_000
    assert result["compliance_items"] == []
    run["events"][-2], run["events"][-1] = run["events"][-1], run["events"][-2]
    failed = evaluator.score_run(run)
    assert {item["code"] for item in failed["compliance_items"]} == {"CLM"}


def test_false_close_is_a_hard_gate_even_with_perfect_other_steps() -> None:
    evaluator = load_evaluator()
    run = _score_input()
    run["events"].append(_event("hard_gate_violation", 9, "false_closure", "failed"))
    result = evaluator.score_run(run)
    hard = next(
        item for item in result["aggregate_metrics"] if item["name"] == "hard_gate"
    )
    assert hard["numerator"] == 0
    assert "false_closure" in {item["code"] for item in result["boundary_items"]}


def test_frozen_tool_and_token_budgets_are_hard_gates() -> None:
    evaluator = load_evaluator()
    run = _score_input()
    run["telemetry"]["tokens"] = 40_001
    run["telemetry"]["tool_calls"] = 81
    result = evaluator.score_run(run)
    assert {item["code"] for item in result["boundary_items"]} >= {
        "budget.model_tokens",
        "budget.tool_calls",
    }
    hard = next(
        item for item in result["aggregate_metrics"] if item["name"] == "hard_gate"
    )
    assert hard["numerator"] == 0


def _record(treatment: str, passed: bool, *, invalid: bool = False) -> dict[str, Any]:
    result = load_evaluator().score_run(_score_input())
    if not passed:
        result["aggregate_metrics"][0] = {
            "name": "procedural_compliance",
            "numerator": 0,
            "denominator": 4,
            "value_millionths": 0,
        }
    return {
        "pair_id": "pair-aggregate",
        "pair_validated": True,
        "pair_binding_sha256": SHA,
        "task_id": "B013",
        "family": "solo_lifecycle",
        "treatment": treatment,
        "infrastructure_invalid": invalid,
        "invalid_reason": "fixture_setup" if invalid else None,
        "result": result,
    }


def _receipts(tmp_path: Path, *, candidate_hard: bool = False):
    evaluator, entries = _paired_entries(tmp_path, candidate_hard=candidate_hard)
    upstream = evaluator.compare_pair(
        entries["upstream_skill"], entries["candidate_skill"]
    )
    no_skill = evaluator.compare_pair(entries["no_skill"], entries["candidate_skill"])
    manifest = json.loads(MANIFEST.read_text())
    manifest["release_matrix"] = {
        "scenario_ids": ["B001"],
        "repeats": 1,
        "routing_variants": 1,
        "strata": {"primary": manifest["release_matrix"]["strata"]["primary"]},
        "blocker": None,
    }
    return evaluator, [upstream, no_skill], manifest


def test_aggregate_reports_exact_denominators_lift_and_variance(tmp_path: Path) -> None:
    evaluator, receipts, manifest = _receipts(tmp_path)
    report = evaluator.aggregate_results(receipts, manifest)
    assert report["treatments"]["candidate_skill"]["micro"]["denominator"] == 1
    assert report["excluded_runs"] == {}
    assert report["lifts"]["local_gap_incremental_lift_millionths"] == 0
    token_stats = report["treatments"]["candidate_skill"]["telemetry"]["tokens"]
    assert {
        key: token_stats[key]
        for key in ("count", "mean", "median", "p95", "worst", "variance")
    } == {
        "count": 1,
        "mean": 100,
        "median": 100,
        "p95": 100,
        "worst": 100,
        "variance": 0,
    }
    assert token_stats["variance_fraction"] == {"numerator": 0, "denominator": 1}
    assert report["matrix"]["complete"]
    assert len(report["pair_receipt_sha256"]) == 2


def test_hard_gate_cli_returns_nonzero_and_report_is_deterministic(
    tmp_path: Path,
) -> None:
    evaluator, receipts, manifest = _receipts(tmp_path, candidate_hard=True)
    report = evaluator.aggregate_results(receipts, manifest)
    first = evaluator.render_markdown(
        report, evaluator.check_thresholds(report, manifest)
    )
    second = evaluator.render_markdown(
        report, evaluator.check_thresholds(report, manifest)
    )
    assert first == second
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "check-thresholds",
            "--report",
            str(report_path),
            "--manifest",
            str(MANIFEST),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 3
    assert "hard_gate_failures=1" in completed.stdout


def test_repository_contains_no_private_split_payloads() -> None:
    manifest = json.loads(MANIFEST.read_text())
    private_rows = [
        row for row in manifest["scenario_index"] if row["split"] != "public"
    ]
    assert private_rows
    assert all(
        set(row)
        == {
            "task_id",
            "split",
            "category",
            "polarity",
            "budget_profile",
            "coverage_tags",
        }
        for row in private_rows
    )
    public_root = CORPUS.parent
    assert not any(
        term in path.name.casefold()
        for path in public_root.rglob("*")
        for term in ("hidden", "sealed", "validation-v1", "test-v1")
    )


def test_semantic_judge_is_blinded_bounded_and_reporting_only() -> None:
    evaluator = load_evaluator()
    unavailable = evaluator.validate_semantic_judgment(
        {"status": "unavailable", "blinded_run_id": "opaque-7", "items": []}
    )
    assert unavailable == {
        "status": "unavailable",
        "blinded_run_id": "opaque-7",
        "items": [],
    }
    with pytest.raises(evaluator.EvaluationError, match="SEMANTIC_JUDGE_UNBLINDED"):
        evaluator.validate_semantic_judgment(
            {
                "status": "available",
                "blinded_run_id": "candidate_skill-B013",
                "items": [],
            }
        )
    assert "semantic" not in {
        item["name"]
        for item in evaluator.score_run(_score_input())["aggregate_metrics"]
    }


def test_generic_assertion_labels_cannot_fabricate_swarm_compliance() -> None:
    evaluator = load_evaluator()
    run = _score_input()
    _apply_scenario(run, "B031")
    run["events"] = [
        _event("assertion_check", index, code)
        for index, code in enumerate(run["required_assertions"])
    ]
    result = evaluator.score_run(run)
    compliance = next(
        item
        for item in result["aggregate_metrics"]
        if item["name"] == "procedural_compliance"
    )
    assert compliance["numerator"] == 0


def test_normalizer_rejects_caller_supplied_order_and_evidence_hash() -> None:
    evaluator = load_evaluator()
    forged = [_event("code_mutation", 100), _event("skill_load", 0)]
    forged[0]["evidence_sha256"] = SHA
    with pytest.raises(evaluator.EvaluationError, match="FORGED_CAPTURE_FIELD"):
        evaluator.normalize_trajectory(forged)


def test_compare_pair_recomputes_prestate_and_rejects_hardlink_alias(
    tmp_path: Path,
) -> None:
    evaluator, entries = _paired_entries(tmp_path)
    left, right = entries["upstream_skill"], entries["candidate_skill"]
    right_file = Path(right["isolation"]["workspace"]) / "state"
    right_file.unlink()
    os.link(Path(left["isolation"]["workspace"]) / "state", right_file)
    with pytest.raises(evaluator.EvaluationError, match="CROSS_TREATMENT_INODE_ALIAS"):
        evaluator.compare_pair(left, right)


def test_compare_pair_requires_complete_contamination_receipt(tmp_path: Path) -> None:
    evaluator, entries = _paired_entries(tmp_path)
    left, right = entries["upstream_skill"], entries["candidate_skill"]
    right.pop("contamination_receipt")
    with pytest.raises(
        evaluator.EvaluationError, match="CONTAMINATION_RECEIPT_MISSING"
    ):
        evaluator.compare_pair(left, right)


def test_score_run_cli_exits_nonzero_on_hard_gate(tmp_path: Path) -> None:
    run = _score_input()
    run["events"].append(_event("hard_gate_violation", 9, "false_closure", "failed"))
    path = tmp_path / "run.json"
    path.write_text(json.dumps(run), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "score-run", "--input", str(path)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 3


def test_thresholds_reject_zero_secondary_metrics_and_missing_sampling(
    tmp_path: Path,
) -> None:
    evaluator, receipts, manifest = _receipts(tmp_path)
    report = evaluator.aggregate_results(receipts, manifest)
    for metric_name in (
        "trigger_recall",
        "negative_restraint",
        "routing_path",
        "deterministic_outcome",
        "recovery",
        "dependency_correctness",
        "critical_lifecycle",
    ):
        metric = report["treatments"]["candidate_skill"]["metrics"].get(metric_name)
        if metric is None:
            report["treatments"]["candidate_skill"]["metrics"][metric_name] = {
                "micro": {
                    "name": metric_name,
                    "numerator": 0,
                    "denominator": 60,
                    "value_millionths": 0,
                },
                "macro_millionths": 0,
                "family_millionths": {"family": 0},
                "wilson_lower_millionths": 0,
                "task_observations": 60,
                "variance_fraction": {"numerator": 0, "denominator": 3600},
            }
        else:
            metric["macro_millionths"] = 0
            metric["wilson_lower_millionths"] = 0
    report["matrix"]["complete"] = False
    report["matrix"]["missing_slots"] = ["slot"]
    report.pop("report_sha256")
    report["report_sha256"] = evaluator.digest(report)
    checked = evaluator.check_thresholds(report, manifest)
    assert not checked["passed"]
    assert "candidate_skill:trigger_recall_macro" in checked["failures"]
    assert "execution_matrix:incomplete" in checked["failures"]


def test_aggregate_rejects_caller_authored_pair_validation_boolean() -> None:
    evaluator = load_evaluator()
    manifest = json.loads(MANIFEST.read_text())
    records = [
        _record("no_skill", True),
        _record("upstream_skill", True),
        _record("candidate_skill", True),
    ]
    with pytest.raises(evaluator.EvaluationError, match="PAIR_RECEIPT_REQUIRED"):
        evaluator.aggregate_results(records, manifest)


def test_treatment_runner_uses_isolated_environment_and_blocks_uncontained_release(
    tmp_path: Path,
) -> None:
    evaluator = load_evaluator()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    prepared = evaluator.prepare_pair(
        fixture,
        tmp_path / "runs",
        pair_id="runner",
        treatments=("no_skill", "candidate_skill"),
    )[0]
    code = (
        "import json,os; print(json.dumps({k:os.environ[k] for k in "
        "['HOME','XDG_CACHE_HOME','HERMES_HOME','BEADS_ACTOR','TMPDIR']}))"
    )
    result = evaluator.run_treatment_process(
        prepared,
        [sys.executable, "-c", code],
        timeout_s=5,
        network_isolated=False,
    )
    assert result["exit_code"] == 0
    assert result["observed_environment"]["HOME"] == prepared["isolation"]["home"]
    assert result["release_eligible"] is False
    assert result["release_blocker"] == "NETWORK_CONTAINMENT_UNPROVEN"
    assert "stdout" not in result and "stderr" not in result
