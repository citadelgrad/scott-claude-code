"""Versioned adapter from runtime worker/coordinator artifacts to evaluator evidence.

The evaluator never accepts caller-authored lifecycle labels, result classes,
or outcome hashes. This module is the only sanctioned translation from
schema-valid worker execution results, coordinator run checkpoints, and
operation journals into the typed evidence bundle ``score_run`` consumes.
It is versioned (``beads.evidence-adapter.v1``) so runtime schema evolution
on parallel lanes cannot silently change evaluator semantics: only the exact
source schema versions listed here are adapted, and no future runtime fields
are guessed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ADAPTER_VERSION = "beads.evidence-adapter.v1"
EVIDENCE_SCHEMA_VERSION = "beads.evaluator-evidence.v1"
MAX_JOURNAL_EVENTS = 256

SOURCE_SCHEMA_VERSIONS = {
    "worker_execution_result": (
        "beads.worker-execution-result.v1",
        "worker-execution-result-v1.schema.json",
    ),
    "run_checkpoint": ("beads.run-checkpoint.v1", "run-checkpoint-v1.schema.json"),
    "operation_journal_event": (
        "beads.operation-journal-event.v1",
        "operation-journal-event-v1.schema.json",
    ),
}

EFFECT_EVENT_MAP = {
    "skill_load": "skill_load",
    "tool_identity": "tool_identity",
    "version_check": "version_check",
    "workspace_where": "workspace_where",
    "prime": "prime",
    "bd_show": "issue_view",
    "bd_update_status_claimed": "claim",
    "bd_claim_readback": "claim_readback",
    "bd_update_status_closed": "close",
    "state_readback": "state_readback",
    "reconcile": "reconcile",
}


def _load_module(name: str, path: Path):
    if path.name in sys.modules:  # pragma: no cover - defensive
        return sys.modules[path.name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_EVALUATOR = sys.modules.get("beads_skill_evaluator")
if _EVALUATOR is None:
    _EVALUATOR = _load_module(
        "beads_skill_evaluator",
        Path(__file__).resolve().parent / "evaluator.py",
    )
EvaluationError = _EVALUATOR.EvaluationError
digest = _EVALUATOR.digest

_SCHEMA_RUNTIME = _load_module(
    "beads_evidence_schema_runtime",
    Path(__file__).resolve().parents[3] / "skills/beads/scripts/schema_runtime.py",
)


def _require_source(kind: str, value: Any, name: str) -> Mapping[str, Any]:
    expected_version, schema_name = SOURCE_SCHEMA_VERSIONS[kind]
    if not isinstance(value, Mapping):
        raise EvaluationError("EVIDENCE_SOURCE_INVALID", name)
    if value.get("schema_version") != expected_version:
        raise EvaluationError(
            "EVIDENCE_SOURCE_VERSION_UNSUPPORTED",
            f"{name}:{value.get('schema_version')}",
        )
    try:
        _SCHEMA_RUNTIME.require_valid(schema_name, value)
    except Exception as exc:
        raise EvaluationError("EVIDENCE_SOURCE_SCHEMA_INVALID", name) from exc
    return value


def _journal_events(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    effect = entry["effect_type"]
    target = (
        entry.get("issue_id")
        if isinstance(entry.get("issue_id"), str)
        else entry["operation_id"]
    )
    mapped = EFFECT_EVENT_MAP.get(effect)
    phase = entry["phase"]
    if phase == "RESOLUTION":
        applied = entry.get("status") == "APPLIED"
        if mapped is None:
            return [
                {
                    "event_type": "operation_result",
                    "target_identity": target,
                    "result_class": "schema_valid" if applied else "failed",
                }
            ]
        return [
            {
                "event_type": mapped,
                "target_identity": target,
                "result_class": "passed" if applied else "failed",
            }
        ]
    if mapped is None:
        return [
            {
                "event_type": "operation_result",
                "target_identity": target,
                "result_class": "schema_valid",
            }
        ]
    return [
        {
            "event_type": mapped,
            "target_identity": target,
            "result_class": "passed",
        }
    ]


def adapt_runtime_evidence(
    worker_execution_result: Any,
    run_checkpoint: Any,
    operation_journal: Sequence[Any] = (),
) -> dict[str, Any]:
    """Derive typed evaluator evidence from schema-valid runtime artifacts.

    Every emitted event and outcome check is recomputed from the supplied
    artifacts; callers cannot inject labels, classes, or hashes. The returned
    bundle is closed and hash-sealed with ``evidence_sha256``.
    """
    worker = _require_source(
        "worker_execution_result", worker_execution_result, "worker_execution_result"
    )
    checkpoint = _require_source("run_checkpoint", run_checkpoint, "run_checkpoint")
    journal_list = list(operation_journal)
    if len(journal_list) > MAX_JOURNAL_EVENTS:
        raise EvaluationError("EVIDENCE_JOURNAL_BUDGET", str(len(journal_list)))
    journal = [
        _require_source("operation_journal_event", entry, f"operation_journal[{index}]")
        for index, entry in enumerate(journal_list)
    ]
    if worker["run_id"] != checkpoint["run_id"]:
        raise EvaluationError("EVIDENCE_IDENTITY_MISMATCH", "run_id")
    if worker["issue_id"] != checkpoint["root_issue_id"]:
        raise EvaluationError("EVIDENCE_IDENTITY_MISMATCH", "issue_id")
    for index, entry in enumerate(journal):
        if entry["run_id"] != worker["run_id"]:
            raise EvaluationError(
                "EVIDENCE_IDENTITY_MISMATCH", f"operation_journal[{index}].run_id"
            )
        if (
            isinstance(entry.get("issue_id"), str)
            and entry["issue_id"] != worker["issue_id"]
        ):
            raise EvaluationError(
                "EVIDENCE_IDENTITY_MISMATCH", f"operation_journal[{index}].issue_id"
            )
    events: list[dict[str, Any]] = []
    for entry in journal:
        events.extend(_journal_events(entry))
    changes = worker["changes"]
    for path in sorted(changes["paths"]):
        events.append(
            {
                "event_type": "code_mutation",
                "target_identity": path,
                "result_class": "passed",
            }
        )
    if changes["outside_allowed_scope"]:
        events.append(
            {
                "event_type": "hard_gate_violation",
                "target_identity": changes["outside_allowed_scope"][0],
                "result_class": "failed",
            }
        )
    for record in worker["verification"]:
        events.append(
            {
                "event_type": (
                    "verification_pass"
                    if record["exit_code"] == 0
                    else "verification_failure"
                ),
                "target_identity": record["log_path"],
                "result_class": "passed" if record["exit_code"] == 0 else "failed",
            }
        )
    exit_codes = [int(record["exit_code"]) for record in worker["verification"]]
    outcome_checks = [
        {
            "check_id": "worker_status",
            "expected_sha256": digest("completed"),
            "observed_sha256": digest(worker["status"]),
        },
        {
            "check_id": "verification_exit_codes",
            "expected_sha256": digest([0] * len(exit_codes)),
            "observed_sha256": digest(exit_codes),
        },
    ]
    bundle = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "run_id": worker["run_id"],
        "worker_execution_result_sha256": digest(worker),
        "run_checkpoint_sha256": digest(checkpoint),
        "operation_journal_sha256": digest(journal),
        "events": events,
        "outcome_checks": outcome_checks,
    }
    bundle["evidence_sha256"] = digest(bundle)
    return bundle
