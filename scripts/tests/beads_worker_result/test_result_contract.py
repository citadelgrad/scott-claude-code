from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FINALIZE_TEST = ROOT / "scripts/tests/beads_worker_result/test_finalize.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **mode: str):
    fixtures = _load(FINALIZE_TEST, f"result_contract_fixtures_{tmp_path.name}")
    worker, context, candidate = fixtures._setup(tmp_path, monkeypatch, **mode)
    descriptor = fixtures._descriptor(tmp_path, [])
    return fixtures, worker, context, candidate, descriptor


@pytest.mark.parametrize(
    "status", ["completed", "blocked", "failed", "inconclusive", "cancelled"]
)
def test_all_terminal_status_variants_enforce_their_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _, worker, context, candidate, descriptor = _setup(tmp_path, monkeypatch)
    candidate["status"] = status
    if status == "blocked":
        candidate["blockers"] = ["dependency unavailable"]
    elif status == "failed":
        candidate["errors"] = [
            {
                "code": "CHECK_FAILED",
                "template_id": "check_failed",
                "field_path": "/verification/0",
                "parameters": [],
            }
        ]
    elif status == "inconclusive":
        candidate["residual_risks"] = ["identity could not be established"]
    elif status == "cancelled":
        candidate["cancellation_reason"] = "parent cancelled"

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=descriptor
    )
    assert receipt["status"] == status


@pytest.mark.parametrize(
    ("status", "field", "empty"),
    [
        ("blocked", "blockers", []),
        ("failed", "errors", []),
        ("inconclusive", "residual_risks", []),
        ("cancelled", "cancellation_reason", None),
    ],
)
def test_terminal_status_without_required_evidence_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    field: str,
    empty: object,
) -> None:
    _, worker, context, candidate, descriptor = _setup(tmp_path, monkeypatch)
    candidate["status"] = status
    candidate[field] = empty

    with pytest.raises(worker.WorkerResultError, match="RESULT_SCHEMA_INVALID"):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)


@pytest.mark.parametrize(
    ("status", "evidence"),
    [
        (
            "failed",
            {
                "errors": [
                    {
                        "code": "CHECK_FAILED",
                        "template_id": "check_failed",
                        "field_path": "/verification/0",
                        "parameters": [],
                    }
                ]
            },
        ),
        ("blocked", {"blockers": ["dependency unavailable"]}),
        ("cancelled", {"cancellation_reason": "parent cancelled"}),
    ],
)
@pytest.mark.parametrize("mode", ["commit", "external_export"])
def test_terminal_failure_may_be_artifact_less(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    evidence: dict,
    mode: str,
) -> None:
    fixtures, worker, context, candidate, descriptor = _setup(
        tmp_path, monkeypatch, integration_mode=mode
    )
    candidate["status"] = status
    candidate.update(evidence)
    candidate["worker_frozen_artifact"] = None

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=descriptor
    )

    assert receipt["status"] == status


def test_completed_commit_without_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, worker, context, candidate, descriptor = _setup(
        tmp_path, monkeypatch, integration_mode="commit"
    )
    candidate["worker_frozen_artifact"] = None

    with pytest.raises(worker.WorkerResultError, match="FROZEN_ARTIFACT_MISSING"):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)


def test_scope_declaration_is_exact_and_completed_cannot_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, worker, context, candidate, descriptor = _setup(tmp_path, monkeypatch)
    candidate["changes"] = {"paths": ["outside/file.py"], "outside_allowed_scope": []}
    with pytest.raises(worker.WorkerResultError, match="SCOPE_DECLARATION_MISMATCH"):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)

    candidate["changes"]["outside_allowed_scope"] = ["outside/file.py"]
    with pytest.raises(
        worker.WorkerResultError,
        match="RESULT_SCHEMA_INVALID|COMPLETED_OUTSIDE_SCOPE",
    ):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)


def test_artifact_size_hash_alias_and_unregistered_log_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, worker, context, candidate, descriptor = _setup(tmp_path, monkeypatch)
    wrong = copy.deepcopy(candidate)
    wrong["artifacts"][0]["size_bytes"] += 1
    with pytest.raises(worker.WorkerResultError, match="ARTIFACT_SIZE_MISMATCH"):
        worker.finalize_attempt(context, wrong, sensitive_values_file=descriptor)

    wrong = copy.deepcopy(candidate)
    wrong["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(worker.WorkerResultError, match="ARTIFACT_HASH_MISMATCH"):
        worker.finalize_attempt(context, wrong, sensitive_values_file=descriptor)

    rogue = context.outbox / "prewritten.log"
    rogue.write_text("uncontrolled")
    rogue.chmod(0o600)
    wrong = copy.deepcopy(candidate)
    wrong["artifacts"].append(fixtures._identity(rogue, "test_log"))
    with pytest.raises(
        worker.WorkerResultError, match="UNREGISTERED_ARTIFACT|OUTBOX_UNDECLARED_ENTRY"
    ):
        worker.finalize_attempt(context, wrong, sensitive_values_file=descriptor)
    rogue.unlink()

    target = tmp_path / "outside.log"
    target.write_text("safe")
    target.chmod(0o600)
    alias = context.outbox / "alias.log"
    alias.symlink_to(target)
    wrong = copy.deepcopy(candidate)
    wrong["artifacts"][1] = {
        "type": "stdout_log",
        "path": str(alias),
        "size_bytes": 4,
        "sha256": hashlib.sha256(b"safe").hexdigest(),
    }
    with pytest.raises(worker.WorkerResultError, match="ARTIFACT_PATH_INVALID"):
        worker.finalize_attempt(context, wrong, sensitive_values_file=descriptor)


def test_result_and_receipt_redact_configured_values_and_enforce_byte_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, worker, context, candidate, _ = _setup(tmp_path, monkeypatch)
    sentinel = "synthetic-result-secret-0123456789"
    candidate["summary"] = sentinel
    descriptor = fixtures._descriptor(tmp_path, [sentinel])

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=descriptor
    )
    assert receipt["summary"] == "[REDACTED]"
    assert sentinel.encode() not in (context.outbox / "result.json").read_bytes()
    assert sentinel.encode() not in (context.outbox / "receipt.json").read_bytes()


def test_oversized_manifest_is_refused_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, worker, context, candidate, descriptor = _setup(tmp_path, monkeypatch)
    candidate["changes"]["paths"] = [
        f"src/{index:04d}-{'x' * 32}.py" for index in range(2000)
    ]
    candidate["changes"]["outside_allowed_scope"] = []

    with pytest.raises(worker.WorkerResultError, match="RESULT_TOO_LARGE"):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)
    assert not (context.outbox / "result.json").exists()
    assert not (context.outbox / "receipt.json").exists()


def test_identity_and_required_command_evidence_cannot_be_forged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, worker, context, candidate, descriptor = _setup(tmp_path, monkeypatch)
    wrong = copy.deepcopy(candidate)
    wrong["attempt_id"] = "attempt-002"
    with pytest.raises(worker.WorkerResultError, match="RESULT_IDENTITY_MISMATCH"):
        worker.finalize_attempt(context, wrong, sensitive_values_file=descriptor)

    wrong = copy.deepcopy(candidate)
    wrong["verification"] = []
    with pytest.raises(worker.WorkerResultError, match="COMMAND_VERIFICATION_MISSING"):
        worker.finalize_attempt(context, wrong, sensitive_values_file=descriptor)
