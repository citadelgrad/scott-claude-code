from __future__ import annotations

import importlib.util
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "skills/beads/scripts/operation_result.py"


def _load():
    spec = importlib.util.spec_from_file_location("beads_operation_result", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(tmp_path: Path) -> dict:
    return {
        "schema_version": "beads.operation-result.v1",
        "operation": "observe",
        "status": "success",
        "request_id": "request-00000001",
        "issue_ids": [],
        "root_issue_id": None,
        "workspace": {
            "repository_root": str(tmp_path),
            "workspace_sha256": "a" * 64,
            "cli_version": "1.2.2",
        },
        "observed_changes": [],
        "authority": {
            "requested": ["read"],
            "exercised": ["read"],
            "readback_proven": ["read"],
        },
        "native_events": [],
        "verification": {
            "required": False,
            "disposition": "not_required",
            "target_sha256": None,
            "observed_at": None,
            "evidence": [],
        },
        "creation": None,
        "pending_actions": [],
        "blockers": [],
        "warnings": [],
        "coverage_gaps": [],
        "errors": [],
        "error_code": None,
        "safe_next_action": None,
        "run_id": None,
        "checkpoint": None,
        "cancellation_reason": None,
    }


def test_validated_success_renders_only_supported_claim(tmp_path: Path) -> None:
    operation = _load()
    value = _result(tmp_path)
    result = operation.validate_operation_result(value)
    rendered = operation.render_human(result)
    assert rendered == "Observed 0 issue(s); readback verified."
    assert not any(
        word in rendered.casefold()
        for word in ("created", "closed", "pushed", "synced")
    )


@pytest.mark.parametrize(
    ("classification", "exit_code"), [("native_error", 1), ("success", None)]
)
def test_contradiction_cannot_build_or_render_success(
    tmp_path: Path, classification: str, exit_code: int | None
) -> None:
    operation = _load()
    value = _result(tmp_path)
    value["native_events"] = [
        {
            "schema_version": "beads.native-command-event.v1",
            "request_id": "request-00000001",
            "profile": "issue_get",
            "argv_descriptor_sha256": "b" * 64,
            "classification": classification,
            "exit_code": exit_code,
            "target_fingerprint_sha256": None,
            "evidence_path": None,
            "evidence_sha256": None,
        }
    ]
    built = operation.build_operation_result(operation.OperationBuildInput(value))
    assert built.value["status"] == "inconclusive"
    assert built.value["error_code"] == "NATIVE_EVIDENCE_INCOMPLETE"
    assert "success" not in operation.render_human(built).casefold()


def test_schema_rejects_non_success_without_safe_action(tmp_path: Path) -> None:
    operation = _load()
    value = _result(tmp_path)
    value["status"] = "failed"
    value["errors"] = [
        {"code": "X", "template_id": "x", "field_path": "/", "parameters": []}
    ]
    with pytest.raises(operation.OperationResultError):
        operation.validate_operation_result(value)


def test_required_verification_needs_matching_evidence(tmp_path: Path) -> None:
    operation = _load()
    value = _result(tmp_path)
    value["verification"].update(required=True, disposition="passed")
    with pytest.raises(
        operation.OperationResultError, match="VERIFICATION_EVIDENCE_MISSING"
    ):
        operation.validate_operation_result(value)

    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b"{}\n")
    value["verification"]["evidence"] = [
        {
            "type": "test",
            "path": str(evidence),
            "size_bytes": 999,
            "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        }
    ]
    with pytest.raises(operation.OperationResultError, match="EVIDENCE_SIZE_MISMATCH"):
        operation.validate_operation_result(value)


@pytest.mark.parametrize("status", ["success", "partial"])
def test_create_operations_require_creation_accounting(
    tmp_path: Path, status: str
) -> None:
    operation = _load()
    value = _result(tmp_path)
    value["operation"] = "create_graph"
    value["status"] = status
    if status == "partial":
        value["error_code"] = "PARTIAL"
        value["safe_next_action"] = {
            "code": "RETRY",
            "template_id": "retry",
            "field_path": "/",
            "parameters": [],
        }
    with pytest.raises(
        operation.OperationResultError, match="CREATION_ACCOUNTING_MISSING"
    ):
        operation.validate_operation_result(value)


def test_validation_rejects_attacker_controlled_codes_before_persistence(
    tmp_path: Path,
) -> None:
    operation = _load()
    value = _result(tmp_path)
    value.update(
        status="failed",
        error_code="X)\nCreated 99 issues",
        safe_next_action={
            "code": "RETRY\nSynced remote",
            "template_id": "retry",
            "field_path": "/",
            "parameters": [],
        },
        errors=[
            {
                "code": "X",
                "template_id": "x",
                "field_path": "/",
                "parameters": [],
            }
        ],
    )
    with pytest.raises(operation.OperationResultError, match="UNSAFE_RESULT_CODE"):
        operation.validate_operation_result(value)


def test_build_redacts_configured_secrets_before_persistence(tmp_path: Path) -> None:
    operation = _load()
    value = _result(tmp_path)
    sentinel = "redaction-probe-value-0123456789"
    value.update(
        status="failed",
        error_code="FAILED",
        safe_next_action={
            "code": "RETRY",
            "template_id": "retry",
            "field_path": "/",
            "parameters": [],
        },
        errors=[
            {
                "code": "FAILED",
                "template_id": "failed",
                "field_path": "/",
                "parameters": [sentinel],
            }
        ],
    )

    result = operation.build_operation_result(
        operation.OperationBuildInput(
            value, operation.safe_output.SensitiveSet((sentinel,))
        )
    )

    assert sentinel not in repr(result.value)
    assert result.value["errors"][0]["parameters"] == ["[REDACTED]"]


def test_sensitive_descriptor_must_be_canonical_and_owner_only(tmp_path: Path) -> None:
    operation = _load()
    descriptor = tmp_path / "sensitive.json"
    descriptor.write_text('["redaction-probe-value-0123456789"]')
    descriptor.chmod(0o600)

    loaded = operation._load_sensitive_values(descriptor)

    assert loaded.values == ("redaction-probe-value-0123456789",)
    descriptor.chmod(0o644)
    with pytest.raises(
        operation.OperationResultError, match="SENSITIVE_DESCRIPTOR_INVALID"
    ):
        operation._load_sensitive_values(descriptor)
