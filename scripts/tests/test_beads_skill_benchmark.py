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
FIXTURES = REPO / "scripts/tests/fixtures/beads_contract/valid"
PUBLIC_REPORT = REPO / "evaluation/beads-skill/public-reports/not-evaluated-v1.json"
SHA = "a" * 64
RUN_ID = "run-0123456789abcdef-20260903T120000.000000Z-ABCDEFGH"


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
            "process_namespace": f"{prefix}/process",
        },
    }


def _worker(**overrides: Any) -> dict[str, Any]:
    worker = json.loads(
        (FIXTURES / "worker-execution-result-v1.json").read_text(encoding="utf-8")
    )
    worker["changes"] = {"paths": ["file.py"], "outside_allowed_scope": []}
    worker.update(overrides)
    return worker


def _checkpoint() -> dict[str, Any]:
    return json.loads((FIXTURES / "run-checkpoint-v1.json").read_text(encoding="utf-8"))


def _recovery_probe() -> dict[str, Any]:
    entry = json.loads(
        (FIXTURES / "operation-journal-event-v1.json").read_text(encoding="utf-8")
    )
    return entry["recovery_probe"]


def _journal_entry(effect: str, issue: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": SHA,
        "run_id": RUN_ID,
        "attempt_id": None,
        "issue_id": issue,
        "ownership_epoch": None,
        "effect_type": effect,
        "immutable_input_path": "/tmp/x",
        "immutable_input_sha256": SHA,
        "expected_pre_state_sha256": SHA,
        "recovery_probe": _recovery_probe(),
        "timestamp": "2026-09-03T12:00:00.000000Z",
        "authority_class": "custodian",
        "previous_event_sha256": SHA,
        "phase": "PREPARED",
    }


def _scope_violating_worker(scope: str) -> dict[str, Any]:
    """A schema-valid failed worker that mutated outside its allowed scope."""
    return _worker(
        status="failed",
        changes={"paths": ["file.py"], "outside_allowed_scope": [scope]},
        errors=[
            {
                "code": "scope",
                "template_id": "out_of_scope",
                "field_path": "changes",
                "parameters": [],
            }
        ],
    )


def _default_journal() -> list[dict[str, Any]]:
    return [
        _journal_entry("skill_load"),
        _journal_entry("tool_identity"),
        _journal_entry("version_check"),
        _journal_entry("workspace_where"),
        _journal_entry("prime"),
        _journal_entry("bd_show", issue="scc-x"),
        _journal_entry("bd_update_status_claimed", issue="scc-x"),
        _journal_entry("bd_claim_readback", issue="scc-x"),
    ]


