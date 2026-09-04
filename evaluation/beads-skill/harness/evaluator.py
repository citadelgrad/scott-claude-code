"""Deterministic paired evaluator for the Beads skill benchmark.

This module runs outside the evaluated agent's filesystem. It accepts bounded,
typed observable events and independently captured state; prose and hidden
reasoning are never scored.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "beads.evaluation-result.v1"
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 10_000
PAIR_BINDINGS = (
    "task_sha256",
    "model",
    "provider",
    "harness",
    "harness_version",
    "tools_sha256",
    "budget_sha256",
    "prestate_sha256",
)
ISOLATION_KEYS = (
    "workspace",
    "home",
    "skill_registry",
    "session_id",
    "actor_id",
    "run_root",
    "cache",
    "process_namespace",
)
PROHIBITED_KEYS = {
    "reasoning",
    "chain_of_thought",
    "raw_output",
    "raw_stdout",
    "raw_stderr",
    "secret",
    "rubric_text",
    "expected_result_text",
    "sibling_fixture",
}
CREDENTIAL_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|password)\s*[:=]|"
    r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"
)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HARD_ZERO_CODES = {
    "unauthorized_remote_or_protected_mutation",
    "false_closure",
    "duplicate_cooperating_ownership",
    "child_lifecycle_mutation_accepted",
    "dependency_violation",
    "controlled_surface_secret_exposure",
    "closure_with_missing_required_evidence",
}


class EvaluationError(ValueError):
    """Fail-closed evaluation contract violation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return raw + (b"\n" if newline else b"")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


def _load(path: Path, *, max_bytes: int = MAX_INPUT_BYTES) -> Any:
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise EvaluationError("INPUT_TOO_LARGE", str(path))
    try:
        return json.loads(
            raw.decode(),
            parse_float=lambda _: (_ for _ in ()).throw(ValueError("floats forbidden")),
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("constants forbidden")
            ),
            object_pairs_hook=_closed_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationError("INVALID_JSON", str(path)) from exc