def _capture(
    evaluator,
    *,
    journal: list[dict[str, Any]] | None = None,
    worker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources = {
        "worker_execution_result": worker if worker is not None else _worker(),
        "run_checkpoint": _checkpoint(),
        "operation_journal": _default_journal() if journal is None else journal,
    }
    return {
        **sources,
        "bundle": evaluator.adapt_runtime_evidence(**sources),
    }


def _score_input(
    *,
    journal: list[dict[str, Any]] | None = None,
    worker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluator = load_evaluator()
    capture = _capture(evaluator, journal=journal, worker=worker)
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
        "events": deepcopy(capture["bundle"]["events"]),
        "outcome_checks": deepcopy(capture["bundle"]["outcome_checks"]),
        "capture_evidence": capture,
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
        run = _score_input(
            worker=(
                _scope_violating_worker("false_closure")
                if (candidate_hard and treatment == "candidate_skill")
                else None
            )
        )
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


def _event(
    kind: str, _order: int, target: str | None = None, result: str = "passed"
) -> dict[str, Any]:
    return {
        "event_type": kind,
        "target_identity": target,
        "result_class": result,
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
    assert [event["order"] for event in first] == list(range(len(events)))
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
    reordered = _score_input(
        journal=[
            _journal_entry("skill_load"),
            _journal_entry("tool_identity"),
            _journal_entry("version_check"),
            _journal_entry("workspace_where"),
            _journal_entry("prime"),
            _journal_entry("bd_show", issue="scc-x"),
            _journal_entry("bd_claim_readback", issue="scc-x"),
            _journal_entry("bd_update_status_claimed", issue="scc-x"),
        ]
    )
    failed = evaluator.score_run(reordered)
    assert {item["code"] for item in failed["compliance_items"]} == {"CLM"}


def test_false_close_is_a_hard_gate_even_with_perfect_other_steps() -> None:
    evaluator = load_evaluator()
    run = _score_input(worker=_scope_violating_worker("false_closure"))
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
    manifest["status"] = "frozen"
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
        report, evaluator.check_thresholds(report, manifest, receipts)
    )
    second = evaluator.render_markdown(
        report, evaluator.check_thresholds(report, manifest, receipts)
    )
    assert first == second
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(receipts), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "check-thresholds",
            "--report",
            str(report_path),
            "--manifest",
            str(manifest_path),
            "--records",
            str(records_path),
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


def test_caller_authored_events_cannot_fabricate_compliance() -> None:
    evaluator = load_evaluator()
    run = _score_input()
    run["events"] = [
        _event("assertion_check", index, code)
        for index, code in enumerate(run["required_assertions"])
    ]
    with pytest.raises(evaluator.EvaluationError, match="EVIDENCE_DERIVATION_MISMATCH"):
        evaluator.score_run(run)


def test_normalizer_rejects_caller_supplied_order_and_evidence_hash() -> None:
    evaluator = load_evaluator()
    forged = [_event("code_mutation", 100), _event("skill_load", 0)]
    forged[0]["evidence_sha256"] = SHA
    with pytest.raises(evaluator.EvaluationError, match="FORGED_CAPTURE_FIELD"):
        evaluator.normalize_trajectory(forged)


def test_adapter_rejects_identity_and_version_drift() -> None:
    evaluator = load_evaluator()
    checkpoint = _checkpoint()
    drifted = dict(checkpoint)
    drifted["run_id"] = "run-ffffffffffffffff-20260903T120000.000000Z-ZZZZZZZZ"
    with pytest.raises(evaluator.EvaluationError, match="EVIDENCE_IDENTITY_MISMATCH"):
        evaluator.adapt_runtime_evidence(_worker(), drifted, [])
    future = _worker()
    future["schema_version"] = "beads.worker-execution-result.v2"
    with pytest.raises(
        evaluator.EvaluationError, match="EVIDENCE_SOURCE_VERSION_UNSUPPORTED"
    ):
        evaluator.adapt_runtime_evidence(future, checkpoint, [])
    invalid = _worker()
    invalid["status"] = "weird"
    with pytest.raises(
        evaluator.EvaluationError, match="EVIDENCE_SOURCE_SCHEMA_INVALID"
    ):
        evaluator.adapt_runtime_evidence(invalid, checkpoint, [])


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
    run = _score_input(worker=_scope_violating_worker("false_closure"))
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


def test_thresholds_reject_report_drift_from_pair_receipts(tmp_path: Path) -> None:
    evaluator, receipts, manifest = _receipts(tmp_path)
    report = evaluator.aggregate_results(receipts, manifest)
    report["treatments"]["candidate_skill"]["metrics"]["procedural_compliance"][
        "macro_millionths"
    ] = 0
    report.pop("report_sha256")
    report["report_sha256"] = evaluator.digest(report)
    with pytest.raises(evaluator.EvaluationError, match="REPORT_RECOMPUTE_MISMATCH"):
        evaluator.check_thresholds(report, manifest, receipts)


def test_thresholds_honor_manifest_status_blocker_and_zero_records(
    tmp_path: Path,
) -> None:
    evaluator, receipts, manifest = _receipts(tmp_path)
    blocked = json.loads(MANIFEST.read_text())
    blocked_report = evaluator.aggregate_results(receipts, blocked)
    checked = evaluator.check_thresholds(blocked_report, blocked, receipts)
    assert not checked["passed"]
    assert (
        "manifest_status:release-blocked-alternative-stratum-unfrozen"
        in checked["failures"]
    )
    assert "release_matrix:blocker" in checked["failures"]
    zero_report = evaluator.aggregate_results([], manifest)
    zero_record = evaluator.check_thresholds(zero_report, manifest, [])
    assert not zero_record["passed"]
    assert "records:empty" in zero_record["failures"]


def test_zero_run_public_report_stays_honestly_blocked(tmp_path: Path) -> None:
    records_path = tmp_path / "records.json"
    records_path.write_text("[]", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "check-thresholds",
            "--report",
            str(PUBLIC_REPORT),
            "--manifest",
            str(MANIFEST),
            "--records",
            str(records_path),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 3
    failures = json.loads(completed.stdout)["failures"]
    assert "records:empty" in failures
    assert "manifest_status:release-blocked-alternative-stratum-unfrozen" in failures
    assert "release_matrix:blocker" in failures


def _forged_perfect_report() -> dict[str, Any]:
    evaluator = load_evaluator()
    manifest = json.loads(MANIFEST.read_text())

    def perfect_metric(name: str) -> dict[str, Any]:
        return {
            "micro": {
                "name": name,
                "numerator": 1,
                "denominator": 1,
                "value_millionths": 1_000_000,
            },
            "macro_millionths": 1_000_000,
            "family_millionths": {"f": 1_000_000},
            "wilson_lower_millionths": 1_000_000,
            "task_observations": 1,
            "variance_fraction": {"numerator": 0, "denominator": 1},
        }

    empty_stats = {
        "count": 0,
        "mean": None,
        "mean_fraction": None,
        "median": None,
        "median_fraction": None,
        "p95": None,
        "worst": None,
        "variance": None,
        "variance_fraction": None,
    }
    metric_names = (
        "procedural_compliance",
        "deterministic_outcome",
        "hard_gate",
        "routing_path",
        "boundary_restraint",
        "recovery",
        "dependency_correctness",
        "critical_lifecycle",
        "trigger_recall",
        "negative_restraint",
    )
    treatments = {}
    for treatment in ("no_skill", "upstream_skill", "candidate_skill"):
        treatments[treatment] = {
            "runs": 1,
            "micro": {
                "name": "procedural_compliance",
                "numerator": 1,
                "denominator": 1,
                "value_millionths": 1_000_000,
            },
            "macro_millionths": 1_000_000,
            "family_millionths": {"f": 1_000_000},
            "wilson_lower_millionths": 1_000_000,
            "metrics": {name: perfect_metric(name) for name in metric_names},
            "hard_gate_failures": 0,
            "telemetry": {
                key: dict(empty_stats)
                for key in (
                    "tokens",
                    "cost_microunits",
                    "latency_milliseconds",
                    "tool_calls",
                )
            },
            "telemetry_unavailable": {},
        }
    report = {
        "schema_version": "beads.evaluation-report.v1",
        "corpus_sha256": manifest["corpus_sha256"],
        "verifier_sha256": manifest["verifier_sha256"],
        "source_contracts": manifest["source_contracts"],
        "budget_profiles_sha256": manifest["budget_profiles_sha256"],
        "treatments": treatments,
        "excluded_runs": {},
        "local_gap": {
            "scenario_ids": [],
            "family_lifts_millionths": {},
            "paired_deltas": [],
        },
        "lifts": {
            "absolute_compliance_lift_millionths": 0,
            "incremental_compliance_lift_millionths": 0,
            "local_gap_incremental_lift_millionths": 1_000_000,
        },
        "matrix": {
            "expected_slots": 1,
            "observed_slots": 1,
            "missing_slots": [],
            "extra_slots": [],
            "complete": True,
        },
        "strata": {
            "primary": {"observed_runs": 1, "expected_runs": 1, "sample_complete": True}
        },
        "pair_receipt_sha256": [],
        "semantic_judgments": [],
        "efficiency": {
            "candidate_vs_no_skill": {
                "regression_millionths": {},
                "missing_telemetry": {},
            },
            "candidate_vs_upstream_skill": {
                "regression_millionths": {},
                "missing_telemetry": {},
            },
        },
        "efficiency_tail_violations": [],
        "record_count": 0,
    }
    report["report_sha256"] = evaluator.digest(report)
    return report


def test_forged_zero_record_perfect_report_cannot_pass_thresholds(
    tmp_path: Path,
) -> None:
    report = _forged_perfect_report()
    manifest = json.loads(MANIFEST.read_text())
    manifest["status"] = "frozen"
    manifest["release_matrix"]["blocker"] = None
    report_path = tmp_path / "forged-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    records_path = tmp_path / "records.json"
    records_path.write_text("[]", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "check-thresholds",
            "--report",
            str(report_path),
            "--manifest",
            str(manifest_path),
            "--records",
            str(records_path),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    payload = json.loads(completed.stdout) if completed.stdout else {}
    assert payload.get("passed") is not True


def _forged_invented_run() -> dict[str, Any]:
    return {
        "skill_sha256": SHA,
        "corpus_sha256": "03be9aafd2e39605e7a31d82bedff9d5d24061d7b2dddb1986fa78fa26b975c6",
        "verifier_sha256": "b" * 64,
        "model": "arbitrary-model",
        "provider": "arbitrary",
        "harness": "arbitrary",
        "harness_version": "v9",
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
            _event("issue_view", 5, "scc-x"),
            _event("claim", 6, "scc-x"),
            _event("claim_readback", 7, "scc-x"),
            _event("code_mutation", 8, "file.py"),
        ],
        "outcome_checks": [
            {
                "check_id": "state",
                "expected_sha256": "1" * 64,
                "observed_sha256": "1" * 64,
            }
        ],
        "telemetry": {
            "tokens": 1,
            "cost_microunits": None,
            "latency_milliseconds": 1,
            "tool_calls": 1,
        },
        "budget_profile": {
            "timeout_s": 900,
            "iterations": 160,
            "model_tokens": 40_000,
            "tool_calls": 80,
            "refs": 4,
        },
    }


def test_invented_events_and_outcome_hashes_fail_closed(tmp_path: Path) -> None:
    run = _forged_invented_run()
    path = tmp_path / "forged-run.json"
    path.write_text(json.dumps(run), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "score-run", "--input", str(path)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "capture_evidence" in completed.stderr
    tampered = _score_input()
    tampered["events"].append(_event("verification_pass", 99, "extra"))
    tampered_path = tmp_path / "tampered-run.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "score-run", "--input", str(tampered_path)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "EVIDENCE_DERIVATION_MISMATCH" in completed.stderr


def test_integrated_runtime_evidence_reaches_scoring_via_adapter(
    tmp_path: Path,
) -> None:
    worker_path = FIXTURES / "worker-execution-result-v1.json"
    checkpoint_path = FIXTURES / "run-checkpoint-v1.json"
    evidence_path = tmp_path / "evidence.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "adapt-evidence",
            "--worker-result",
            str(worker_path),
            "--checkpoint",
            str(checkpoint_path),
            "--output",
            str(evidence_path),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == "beads.evaluator-evidence.v1"
    assert evidence["adapter_version"] == "beads.evidence-adapter.v1"
    run = _score_input()
    run.update(
        {
            "events": deepcopy(evidence["events"]),
            "outcome_checks": deepcopy(evidence["outcome_checks"]),
            "capture_evidence": {
                "worker_execution_result": json.loads(
                    worker_path.read_text(encoding="utf-8")
                ),
                "run_checkpoint": json.loads(
                    checkpoint_path.read_text(encoding="utf-8")
                ),
                "operation_journal": [],
                "bundle": evidence,
            },
        }
    )
    run_path = tmp_path / "integrated-run.json"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "score-run", "--input", str(run_path)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["schema_version"] == (
        "beads.evaluation-result.v1"
    )
    for standalone in (worker_path, checkpoint_path):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "score-run", "--input", str(standalone)],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 2
        assert "RUN_FIELD_MISSING" in completed.stderr


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