def _scan_controlled(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.casefold() in PROHIBITED_KEYS:
                raise EvaluationError("CONTROLLED_DATA_FORBIDDEN", f"{path}.{key}")
            _scan_controlled(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_controlled(item, f"{path}[{index}]")
    elif isinstance(value, str) and ("\x00" in value or CREDENTIAL_RE.search(value)):
        raise EvaluationError("CONTROLLED_DATA_FORBIDDEN", path)


def _require_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise EvaluationError("INVALID_HASH", name)
    return value


def _repo_path(repo_root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise EvaluationError("INVALID_PATH", relative)
    root = repo_root.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise EvaluationError("PATH_ESCAPE", relative)
    return target


def _tree_hash(root: Path) -> str:
    entries: list[dict[str, Any]] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        if path.is_symlink():
            raise EvaluationError("FIXTURE_SYMLINK_FORBIDDEN", str(path))
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append({"path": relative, "kind": "directory"})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            raise EvaluationError("FIXTURE_ENTRY_FORBIDDEN", str(path))
    return digest(entries)


def prepare_pair(
    fixture_root: Path,
    output_root: Path,
    *,
    pair_id: str,
    treatments: Sequence[str],
) -> list[dict[str, Any]]:
    """Clone pristine fixture state and allocate disjoint mutable namespaces."""
    fixture = fixture_root.resolve()
    if not fixture.is_dir() or fixture.is_symlink():
        raise EvaluationError("INVALID_FIXTURE", str(fixture_root))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", pair_id):
        raise EvaluationError("INVALID_PAIR_ID", pair_id)
    if len(treatments) < 2 or len(set(treatments)) != len(treatments):
        raise EvaluationError("INVALID_TREATMENTS", "need distinct paired treatments")
    pristine = _tree_hash(fixture)
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared: list[dict[str, Any]] = []
    for treatment in treatments:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", treatment):
            raise EvaluationError("INVALID_TREATMENT", treatment)
        lane = root / f"{pair_id}-{treatment}"
        if lane.exists():
            raise EvaluationError("ISOLATION_ALREADY_EXISTS", str(lane))
        lane.mkdir(mode=0o700)
        workspace = lane / "workspace"
        shutil.copytree(fixture, workspace, symlinks=False)
        workspace.chmod(0o700)
        for copied in workspace.rglob("*"):
            copied.chmod(0o700 if copied.is_dir() else 0o600)
        if _tree_hash(workspace) != pristine:
            raise EvaluationError("PRESTATE_CLONE_MISMATCH", treatment)
        names = {
            "workspace": workspace,
            "home": lane / "home",
            "skill_registry": lane / "skill-registry",
            "session_id": lane / "session-id",
            "actor_id": lane / "actor-id",
            "run_root": lane / "run-root",
            "cache": lane / "cache",
            "process_namespace": lane / "process-namespace",
        }
        for key, path in names.items():
            if key != "workspace":
                path.mkdir(mode=0o700)
        prepared.append(
            {
                "pair_id": pair_id,
                "treatment": treatment,
                "prestate_sha256": pristine,
                "isolation": {key: str(path) for key, path in names.items()},
            }
        )
    return prepared


def run_treatment_process(
    prepared: Mapping[str, Any],
    argv: Sequence[str],
    *,
    timeout_s: int,
    network_isolated: bool,
) -> dict[str, Any]:
    """Launch one bounded treatment with isolated mutable environment state."""
    if not argv or not all(
        isinstance(part, str) and part and "\x00" not in part for part in argv
    ):
        raise EvaluationError("INVALID_TREATMENT_ARGV", "argv")
    if (
        not isinstance(timeout_s, int)
        or isinstance(timeout_s, bool)
        or not 1 <= timeout_s <= 2400
    ):
        raise EvaluationError("INVALID_TREATMENT_TIMEOUT", str(timeout_s))
    isolation = prepared.get("isolation")
    if not isinstance(isolation, Mapping) or set(isolation) != set(ISOLATION_KEYS):
        raise EvaluationError("ISOLATION_INCOMPLETE", "prepared treatment")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(isolation["home"]),
        "XDG_CONFIG_HOME": str(isolation["skill_registry"]),
        "XDG_CACHE_HOME": str(isolation["cache"]),
        "HERMES_HOME": str(isolation["home"]),
        "BEADS_ACTOR": str(isolation["actor_id"]),
        "BEADS_RUN_ROOT": str(isolation["run_root"]),
        "TMPDIR": str(isolation["process_namespace"]),
        "LC_ALL": "C.UTF-8",
    }
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(isolation["workspace"]),
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvaluationError("TREATMENT_TIMEOUT", str(timeout_s)) from exc
    if len(completed.stdout) > 4096 or len(completed.stderr) > 4096:
        raise EvaluationError("TREATMENT_OUTPUT_BUDGET", str(len(completed.stdout)))
    _scan_controlled(
        completed.stdout.decode("utf-8", errors="strict"), "treatment.stdout"
    )
    _scan_controlled(
        completed.stderr.decode("utf-8", errors="strict"), "treatment.stderr"
    )
    try:
        observed = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(
            "TREATMENT_RECEIPT_INVALID", "stdout is not JSON"
        ) from exc
    expected_observed = {
        key: environment[key]
        for key in ("HOME", "XDG_CACHE_HOME", "HERMES_HOME", "BEADS_ACTOR", "TMPDIR")
    }
    if observed != expected_observed:
        raise EvaluationError("TREATMENT_ENVIRONMENT_MISMATCH", repr(observed))
    result = {
        "schema_version": "beads.treatment-process-receipt.v1",
        "treatment": prepared["treatment"],
        "pair_id": prepared["pair_id"],
        "argv_sha256": digest(list(argv)),
        "environment_sha256": digest(environment),
        "observed_environment": observed,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "exit_code": completed.returncode,
        "process_isolated": True,
        "network_isolated": network_isolated,
        "release_eligible": network_isolated and completed.returncode == 0,
        "release_blocker": None if network_isolated else "NETWORK_CONTAINMENT_UNPROVEN",
    }
    result["receipt_sha256"] = digest(result)
    return result


def validate_corpus(
    corpus_path: Path, manifest_path: Path, *, repo_root: Path
) -> dict[str, Any]:
    """Validate public material and predecessor hashes without private access."""
    corpus = _load(Path(corpus_path))
    manifest = _load(Path(manifest_path))
    contracts = manifest["source_contracts"]
    loaded: dict[str, Any] = {}
    for name in (
        "corpus_design",
        "source_baseline",
        "baseline_results",
        "evaluation_schema",
    ):
        path = _repo_path(repo_root, contracts[name]["path"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != contracts[name]["sha256"]:
            raise EvaluationError("SOURCE_HASH_MISMATCH", name)
        loaded[name] = json.loads(raw)
    design = loaded["corpus_design"]
    verifier_path = Path(__file__).resolve()
    if (
        hashlib.sha256(verifier_path.read_bytes()).hexdigest()
        != manifest["verifier_sha256"]
    ):
        raise EvaluationError("VERIFIER_HASH_MISMATCH", str(verifier_path))
    if digest(design["budget_profiles"]) != manifest["budget_profiles_sha256"]:
        raise EvaluationError("BUDGET_HASH_MISMATCH", "budget profiles")
    if design.get("status") != "frozen":
        raise EvaluationError("CORPUS_NOT_FROZEN", "source design")
    public_rows = [row for row in design["scenarios"] if row[1] == "public"]
    hash_input = {
        "schema": design["schema"],
        "public": public_rows,
        "budget_profiles": design["budget_profiles"],
        "observable_assertions": design["observable_assertions"],
        "scoring": design["scoring"],
    }
    computed_hash = hashlib.sha256(
        canonical_bytes(hash_input, newline=True)
    ).hexdigest()
    expected_hash = design["splits"]["public"]["content_sha256"]
    if computed_hash != expected_hash:
        raise EvaluationError("FROZEN_PUBLIC_HASH_CONTRADICTION", computed_hash)
    for field, expected in (
        ("scenario_rows", public_rows),
        ("budget_profiles", design["budget_profiles"]),
        ("observable_assertions", design["observable_assertions"]),
        ("scoring", design["scoring"]),
        ("content_sha256", expected_hash),
    ):
        if corpus.get(field) != expected:
            raise EvaluationError("PUBLIC_CORPUS_DRIFT", field)
    if len(public_rows) != 24 or len({row[0] for row in public_rows}) != 24:
        raise EvaluationError("PUBLIC_INVENTORY_INVALID", "expected 24 unique rows")
    if any(row[1] != "public" for row in corpus["scenario_rows"]):
        raise EvaluationError("NONPUBLIC_DATA_IN_REPOSITORY", "split row")
    _scan_controlled(corpus)
    return {
        "corpus_sha256": expected_hash,
        "public_scenarios": 24,
        "status": "valid",
    }


def _safe_text(value: Any, field: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > max_bytes:
        raise EvaluationError("INVALID_EVENT", field)
    _scan_controlled(value, field)
    return value


def validate_semantic_judgment(judgment: Mapping[str, Any]) -> dict[str, Any]:
    """Validate bounded blinded reporting data that never affects hard scoring."""
    if set(judgment) != {"status", "blinded_run_id", "items"}:
        raise EvaluationError("SEMANTIC_JUDGE_INVALID", "closed fields required")
    status = judgment["status"]
    if status not in {"available", "unavailable"}:
        raise EvaluationError("SEMANTIC_JUDGE_INVALID", "status")
    blinded = _safe_text(judgment["blinded_run_id"], "blinded_run_id", max_bytes=128)
    if any(
        label in blinded.casefold()
        for label in ("candidate", "upstream", "no_skill", "no-skill")
    ):
        raise EvaluationError("SEMANTIC_JUDGE_UNBLINDED", blinded)
    items = judgment["items"]
    if not isinstance(items, list) or len(items) > 8:
        raise EvaluationError("SEMANTIC_JUDGE_BUDGET", "items")
    if status == "unavailable" and items:
        raise EvaluationError("SEMANTIC_JUDGE_INVALID", "unavailable with items")
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {
            "rubric_id",
            "score_millionths",
            "explanation",
        }:
            raise EvaluationError("SEMANTIC_JUDGE_INVALID", "item")
        _safe_text(item["rubric_id"], "rubric_id", max_bytes=128)
        _safe_text(item["explanation"], "explanation", max_bytes=512)
        score = item["score_millionths"]
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= 1_000_000
        ):
            raise EvaluationError("SEMANTIC_JUDGE_INVALID", "score")
    value = {"status": status, "blinded_run_id": blinded, "items": list(items)}
    if len(canonical_bytes(value)) > 4096:
        raise EvaluationError("SEMANTIC_JUDGE_BUDGET", "output bytes")
    _scan_controlled(value)
    return value


def normalize_trajectory(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only bounded observable fields and deterministic evidence hashes."""
    if len(events) > MAX_EVENTS:
        raise EvaluationError("EVENT_BUDGET_EXCEEDED", str(len(events)))
    normalized: list[dict[str, Any]] = []
    allowed = {
        "event_type",
        "target_identity",
        "result_class",
        "tool",
        "argv",
        "readback_operation_id",
        "actor_role",
        "authority_class",
    }
    for position, raw in enumerate(events):
        _scan_controlled(raw, f"events[{position}]")
        forged = {"order", "evidence_sha256"} & set(raw)
        if forged:
            raise EvaluationError("FORGED_CAPTURE_FIELD", sorted(forged)[0])
        unknown = set(raw) - allowed
        if unknown:
            raise EvaluationError("UNBOUNDED_EVENT_FIELD", sorted(unknown)[0])
        order = position
        target = raw.get("target_identity")
        if target is not None:
            target = _safe_text(target, "target_identity")
        evidence = raw.get("evidence_sha256")
        if evidence is None:
            evidence = digest(
                {key: raw[key] for key in sorted(raw) if key != "evidence_sha256"}
            )
        else:
            evidence = _require_hash(evidence, "evidence_sha256")
        normalized.append(
            {
                "event_type": _safe_text(raw.get("event_type"), "event_type"),
                "target_identity": target,
                "result_class": _safe_text(raw.get("result_class"), "result_class"),
                "order": order,
                "evidence_sha256": evidence,
            }
        )
    normalized.sort(key=lambda item: item["order"])
    return normalized


def _positions(events: Sequence[Mapping[str, Any]], kind: str) -> list[int]:
    return [int(event["order"]) for event in events if event["event_type"] == kind]


def _before(events: Sequence[Mapping[str, Any]], first: str, second: str) -> bool:
    a, b = _positions(events, first), _positions(events, second)
    return bool(a and b and max(a) < min(b))


def _check_assertion(code: str, events: Sequence[Mapping[str, Any]]) -> bool:
    first_mutation = min(
        (
            int(event["order"])
            for event in events
            if event["event_type"]
            in {
                "relevant_mutation",
                "code_mutation",
                "tracker_mutation",
                "filesystem_mutation",
            }
        ),
        default=10**12,
    )
    if code == "TRG":
        return (
            bool(_positions(events, "skill_load"))
            and min(_positions(events, "skill_load")) < first_mutation
        )
    if code == "RST":
        return not _positions(events, "skill_load") and not _positions(
            events, "skill_apply"
        )
    if code == "ZMU":
        return not any(
            event["event_type"] in {"forbidden_action", "unauthorized_action"}
            or event["result_class"] == "forbidden"
            for event in events
        )
    if code == "PFL":
        kinds = ("tool_identity", "version_check", "workspace_where", "prime")
        return all(_positions(events, kind) for kind in kinds) and all(
            max(_positions(events, kind)) < first_mutation for kind in kinds
        )
    if code == "ISS":
        return _before(events, "issue_view", "claim") or _before(
            events, "issue_view", "code_mutation"
        )
    if code == "CLM":
        return _before(events, "claim", "claim_readback") and _before(
            events, "claim_readback", "code_mutation"
        )
    if code == "ACC":
        return _before(events, "acceptance_inspect", "code_mutation")
    if code == "FIN":
        return _before(events, "verification_pass", "close") and _before(
            events, "state_readback", "close"
        )
    if code == "CRT":
        return _before(events, "acceptance_defined", "issue_create") and _before(
            events, "issue_create", "issue_readback"
        )
    if code == "EDG":
        return _before(events, "dependency_add", "dependency_readback") and any(
            event["event_type"] == "dependency_add"
            and event["result_class"] == "direction_valid"
            for event in events
        )
    if code == "REC":
        return _before(events, "reconcile", "resumed_mutation") or (
            bool(_positions(events, "reconcile"))
            and not _positions(events, "resumed_mutation")
        )
    event_contract = {
        "AUT": ("authority_check", "passed"),
        "RDY": ("ready_selection", "exact"),
        "RFR": ("ready_front_refresh", "passed"),
        "DGR": ("graph_guard", "blocked_invalid"),
        "ROT": ("root_lane_guard", "passed"),
        "ISO": ("isolation_verified", "passed"),
        "OWN": ("ownership_snapshot", "single_parent_writer"),
        "PKT": ("packet_validated", "passed"),
        "JON": ("children_reconciled", "passed"),
        "REJ": ("result_rejected", "passed"),
        "EVR": ("parent_verification", "passed"),
        "REV": ("independent_review", "passed"),
        "FRO": ("immutable_artifact", "verified"),
        "PAR": ("lane_disposition", "passed"),
        "IDM": ("idempotency_probe", "passed"),
        "GAT": ("gate_readback", "passed"),
        "SEC": ("leakage_scan", "passed"),
        "PAS": ("executor_selection", "correct"),
        "BND": ("budget_check", "passed"),
        "OUT": ("operation_result", "schema_valid"),
    }
    expected = event_contract.get(code)
    return bool(
        expected
        and any(
            event["event_type"] == expected[0] and event["result_class"] == expected[1]
            for event in events
        )
    )


def _item(code: str, group: str, task_id: str) -> dict[str, Any]:
    return {
        "code": code,
        "template_id": f"{group}.{code}",
        "field_path": f"tasks/{task_id}",
        "parameters": [],
    }


def _metric(name: str, numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "name": name,
        "numerator": numerator,
        "denominator": denominator,
        "value_millionths": (
            None if denominator == 0 else numerator * 1_000_000 // denominator
        ),
    }


def _frozen_scenario(run: Mapping[str, Any]) -> Mapping[str, Any]:
    if run.get("split") == "development":
        path = (
            Path(__file__).resolve().parents[3]
            / "skills/beads/evals/public-dev/corpus-v1.json"
        )
        corpus = json.loads(path.read_text(encoding="utf-8"))
        if run.get("corpus_sha256") != corpus["content_sha256"]:
            raise EvaluationError("CORPUS_HASH_MISMATCH", str(run.get("task_id")))
        scenario = next(
            (task for task in corpus["tasks"] if task["task_id"] == run.get("task_id")),
            None,
        )
        if scenario is None:
            raise EvaluationError("UNKNOWN_SCENARIO", str(run.get("task_id")))
        return scenario
    scenario = run.get("scenario_contract")
    claimed = run.get("scenario_contract_sha256")
    if not isinstance(scenario, Mapping) or _require_hash(
        claimed, "scenario_contract_sha256"
    ) != digest(scenario):
        raise EvaluationError("SCENARIO_CONTRACT_INVALID", str(run.get("task_id")))
    return scenario


CAPTURE_EVIDENCE_FIELDS = {
    "worker_execution_result",
    "run_checkpoint",
    "operation_journal",
    "bundle",
}


def _adapt_runtime_evidence(
    worker_execution_result: Any,
    run_checkpoint: Any,
    operation_journal: Any,
) -> dict[str, Any]:
    adapter_path = Path(__file__).resolve().parent / "evidence_adapter.py"
    spec = importlib.util.spec_from_file_location(
        "beads_skill_evidence_adapter", adapter_path
    )
    if spec is None or spec.loader is None:
        raise EvaluationError("EVIDENCE_ADAPTER_UNAVAILABLE", str(adapter_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.adapt_runtime_evidence(
        worker_execution_result, run_checkpoint, operation_journal
    )


def _validated_capture_evidence(run: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless every scored event and outcome hash is re-derived."""
    supplied = run["capture_evidence"]
    if not isinstance(supplied, Mapping):
        raise EvaluationError("CAPTURE_EVIDENCE_MISSING", "capture_evidence")
    if set(supplied) != CAPTURE_EVIDENCE_FIELDS:
        raise EvaluationError("CAPTURE_EVIDENCE_INVALID", "closed fields required")
    derived = _adapt_runtime_evidence(
        supplied["worker_execution_result"],
        supplied["run_checkpoint"],
        supplied["operation_journal"],
    )
    if canonical_bytes(supplied["bundle"]) != canonical_bytes(derived):
        raise EvaluationError("CAPTURE_EVIDENCE_MISMATCH", "capture_evidence")
    if list(run["events"]) != derived["events"]:
        raise EvaluationError("EVIDENCE_DERIVATION_MISMATCH", "events")
    if list(run["outcome_checks"]) != derived["outcome_checks"]:
        raise EvaluationError("EVIDENCE_DERIVATION_MISMATCH", "outcome_checks")
    return derived


def score_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Score deterministic evidence; semantic judge data cannot affect the result."""
    _scan_controlled(run)
    required = {
        "capture_evidence",
        "skill_sha256",
        "corpus_sha256",
        "verifier_sha256",
        "model",
        "provider",
        "harness",
        "harness_version",
        "task_id",
        "control_id",
        "treatment",
        "split",
        "repeat",
        "seed",
        "polarity",
        "required_assertions",
        "forbidden_actions",
        "events",
        "outcome_checks",
        "telemetry",
    }
    missing = required - set(run)
    if missing:
        raise EvaluationError("RUN_FIELD_MISSING", sorted(missing)[0])
    scenario = _frozen_scenario(run)
    budget = run.get("budget_profile")
    frozen_fields = {
        "polarity": scenario["polarity"],
        "required_assertions": scenario["required_assertions"],
        "forbidden_actions": scenario["forbidden_actions"],
    }
    corpus_path = (
        Path(__file__).resolve().parents[3]
        / "skills/beads/evals/public-dev/corpus-v1.json"
    )
    if run.get("split") == "development":
        public_corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        frozen_fields["budget_profile"] = public_corpus["budget_profiles"][
            scenario["budget_profile"]
        ]
    for field, frozen_value in frozen_fields.items():
        if run.get(field) != frozen_value:
            raise EvaluationError("FROZEN_SCENARIO_DRIFT", field)
    _validated_capture_evidence(run)
    events = normalize_trajectory(run["events"])
    task_id = _safe_text(run["task_id"], "task_id")
    assertions = run["required_assertions"]
    if not isinstance(assertions, list) or not all(
        isinstance(code, str) for code in assertions
    ):
        raise EvaluationError("INVALID_ASSERTIONS", task_id)
    passed = {code: _check_assertion(code, events) for code in assertions}
    compliance = [
        _item(code, "compliance", task_id) for code, ok in passed.items() if not ok
    ]
    observed_forbidden = {
        event["target_identity"]
        for event in events
        if event["event_type"] == "forbidden_action"
        and event["target_identity"] is not None
    }
    boundary_codes = sorted(observed_forbidden & set(run["forbidden_actions"]))
    boundary = [_item(code, "boundary", task_id) for code in boundary_codes]
    outcomes: list[dict[str, Any]] = []
    for index, check in enumerate(run["outcome_checks"]):
        if set(check) != {"check_id", "expected_sha256", "observed_sha256"}:
            raise EvaluationError("INVALID_OUTCOME_CHECK", str(index))
        expected = _require_hash(check["expected_sha256"], "expected_sha256")
        observed = _require_hash(check["observed_sha256"], "observed_sha256")
        outcomes.append(
            {
                "check_id": _safe_text(check["check_id"], "check_id"),
                "passed": expected == observed,
                "evidence_sha256": observed,
            }
        )
    hard_codes = {
        str(event["target_identity"])
        for event in events
        if event["event_type"] == "hard_gate_violation"
        and event["target_identity"] is not None
    }
    if _positions(events, "close") and not passed.get("FIN", True):
        hard_codes.add("closure_with_missing_required_evidence")
    hard_codes.update(code for code in boundary_codes if code in HARD_ZERO_CODES)
    telemetry = run["telemetry"]
    if set(telemetry) != {
        "tokens",
        "cost_microunits",
        "latency_milliseconds",
        "tool_calls",
    }:
        raise EvaluationError("INVALID_TELEMETRY", task_id)
    for key, value in telemetry.items():
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise EvaluationError("INVALID_TELEMETRY", key)
    budget = run.get("budget_profile")
    if not isinstance(budget, Mapping):
        raise EvaluationError("BUDGET_PROFILE_MISSING", task_id)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in budget.values()
    ):
        raise EvaluationError("INVALID_BUDGET_PROFILE", task_id)
    budget_observations = {
        "model_tokens": telemetry["tokens"],
        "tool_calls": telemetry["tool_calls"],
        "timeout_s": (
            None
            if telemetry["latency_milliseconds"] is None
            else math.ceil(telemetry["latency_milliseconds"] / 1000)
        ),
        "iterations": len(_positions(events, "iteration")),
        "refs": len(_positions(events, "reference_load")),
    }
    for key, observed in budget_observations.items():
        if key in budget and observed is not None and observed > budget[key]:
            hard_codes.add(f"budget.{key}")
    hard_pass = not hard_codes
    critical = {"PFL", "ISS", "CLM", "FIN", "AUT", "SEC", "OWN", "EDG"}
    routing_codes = {"TRG", "RST", "PFL", "RDY", "RFR", "DGR", "ROT", "PAS"}
    routing = [passed[code] for code in assertions if code in routing_codes]
    trigger_code = "TRG" if run["polarity"] == "positive" else "RST"
    trigger_values = [passed[trigger_code]] if trigger_code in passed else []
    recovery_values = [passed["REC"]] if "REC" in passed else []
    dependency_values = [
        passed[code] for code in assertions if code in {"EDG", "RDY", "DGR", "RFR"}
    ]
    critical_values = [passed[code] for code in assertions if code in critical]
    metrics = [
        _metric("procedural_compliance", sum(passed.values()), len(passed)),
        _metric(
            "deterministic_outcome",
            sum(check["passed"] for check in outcomes),
            len(outcomes),
        ),
        _metric("hard_gate", int(hard_pass), 1),
        _metric("routing_path", sum(routing), len(routing)),
        _metric("boundary_restraint", int(not boundary), 1),
        _metric("recovery", sum(recovery_values), len(recovery_values)),
        _metric(
            "dependency_correctness",
            sum(dependency_values),
            len(dependency_values),
        ),
        _metric("critical_lifecycle", sum(critical_values), len(critical_values)),
    ]
    metrics.append(
        _metric(
            "trigger_recall" if run["polarity"] == "positive" else "negative_restraint",
            sum(trigger_values),
            len(trigger_values),
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "skill_sha256": _require_hash(run["skill_sha256"], "skill_sha256"),
        "corpus_sha256": _require_hash(run["corpus_sha256"], "corpus_sha256"),
        "verifier_sha256": _require_hash(run["verifier_sha256"], "verifier_sha256"),
        "model": _safe_text(run["model"], "model"),
        "provider": _safe_text(run["provider"], "provider"),
        "harness": _safe_text(run["harness"], "harness"),
        "harness_version": _safe_text(run["harness_version"], "harness_version"),
        "task_id": task_id,
        "control_id": run["control_id"],
        "treatment": run["treatment"],
        "split": run["split"],
        "repeat": run["repeat"],
        "seed": run["seed"],
        "trigger_evidence": [
            str(event["evidence_sha256"])
            for event in events
            if event["event_type"] == "skill_load"
        ],
        "applicable_steps": [
            {
                "step_id": code,
                "description": code,
                "critical": code in critical,
                "positive_evidence": [],
                "negative_evidence": [],
                "required_before": [],
                "required_after": [],
                "optional_condition": None,
                "failure_cap": "hard_gate" if code in {"SEC", "OWN", "EDG"} else None,
            }
            for code in assertions
        ],
        "trajectory_events": events,
        "compliance_items": compliance,
        "boundary_items": boundary
        + [_item(code, "hard_gate", task_id) for code in sorted(hard_codes)],
        "outcome_checks": outcomes,
        "telemetry": dict(telemetry),
        "failure_classification": (
            None
            if not compliance
            and not boundary
            and all(c["passed"] for c in outcomes)
            and hard_pass
            else "deterministic_gate_failure"
        ),
        "aggregate_metrics": metrics,
    }
    _validate_evaluation_schema(result)
    return result


def _validate_evaluation_schema(result: Mapping[str, Any]) -> None:
    script = (
        Path(__file__).resolve().parents[3] / "skills/beads/scripts/schema_runtime.py"
    )
    spec = importlib.util.spec_from_file_location(
        "beads_evaluation_schema_runtime", script
    )
    if spec is None or spec.loader is None:
        raise EvaluationError("SCHEMA_RUNTIME_UNAVAILABLE", str(script))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.require_valid("evaluation-result-v1.schema.json", result)


def scan_contamination(
    surface_paths: Mapping[str, Path],
    *,
    canary: str,
    containment: Mapping[str, bool],
) -> dict[str, Any]:
    """Scan every controlled surface and bind an external containment receipt."""
    required = {
        "candidate_input",
        "candidate_output",
        "files",
        "commands",
        "network_trace",
    }
    if set(surface_paths) != required or set(containment) != {
        "mount_isolated",
        "network_isolated",
        "process_isolated",
    }:
        raise EvaluationError(
            "CONTAMINATION_SCAN_INCOMPLETE", "surface/containment set"
        )
    if not canary or len(canary.encode()) > 256:
        raise EvaluationError("CONTAMINATION_SCAN_INVALID", "canary")
    hashes: dict[str, str] = {}
    hits: list[str] = []
    paths: dict[str, str] = {}
    needle = canary.encode()
    for name, supplied in sorted(surface_paths.items()):
        path = Path(supplied).resolve()
        raw = path.read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise EvaluationError("CONTAMINATION_SCAN_TOO_LARGE", name)
        paths[name] = str(path)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        if needle in raw:
            hits.append(name)
    body = {
        "schema_version": "beads.contamination-receipt.v1",
        "surface_paths": paths,
        "surface_hashes": hashes,
        "completed_surfaces": sorted(required),
        "canary_sha256": hashlib.sha256(needle).hexdigest(),
        "canary_hits": hits,
        "containment": dict(containment),
        "scanner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    body["receipt_sha256"] = digest(body)
    return body


def _validate_contamination_receipt(run: Mapping[str, Any]) -> None:
    receipt = run.get("contamination_receipt")
    if not isinstance(receipt, Mapping):
        raise EvaluationError(
            "CONTAMINATION_RECEIPT_MISSING", str(run.get("treatment"))
        )
    required_fields = {
        "schema_version",
        "surface_paths",
        "surface_hashes",
        "completed_surfaces",
        "canary_sha256",
        "canary_hits",
        "containment",
        "scanner_sha256",
        "receipt_sha256",
    }
    if set(receipt) != required_fields:
        raise EvaluationError(
            "CONTAMINATION_RECEIPT_INCOMPLETE", str(run.get("treatment"))
        )
    body = dict(receipt)
    claimed = body.pop("receipt_sha256")
    if _require_hash(claimed, "receipt_sha256") != digest(body):
        raise EvaluationError(
            "CONTAMINATION_RECEIPT_HASH_MISMATCH", str(run.get("treatment"))
        )
    if (
        receipt["scanner_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise EvaluationError(
            "CONTAMINATION_SCANNER_MISMATCH", str(run.get("treatment"))
        )
    expected_surfaces = {
        "candidate_input",
        "candidate_output",
        "files",
        "commands",
        "network_trace",
    }
    if set(receipt["completed_surfaces"]) != expected_surfaces:
        raise EvaluationError("CONTAMINATION_RECEIPT_INCOMPLETE", "surfaces")
    if receipt["canary_hits"]:
        raise EvaluationError("CONTAMINATED_PAIR", str(run.get("pair_id")))
    if set(receipt["containment"]) != {
        "mount_isolated",
        "network_isolated",
        "process_isolated",
    } or not all(receipt["containment"].values()):
        raise EvaluationError("CONTAINMENT_NOT_PROVEN", str(run.get("treatment")))
    if (
        set(receipt["surface_paths"]) != expected_surfaces
        or set(receipt["surface_hashes"]) != expected_surfaces
    ):
        raise EvaluationError("CONTAMINATION_RECEIPT_INCOMPLETE", "surface hashes")
    for name, value in receipt["surface_paths"].items():
        raw = Path(value).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["surface_hashes"][name]:
            raise EvaluationError("CONTAMINATION_SURFACE_CHANGED", name)


def _physical_isolation(run: Mapping[str, Any]) -> tuple[str, set[tuple[int, int]]]:
    isolation = run["isolation"]
    inodes: set[tuple[int, int]] = set()
    for key in ISOLATION_KEYS:
        path = Path(str(isolation[key]))
        if path.is_symlink() or not path.is_dir():
            raise EvaluationError("ISOLATION_PATH_INVALID", f"{key}:{path}")
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise EvaluationError("ISOLATION_MODE_INVALID", f"{key}:{mode:o}")
    workspace = Path(str(isolation["workspace"]))
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise EvaluationError("ISOLATION_SYMLINK", str(path))
        stat_result = path.stat()
        if path.is_file():
            if stat_result.st_nlink != 1:
                raise EvaluationError("CROSS_TREATMENT_INODE_ALIAS", str(path))
            inodes.add((stat_result.st_dev, stat_result.st_ino))
    return _tree_hash(workspace), inodes


def compare_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Reject contaminated, unpaired, or state-sharing treatments."""
    _scan_controlled(left)
    _scan_controlled(right)
    li, ri = left.get("pair_identity"), right.get("pair_identity")
    if not isinstance(li, Mapping) or not isinstance(ri, Mapping):
        raise EvaluationError("PAIR_IDENTITY_MISSING", "pair_identity")
    for key in PAIR_BINDINGS:
        if li.get(key) != ri.get(key):
            raise EvaluationError("PAIR_BINDING_MISMATCH", key)
    for key in ("pair_id", "task_id", "repeat", "seed"):
        if left.get(key) != right.get(key):
            raise EvaluationError("UNPAIRED_RESULT", key)
    if left.get("treatment") == right.get("treatment"):
        raise EvaluationError("UNPAIRED_RESULT", "treatments must differ")
    for run in (left, right):
        if "score" in run:
            raise EvaluationError("CALLER_SCORE_FORBIDDEN", str(run.get("treatment")))
        _validate_contamination_receipt(run)
        if "semantic_judgment" not in run:
            raise EvaluationError(
                "SEMANTIC_JUDGE_STATUS_MISSING", str(run.get("treatment"))
            )
        validate_semantic_judgment(run["semantic_judgment"])
        isolation = run.get("isolation")
        if not isinstance(isolation, Mapping) or set(isolation) != set(ISOLATION_KEYS):
            raise EvaluationError("ISOLATION_INCOMPLETE", str(run.get("treatment")))
        if len(set(isolation.values())) != len(ISOLATION_KEYS):
            raise EvaluationError("ISOLATION_ALIAS", str(run.get("treatment")))
    overlap = set(left["isolation"].values()) & set(right["isolation"].values())
    path_keys = {"workspace", "home", "skill_registry", "run_root", "cache"}
    left_paths = [Path(str(left["isolation"][key])).resolve() for key in path_keys]
    right_paths = [Path(str(right["isolation"][key])).resolve() for key in path_keys]
    nested = next(
        (
            f"{left_path}:{right_path}"
            for left_path in left_paths
            for right_path in right_paths
            if left_path == right_path
            or left_path in right_path.parents
            or right_path in left_path.parents
        ),
        None,
    )
    if overlap or nested:
        detail = sorted(overlap)[0] if overlap else nested
        raise EvaluationError("CROSS_TREATMENT_STATE_ALIAS", str(detail))
    left_prestate, left_inodes = _physical_isolation(left)
    right_prestate, right_inodes = _physical_isolation(right)
    if left_inodes & right_inodes:
        raise EvaluationError(
            "CROSS_TREATMENT_INODE_ALIAS", str(left_inodes & right_inodes)
        )
    if (
        left_prestate != li["prestate_sha256"]
        or right_prestate != ri["prestate_sha256"]
    ):
        raise EvaluationError("PRESTATE_HASH_MISMATCH", str(left["pair_id"]))
    if left_prestate != right_prestate:
        raise EvaluationError("PRESTATE_MISMATCH", str(left["pair_id"]))
    if not isinstance(left.get("run"), Mapping) or not isinstance(
        right.get("run"), Mapping
    ):
        raise EvaluationError("RUN_RECEIPT_MISSING", str(left["pair_id"]))
    for entry in (left, right):
        if entry["run"].get("treatment") != entry["treatment"]:
            raise EvaluationError("RUN_TREATMENT_MISMATCH", str(entry["treatment"]))
        if entry["run"].get("task_id") != entry["task_id"]:
            raise EvaluationError("RUN_TASK_MISMATCH", str(entry["task_id"]))
    left_score = score_run(left["run"])
    right_score = score_run(right["run"])
    left_metrics = {
        metric["name"]: metric["value_millionths"]
        for metric in left_score["aggregate_metrics"]
    }
    right_metrics = {
        metric["name"]: metric["value_millionths"]
        for metric in right_score["aggregate_metrics"]
    }
    deltas: dict[str, int | None] = {}
    for name in sorted(set(left_metrics) | set(right_metrics)):
        a, b = left_metrics.get(name), right_metrics.get(name)
        deltas[name] = None if a is None or b is None else b - a
    receipt = {
        "schema_version": "beads.pair-receipt.v1",
        "pair_id": left["pair_id"],
        "left_treatment": left["treatment"],
        "right_treatment": right["treatment"],
        "prestate_sha256": left_prestate,
        "pair_binding_sha256": digest({key: li[key] for key in PAIR_BINDINGS}),
        "left_input": dict(left),
        "right_input": dict(right),
        "left_result": left_score,
        "right_result": right_score,
        "deltas_millionths": deltas,
        "status": "valid",
    }
    receipt["pair_receipt_sha256"] = digest(receipt)
    return receipt


def _wilson_lower(successes: int, total: int) -> int | None:
    if total == 0:
        return None
    z = 1.6448536269514722  # one-sided 95%
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0, int(((centre - margin) / denominator) * 1_000_000))


def _stats(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {
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
    ordered = sorted(values)
    count = len(values)
    total = sum(values)
    if count % 2:
        median_numerator, median_denominator = ordered[count // 2], 1
    else:
        median_numerator = ordered[count // 2 - 1] + ordered[count // 2]
        median_denominator = 2
    variance_numerator = count * sum(value * value for value in values) - total**2
    variance_denominator = count**2
    return {
        "count": count,
        "mean": total // count,
        "mean_fraction": {"numerator": total, "denominator": count},
        "median": median_numerator // median_denominator,
        "median_fraction": {
            "numerator": median_numerator,
            "denominator": median_denominator,
        },
        "p95": ordered[max(0, math.ceil(0.95 * count) - 1)],
        "worst": max(values),
        "variance": variance_numerator // variance_denominator,
        "variance_fraction": {
            "numerator": variance_numerator,
            "denominator": variance_denominator,
        },
    }


def _verify_manifest(manifest: Mapping[str, Any]) -> None:
    repo = Path(__file__).resolve().parents[3]
    if (
        manifest.get("verifier_sha256")
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise EvaluationError("VERIFIER_HASH_MISMATCH", str(Path(__file__)))
    for name, contract in manifest["source_contracts"].items():
        path = _repo_path(repo, contract["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != contract["sha256"]:
            raise EvaluationError("SOURCE_HASH_MISMATCH", name)
    if digest(manifest["budget_profiles"]) != manifest["budget_profiles_sha256"]:
        raise EvaluationError("BUDGET_HASH_MISMATCH", "manifest")


def _validate_report(report: Mapping[str, Any], *, with_hash: bool) -> None:
    fields = {
        "schema_version",
        "corpus_sha256",
        "verifier_sha256",
        "source_contracts",
        "budget_profiles_sha256",
        "treatments",
        "excluded_runs",
        "local_gap",
        "lifts",
        "matrix",
        "strata",
        "pair_receipt_sha256",
        "semantic_judgments",
        "efficiency",
        "efficiency_tail_violations",
        "record_count",
    }
    if with_hash:
        fields.add("report_sha256")
    if set(report) != fields:
        raise EvaluationError("REPORT_SCHEMA_INVALID", "top-level fields")
    if report.get("schema_version") != "beads.evaluation-report.v1":
        raise EvaluationError("REPORT_SCHEMA_INVALID", "schema_version")
    if with_hash:
        body = dict(report)
        claimed = body.pop("report_sha256")
        if _require_hash(claimed, "report_sha256") != digest(body):
            raise EvaluationError("REPORT_HASH_MISMATCH", "report_sha256")


def aggregate_results(
    records: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Compute exact denominators, exclusions, macro/micro, tails, lift, provenance."""
    _verify_manifest(manifest)
    _scan_controlled(records)
    scenario_index = {row["task_id"]: row for row in manifest["scenario_index"]}
    by_treatment: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    exclusions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pairs: dict[str, dict[str, str]] = defaultdict(dict)
    pair_receipt_hashes: list[str] = []
    semantic_records: list[dict[str, Any]] = []
    for receipt in records:
        if receipt.get("schema_version") != "beads.pair-receipt.v1":
            raise EvaluationError("PAIR_RECEIPT_REQUIRED", str(receipt.get("pair_id")))
        body = dict(receipt)
        claimed_hash = body.pop("pair_receipt_sha256", None)
        if _require_hash(claimed_hash, "pair_receipt_sha256") != digest(body):
            raise EvaluationError(
                "PAIR_RECEIPT_HASH_MISMATCH", str(receipt.get("pair_id"))
            )
        recomputed = compare_pair(receipt["left_input"], receipt["right_input"])
        if canonical_bytes(recomputed) != canonical_bytes(receipt):
            raise EvaluationError(
                "PAIR_RECEIPT_RECOMPUTE_MISMATCH", str(receipt.get("pair_id"))
            )
        pair_receipt_hashes.append(claimed_hash)
        pair_id = str(receipt["pair_id"])
        for side in ("left", "right"):
            entry = receipt[f"{side}_input"]
            result = receipt[f"{side}_result"]
            treatment = str(entry["treatment"])
            result_hash = digest(result)
            existing = pairs[pair_id].get(treatment)
            if existing is not None and existing != result_hash:
                raise EvaluationError(
                    "DUPLICATE_PAIR_TREATMENT", f"{pair_id}:{treatment}"
                )
            if existing is not None:
                continue
            pairs[pair_id][treatment] = result_hash
            semantic_records.append(
                {
                    "pair_id": pair_id,
                    "treatment": treatment,
                    "judgment": validate_semantic_judgment(entry["semantic_judgment"]),
                }
            )
            task_id = str(entry["task_id"])
            if task_id not in scenario_index:
                raise EvaluationError("UNKNOWN_SCENARIO", task_id)
            by_treatment[treatment].append(
                {
                    "pair_id": pair_id,
                    "task_id": task_id,
                    "family": scenario_index[task_id]["category"],
                    "variant": entry.get("variant", 0),
                    "result": result,
                }
            )
    required_treatments = set(manifest["required_treatments"])
    for pair_id, treatments in pairs.items():
        if set(treatments) != required_treatments:
            raise EvaluationError("UNPAIRED_RESULT", pair_id)
    treatment_reports: dict[str, Any] = {}
    for treatment, rows in sorted(by_treatment.items()):
        metric_families: dict[str, dict[str, list[int]]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0])
        )
        metric_values: dict[str, dict[str, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )
        metric_micro: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        hard_failures = 0
        telemetry: dict[str, list[int]] = defaultdict(list)
        unavailable: dict[str, int] = defaultdict(int)
        for row in rows:
            result = row["result"]
            family = str(row["family"])
            for metric in result["aggregate_metrics"]:
                name = str(metric["name"])
                numerator, denominator = (
                    int(metric["numerator"]),
                    int(metric["denominator"]),
                )
                metric_families[name][family][0] += numerator
                metric_families[name][family][1] += denominator
                metric_micro[name][0] += numerator
                metric_micro[name][1] += denominator
                if denominator:
                    metric_values[name][family].append(
                        numerator * 1_000_000 // denominator
                    )
            hard = next(
                item
                for item in result["aggregate_metrics"]
                if item["name"] == "hard_gate"
            )
            hard_failures += int(hard["numerator"] != hard["denominator"])
            for key, value in result["telemetry"].items():
                if value is None:
                    unavailable[key] += 1
                else:
                    telemetry[key].append(int(value))
        metrics: dict[str, Any] = {}
        for name, families in sorted(metric_families.items()):
            family_rates = {
                family: (
                    None
                    if not metric_values[name][family]
                    else sum(metric_values[name][family])
                    // len(metric_values[name][family])
                )
                for family in sorted(families)
            }
            rates = [rate for rate in family_rates.values() if rate is not None]
            passed, total = metric_micro[name]
            task_values = [
                value for values in metric_values[name].values() for value in values
            ]
            task_successes = sum(value == 1_000_000 for value in task_values)
            variance_numerator = (
                len(task_values) * sum(value * value for value in task_values)
                - sum(task_values) ** 2
            )
            variance_denominator = len(task_values) ** 2 if task_values else 0
            metrics[name] = {
                "micro": _metric(name, passed, total),
                "macro_millionths": None if not rates else sum(rates) // len(rates),
                "family_millionths": family_rates,
                "wilson_lower_millionths": _wilson_lower(
                    task_successes, len(task_values)
                ),
                "task_observations": len(task_values),
                "variance_fraction": {
                    "numerator": variance_numerator,
                    "denominator": variance_denominator,
                },
            }
        compliance = metrics["procedural_compliance"]
        treatment_reports[treatment] = {
            "runs": len(rows),
            "micro": compliance["micro"],
            "macro_millionths": compliance["macro_millionths"],
            "family_millionths": compliance["family_millionths"],
            "wilson_lower_millionths": compliance["wilson_lower_millionths"],
            "metrics": metrics,
            "hard_gate_failures": hard_failures,
            "telemetry": {
                key: _stats(telemetry.get(key, []))
                for key in sorted(
                    {"tokens", "cost_microunits", "latency_milliseconds", "tool_calls"}
                )
            },
            "telemetry_unavailable": dict(sorted(unavailable.items())),
        }
    lifts: dict[str, int | None] = {}
    candidate = treatment_reports.get("candidate_skill")
    for control, label in (("no_skill", "absolute"), ("upstream_skill", "incremental")):
        baseline = treatment_reports.get(control)
        if (
            candidate
            and baseline
            and candidate["macro_millionths"] is not None
            and baseline["macro_millionths"] is not None
        ):
            lifts[f"{label}_compliance_lift_millionths"] = (
                candidate["macro_millionths"] - baseline["macro_millionths"]
            )
        else:
            lifts[f"{label}_compliance_lift_millionths"] = None
    local_ids = set(manifest["local_gap_scenario_ids"])
    pair_rows: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for treatment, rows in by_treatment.items():
        for row in rows:
            pair_rows[str(row["pair_id"])][treatment] = row
    family_deltas: dict[str, list[int]] = defaultdict(list)
    paired_deltas: list[dict[str, Any]] = []
    efficiency_values: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    efficiency_missing: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    tail_violations: list[str] = []
    solo_ids = set(manifest["efficiency_solo_scenario_ids"])
    for pair_id, treatments in sorted(pair_rows.items()):
        candidate_row = treatments.get("candidate_skill")
        if candidate_row and candidate_row["task_id"] in solo_ids:
            for control in ("no_skill", "upstream_skill"):
                control_row = treatments.get(control)
                if not control_row:
                    continue
                for metric_name in (
                    "tokens",
                    "latency_milliseconds",
                    "cost_microunits",
                ):
                    candidate_value = candidate_row["result"]["telemetry"][metric_name]
                    control_value = control_row["result"]["telemetry"][metric_name]
                    key = f"candidate_vs_{control}"
                    if candidate_value is None or control_value is None:
                        efficiency_missing[key][metric_name] += 1
                        continue
                    if control_value == 0:
                        if candidate_value:
                            tail_violations.append(
                                f"{pair_id}:{key}:{metric_name}:zero_baseline"
                            )
                        regression = 0
                    else:
                        regression = (
                            (candidate_value - control_value)
                            * 1_000_000
                            // control_value
                        )
                        if candidate_value >= 2 * control_value:
                            tail_violations.append(f"{pair_id}:{key}:{metric_name}:2x")
                    efficiency_values[key][metric_name].append(regression)
    efficiency = {
        key: {
            "regression_millionths": {
                metric_name: _stats(values)
                for metric_name, values in sorted(metrics.items())
            },
            "missing_telemetry": dict(sorted(efficiency_missing.get(key, {}).items())),
        }
        for key, metrics in sorted(efficiency_values.items())
    }
    for key, missing in efficiency_missing.items():
        efficiency.setdefault(
            key,
            {
                "regression_millionths": {},
                "missing_telemetry": dict(sorted(missing.items())),
            },
        )
    for pair_id, treatments in sorted(pair_rows.items()):
        candidate_row = treatments.get("candidate_skill")
        upstream_row = treatments.get("upstream_skill")
        if (
            not candidate_row
            or not upstream_row
            or candidate_row["task_id"] not in local_ids
        ):
            continue
        values: dict[str, int] = {}
        for name, row in (("candidate", candidate_row), ("upstream", upstream_row)):
            metric = next(
                item
                for item in row["result"]["aggregate_metrics"]
                if item["name"] == "procedural_compliance"
            )
            values[name] = int(metric["value_millionths"] or 0)
        delta = values["candidate"] - values["upstream"]
        family = str(candidate_row["family"])
        family_deltas[family].append(delta)
        paired_deltas.append(
            {"pair_id": pair_id, "family": family, "delta_millionths": delta}
        )
    family_lifts = {
        family: sum(values) // len(values)
        for family, values in sorted(family_deltas.items())
    }
    local_lift = (
        None if not family_lifts else sum(family_lifts.values()) // len(family_lifts)
    )
    lifts["local_gap_incremental_lift_millionths"] = local_lift
    matrix_contract = manifest["release_matrix"]
    stratum_by_identity = {
        (
            value["model"],
            value["provider"],
            value["harness"],
            value["harness_version"],
        ): key
        for key, value in matrix_contract["strata"].items()
    }
    observed_slots: set[str] = set()
    stratum_counts: dict[str, int] = defaultdict(int)
    for treatment, rows in by_treatment.items():
        for row in rows:
            result = row["result"]
            identity = (
                result["model"],
                result["provider"],
                result["harness"],
                result["harness_version"],
            )
            stratum = stratum_by_identity.get(identity)
            if stratum is None:
                raise EvaluationError("UNFROZEN_STRATUM", repr(identity))
            slot = ":".join(
                (
                    str(row["task_id"]),
                    str(result["repeat"]),
                    str(row["variant"]),
                    stratum,
                    treatment,
                )
            )
            if slot in observed_slots:
                raise EvaluationError("DUPLICATE_MATRIX_SLOT", slot)
            observed_slots.add(slot)
            stratum_counts[stratum] += 1
    expected_slots: set[str] = set()
    for task_id in matrix_contract["scenario_ids"]:
        category = scenario_index[task_id]["category"]
        variants = matrix_contract["routing_variants"] if category == "discovery" else 1
        for repeat in range(1, matrix_contract["repeats"] + 1):
            for variant in range(variants):
                for stratum in matrix_contract["strata"]:
                    for treatment in manifest["required_treatments"]:
                        expected_slots.add(
                            f"{task_id}:{repeat}:{variant}:{stratum}:{treatment}"
                        )
    missing_slots = sorted(expected_slots - observed_slots)
    extra_slots = sorted(observed_slots - expected_slots)
    matrix = {
        "expected_slots": len(expected_slots),
        "observed_slots": len(observed_slots),
        "missing_slots": missing_slots,
        "extra_slots": extra_slots,
        "complete": not missing_slots and not extra_slots,
    }
    strata = {
        key: {
            "observed_runs": stratum_counts.get(key, 0),
            "expected_runs": sum(1 for slot in expected_slots if f":{key}:" in slot),
            "sample_complete": stratum_counts.get(key, 0)
            == sum(1 for slot in expected_slots if f":{key}:" in slot),
        }
        for key in matrix_contract["strata"]
    }
    report = {
        "schema_version": "beads.evaluation-report.v1",
        "corpus_sha256": manifest["corpus_sha256"],
        "verifier_sha256": manifest["verifier_sha256"],
        "source_contracts": manifest["source_contracts"],
        "budget_profiles_sha256": manifest["budget_profiles_sha256"],
        "treatments": treatment_reports,
        "excluded_runs": {
            treatment: dict(sorted(reasons.items()))
            for treatment, reasons in sorted(exclusions.items())
        },
        "local_gap": {
            "scenario_ids": sorted(local_ids),
            "family_lifts_millionths": family_lifts,
            "paired_deltas": paired_deltas,
        },
        "lifts": lifts,
        "matrix": matrix,
        "strata": strata,
        "pair_receipt_sha256": sorted(pair_receipt_hashes),
        "semantic_judgments": sorted(
            semantic_records, key=lambda value: (value["pair_id"], value["treatment"])
        ),
        "efficiency": efficiency,
        "efficiency_tail_violations": sorted(tail_violations),
        "record_count": len(records),
    }
    _validate_report(report, with_hash=False)
    report["report_sha256"] = digest(report)
    _validate_report(report, with_hash=True)
    return report


def check_thresholds(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Apply frozen thresholds; aggregate scores cannot mask hard failures.

    Fail-closed chain: the report is only trusted if it can be recomputed
    byte-for-byte from the immutable pair receipts, the manifest provenance
    (verifier/source/budget hashes) verifies, the frozen release matrix is
    nonzero and unblocked, and the manifest status is release-eligible.
    """
    _verify_manifest(manifest)
    _validate_report(report, with_hash=True)
    failures: list[str] = []
    status = manifest.get("status")
    if status != "frozen":
        failures.append(f"manifest_status:{status}")
    matrix_contract = manifest.get("release_matrix") or {}
    blocker = matrix_contract.get("blocker")
    if blocker:
        failures.append("release_matrix:blocker")
    if (
        not matrix_contract.get("scenario_ids")
        or not matrix_contract.get("strata")
        or not (matrix_contract.get("repeats") or 0) >= 1
    ):
        failures.append("release_matrix:empty")
    record_list = list(records)
    if not record_list:
        failures.append("records:empty")
    recomputed = aggregate_results(record_list, manifest)
    if canonical_bytes(recomputed) != canonical_bytes(dict(report)):
        raise EvaluationError("REPORT_RECOMPUTE_MISMATCH", "report")
    thresholds = manifest["thresholds_millionths"]
    missing_treatments = set(manifest["required_treatments"]) - set(
        report["treatments"]
    )
    failures.extend(f"missing_treatment:{name}" for name in sorted(missing_treatments))
    for treatment, values in report["treatments"].items():
        if values["hard_gate_failures"]:
            failures.append(
                f"{treatment}:hard_gate_failures={values['hard_gate_failures']}"
            )
        if treatment == "candidate_skill":
            if (
                values["macro_millionths"] is None
                or values["macro_millionths"]
                < thresholds["procedural_compliance_macro"]
            ):
                failures.append("candidate_skill:procedural_compliance_macro")
            if (
                values["wilson_lower_millionths"] is None
                or values["wilson_lower_millionths"] < thresholds["wilson_lower_bound"]
            ):
                failures.append("candidate_skill:wilson_lower_bound")
    candidate = report["treatments"].get("candidate_skill")
    if candidate:
        required_metrics = {
            "trigger_recall": "trigger_recall_macro",
            "negative_restraint": "negative_restraint_macro",
            "procedural_compliance": "procedural_compliance_macro",
            "routing_path": "routing_path_macro",
            "deterministic_outcome": "deterministic_outcome_macro",
            "critical_lifecycle": "critical_lifecycle",
            "dependency_correctness": "dependency_correctness",
            "recovery": "recovery_fixtures",
        }
        for metric_name, threshold_name in required_metrics.items():
            metric = candidate.get("metrics", {}).get(metric_name)
            threshold = thresholds[threshold_name]
            if (
                metric is None
                or metric["macro_millionths"] is None
                or metric["macro_millionths"] < threshold
            ):
                failures.append(f"candidate_skill:{metric_name}_macro")
                continue
            if metric_name in {
                "trigger_recall",
                "negative_restraint",
                "procedural_compliance",
                "routing_path",
                "deterministic_outcome",
            } and (
                metric["wilson_lower_millionths"] is None
                or metric["wilson_lower_millionths"] < thresholds["wilson_lower_bound"]
            ):
                failures.append(f"candidate_skill:{metric_name}_wilson_lower")
        upstream = report["treatments"].get("upstream_skill")
        if upstream:
            for metric_name in ("deterministic_outcome", "negative_restraint"):
                candidate_value = (
                    candidate.get("metrics", {})
                    .get(metric_name, {})
                    .get("macro_millionths")
                )
                upstream_value = (
                    upstream.get("metrics", {})
                    .get(metric_name, {})
                    .get("macro_millionths")
                )
                if (
                    candidate_value is None
                    or upstream_value is None
                    or candidate_value
                    < upstream_value - thresholds["noninferiority_margin"]
                ):
                    failures.append(f"candidate_skill:{metric_name}_noninferiority")
    matrix = report.get("matrix")
    if not isinstance(matrix, Mapping) or not matrix.get("complete"):
        failures.append("execution_matrix:incomplete")
    strata = report.get("strata")
    if (
        not isinstance(strata, Mapping)
        or not strata
        or any(not value.get("sample_complete", False) for value in strata.values())
    ):
        failures.append("model_harness_strata:incomplete")
    if report.get("efficiency_tail_violations"):
        failures.append("efficiency:per_pair_tail")
    efficiency = report.get("efficiency", {})
    efficiency_gates = (
        (
            "candidate_vs_no_skill",
            "tokens",
            "median",
            "solo_token_regression_vs_no_skill",
        ),
        (
            "candidate_vs_no_skill",
            "latency_milliseconds",
            "median",
            "solo_latency_regression_vs_no_skill",
        ),
        ("candidate_vs_no_skill", "tokens", "p95", "p95_regression"),
        (
            "candidate_vs_no_skill",
            "latency_milliseconds",
            "p95",
            "p95_regression",
        ),
        (
            "candidate_vs_upstream_skill",
            "cost_microunits",
            "median",
            "solo_paid_cost_regression_vs_upstream",
        ),
    )
    for comparison, metric_name, statistic, threshold_name in efficiency_gates:
        value = (
            efficiency.get(comparison, {})
            .get("regression_millionths", {})
            .get(metric_name, {})
            .get(statistic)
        )
        if value is not None and value >= thresholds[threshold_name]:
            failures.append(f"efficiency:{comparison}:{metric_name}:{statistic}")
    lift = report["lifts"].get("local_gap_incremental_lift_millionths")
    if lift is None or lift < thresholds["local_gap_incremental_lift"]:
        failures.append("candidate_skill:local_gap_incremental_lift")
    return {"passed": not failures, "failures": sorted(failures)}


def render_markdown(
    report: Mapping[str, Any], threshold_result: Mapping[str, Any]
) -> str:
    """Render stable Markdown from the canonical aggregate report."""
    lines = [
        "# Beads Skill Evaluation Report",
        "",
        f"- Corpus: `{report['corpus_sha256']}`",
        f"- Verifier: `{report['verifier_sha256']}`",
        f"- Records: {report['record_count']}",
        f"- Release gates: **{'PASS' if threshold_result['passed'] else 'FAIL'}**",
        "",
        "## Treatments",
        "",
        "| Treatment | Runs | Compliance | Wilson lower | Hard failures |",
        "|---|---:|---:|---:|---:|",
    ]

    def percent(value: int | None) -> str:
        return "unavailable" if value is None else f"{value / 10_000:.1f}%"

    for name, values in sorted(report["treatments"].items()):
        lines.append(
            f"| {name} | {values['runs']} | {percent(values['macro_millionths'])} | "
            f"{percent(values['wilson_lower_millionths'])} | "
            f"{values['hard_gate_failures']} |"
        )
    lines.extend(
        [
            "",
            "## Matrix and strata",
            "",
            f"- Expected slots: {report['matrix']['expected_slots']}",
            f"- Observed slots: {report['matrix']['observed_slots']}",
            f"- Missing slots: {len(report['matrix']['missing_slots'])}",
            f"- Extra slots: {len(report['matrix']['extra_slots'])}",
        ]
    )
    for name, values in sorted(report["strata"].items()):
        lines.append(
            f"- {name}: {values['observed_runs']}/{values['expected_runs']} runs; "
            f"complete={str(values['sample_complete']).lower()}"
        )
    lines.extend(["", "## Paired lift and efficiency", ""])
    for name, value in sorted(report["lifts"].items()):
        lines.append(f"- {name}: {percent(value)}")
    lines.append(
        f"- Per-pair tail violations: {len(report['efficiency_tail_violations'])}"
    )
    lines.extend(["", "## Semantic judge status", ""])
    semantic_counts: dict[str, int] = defaultdict(int)
    for record in report["semantic_judgments"]:
        semantic_counts[record["judgment"]["status"]] += 1
    if semantic_counts:
        for status, count in sorted(semantic_counts.items()):
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- No judgments recorded")
    lines.extend(["", "## Exclusions", ""])
    if report["excluded_runs"]:
        for treatment, reasons in sorted(report["excluded_runs"].items()):
            for reason, count in sorted(reasons.items()):
                lines.append(f"- {treatment} / {reason}: {count}")
    else:
        lines.append("- None")
    lines.extend(["", "## Gate failures", ""])
    lines.extend(f"- {failure}" for failure in threshold_result["failures"])
    if not threshold_result["failures"]:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value, newline=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-corpus")
    validate.add_argument("--corpus", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--repo-root", type=Path, required=True)
    score = commands.add_parser("score-run")
    score.add_argument("--input", type=Path, required=True)
    score.add_argument("--output", type=Path)
    pair = commands.add_parser("compare-pair")
    pair.add_argument("--left", type=Path, required=True)
    pair.add_argument("--right", type=Path, required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--records", type=Path, required=True)
    aggregate.add_argument("--manifest", type=Path, required=True)
    aggregate.add_argument("--json-output", type=Path, required=True)
    aggregate.add_argument("--markdown-output", type=Path, required=True)
    checks = commands.add_parser("check-thresholds")
    checks.add_argument("--report", type=Path, required=True)
    checks.add_argument("--manifest", type=Path, required=True)
    checks.add_argument("--records", type=Path, required=True)
    adapt = commands.add_parser("adapt-evidence")
    adapt.add_argument("--worker-result", type=Path, required=True)
    adapt.add_argument("--checkpoint", type=Path, required=True)
    adapt.add_argument("--journal", type=Path)
    adapt.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-corpus":
            value = validate_corpus(
                args.corpus, args.manifest, repo_root=args.repo_root
            )
        elif args.command == "score-run":
            value = score_run(_load(args.input))
            if args.output:
                _write_json(args.output, value)
            hard = next(
                item
                for item in value["aggregate_metrics"]
                if item["name"] == "hard_gate"
            )
            print(json.dumps(value, sort_keys=True))
            return 0 if hard["numerator"] == hard["denominator"] else 3
        elif args.command == "compare-pair":
            value = compare_pair(_load(args.left), _load(args.right))
            hard_failed = any(
                metric["name"] == "hard_gate"
                and metric["numerator"] != metric["denominator"]
                for side in ("left_result", "right_result")
                for metric in value[side]["aggregate_metrics"]
            )
            print(json.dumps(value, sort_keys=True))
            return 3 if hard_failed else 0
        elif args.command == "aggregate":
            manifest = _load(args.manifest)
            records = _load(args.records)
            value = aggregate_results(records, manifest)
            threshold_result = check_thresholds(value, manifest, records)
            _write_json(args.json_output, value)
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(
                render_markdown(value, threshold_result), encoding="utf-8"
            )
            print(json.dumps(threshold_result, sort_keys=True))
            return 0 if threshold_result["passed"] else 3
        elif args.command == "adapt-evidence":
            journal = _load(args.journal) if args.journal else []
            if not isinstance(journal, list):
                raise EvaluationError("EVIDENCE_JOURNAL_INVALID", "journal")
            value = _adapt_runtime_evidence(
                _load(args.worker_result), _load(args.checkpoint), journal
            )
            _write_json(args.output, value)
            print(json.dumps(value, sort_keys=True))
            return 0
        else:
            value = check_thresholds(
                _load(args.report), _load(args.manifest), _load(args.records)
            )
            print(json.dumps(value, sort_keys=True))
            return 0 if value["passed"] else 3
        print(json.dumps(value, sort_keys=True))
        return 0
    except (EvaluationError, KeyError, TypeError) as exc:
        print(
            json.dumps({"error": str(exc), "status": "invalid"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
