#!/usr/bin/env python3
"""Canonical operation-result validation, contradiction handling, and rendering."""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parent))
import schema_runtime
import safe_output

SUCCESS_VERBS = frozenset({"created", "completed", "closed", "pushed", "synced"})
_RENDER_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class OperationResultError(ValueError):
    pass


@dataclass(frozen=True)
class OperationResult:
    value: dict[str, Any]


@dataclass(frozen=True)
class OperationBuildInput:
    candidate: Mapping[str, Any]
    sensitive: safe_output.SensitiveSet = safe_output.SensitiveSet(())


def _diagnostic(code: str, template: str, field: str = "/") -> dict[str, Any]:
    return {
        "code": code,
        "template_id": template,
        "field_path": field,
        "parameters": [],
    }


def _verify_file(
    path: str, expected: str, expected_size: int | None = None
) -> str | None:
    target = Path(path)
    if not target.is_absolute() or target.is_symlink():
        return "EVIDENCE_PATH_INVALID"
    try:
        if target.resolve(strict=True) != target or not target.is_file():
            return "EVIDENCE_PATH_INVALID"
        if expected_size is not None and target.stat().st_size != expected_size:
            return "EVIDENCE_SIZE_MISMATCH"
        with target.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return "EVIDENCE_PATH_INVALID"
    return None if hmac.compare_digest(actual, expected) else "EVIDENCE_HASH_MISMATCH"


def _semantic_contradiction(value: Mapping[str, Any]) -> str | None:
    status = value.get("status")
    events = value.get("native_events", [])
    if status == "success" and any(
        event.get("classification") != "success" or event.get("exit_code") != 0
        for event in events
    ):
        return "NATIVE_EVIDENCE_INCOMPLETE"
    authority = value.get("authority", {})
    requested = set(authority.get("requested", []))
    exercised = set(authority.get("exercised", []))
    proven = set(authority.get("readback_proven", []))
    if not exercised <= requested:
        return "AUTHORITY_CONTRADICTION"
    if status == "success" and not exercised <= proven:
        return "READBACK_MISSING"
    if value.get("operation") == "observe" and exercised - {"read"}:
        return "OBSERVE_MUTATION_CONTRADICTION"
    verification = value.get("verification", {})
    if (
        status == "success"
        and verification.get("required")
        and verification.get("disposition") != "passed"
    ):
        return "VERIFICATION_INCOMPLETE"
    if (
        status == "success"
        and verification.get("required")
        and not verification.get("evidence")
    ):
        return "VERIFICATION_EVIDENCE_MISSING"
    creation = value.get("creation")
    if (
        value.get("operation") in {"create_issue", "create_graph"}
        and status in {"success", "partial"}
        and creation is None
    ):
        return "CREATION_ACCOUNTING_MISSING"
    if status == "success" and creation is not None:
        if set(creation.get("requested_ids", [])) != set(
            creation.get("created_ids", [])
        ):
            return "CREATE_READBACK_MISMATCH"
        if not creation.get("field_comparisons_match") or creation.get(
            "dependency_edges_requested"
        ) != creation.get("dependency_edges_observed"):
            return "CREATE_READBACK_MISMATCH"
        if creation.get("lint_errors") or creation.get("cycle_result") != "acyclic":
            return "CREATE_VALIDATION_FAILED"
    if status == "success" and (
        value.get("errors")
        or value.get("blockers")
        or value.get("coverage_gaps")
        or value.get("pending_actions")
    ):
        return "SUCCESS_HAS_UNRESOLVED_EVIDENCE"
    return None


def _validate_evidence(value: Mapping[str, Any]) -> str | None:
    identities: list[tuple[str | None, str | None, int | None]] = []
    for change in value.get("observed_changes", []):
        identities.append(
            (
                change.get("readback_evidence_path"),
                change.get("readback_evidence_sha256"),
                None,
            )
        )
    for evidence in value.get("verification", {}).get("evidence", []):
        identities.append(
            (evidence.get("path"), evidence.get("sha256"), evidence.get("size_bytes"))
        )
    for path, digest, size in identities:
        if (path is None) != (digest is None):
            return "EVIDENCE_IDENTITY_INCOMPLETE"
        if path is not None:
            assert digest is not None
            failure = _verify_file(path, digest, size)
            if failure is not None:
                return failure
    return None


def _sanitize_tree(value: Any, sensitive: safe_output.SensitiveSet) -> Any:
    if isinstance(value, str):
        sanitized = safe_output.sanitize_value(
            value, sensitive=sensitive, max_utf8_bytes=4096
        )
        if sanitized.value is None:
            raise OperationResultError("RESULT_REDACTION_FAILED")
        return sanitized.value
    if isinstance(value, list):
        return [_sanitize_tree(item, sensitive) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OperationResultError("RESULT_REDACTION_FAILED")
            clean[key] = (
                "[REDACTED]"
                if safe_output.is_sensitive_label(key)
                else _sanitize_tree(item, sensitive)
            )
        return clean
    return value


def _validate_stable_codes(value: Mapping[str, Any]) -> None:
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"code", "error_code"} and child is not None:
                    if not isinstance(child, str) or not _RENDER_CODE.fullmatch(child):
                        raise OperationResultError("UNSAFE_RESULT_CODE")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)


def _load_sensitive_values(path: Path) -> safe_output.SensitiveSet:
    if not path.is_absolute() or path.is_symlink():
        raise OperationResultError("SENSITIVE_DESCRIPTOR_INVALID")
    try:
        if path.resolve(strict=True) != path or not path.is_file():
            raise OperationResultError("SENSITIVE_DESCRIPTOR_INVALID")
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise OperationResultError("SENSITIVE_DESCRIPTOR_INVALID") from None
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OperationResultError("SENSITIVE_DESCRIPTOR_INVALID")
    value = schema_runtime.strict_json_loads(
        schema_runtime.read_bounded(path, 65536), max_bytes=65536
    )
    if not isinstance(value, list) or not all(
        isinstance(item, str) and len(item.encode("utf-8")) >= 8 for item in value
    ):
        raise OperationResultError("SENSITIVE_DESCRIPTOR_INVALID")
    return safe_output.SensitiveSet(tuple(value))


def validate_operation_result(
    value: Mapping[str, Any],
    *,
    sensitive: safe_output.SensitiveSet = safe_output.SensitiveSet(()),
) -> OperationResult:
    candidate = _sanitize_tree(copy.deepcopy(dict(value)), sensitive)
    _validate_stable_codes(candidate)
    findings = schema_runtime.validate_instance(
        "operation-result-v1.schema.json", candidate
    )
    if findings:
        raise OperationResultError(
            f"SCHEMA_INVALID:{findings[0].code}:{findings[0].instance_path}"
        )
    contradiction = _semantic_contradiction(candidate) or _validate_evidence(candidate)
    if contradiction:
        raise OperationResultError(contradiction)
    return OperationResult(candidate)


def build_operation_result(inp: OperationBuildInput) -> OperationResult:
    value = _sanitize_tree(copy.deepcopy(dict(inp.candidate)), inp.sensitive)
    contradiction = _semantic_contradiction(value) or _validate_evidence(value)
    if contradiction:
        value["status"] = (
            "conflict"
            if contradiction
            in {
                "AUTHORITY_CONTRADICTION",
                "OBSERVE_MUTATION_CONTRADICTION",
                "CREATE_READBACK_MISMATCH",
                "EVIDENCE_HASH_MISMATCH",
            }
            else "inconclusive"
        )
        if value["status"] == "inconclusive" and not value.get("coverage_gaps"):
            value["coverage_gaps"] = [_diagnostic(contradiction, "evidence_incomplete")]
        if value["status"] == "conflict" and not value.get("errors"):
            value["errors"] = [_diagnostic(contradiction, "evidence_conflict")]
        value["error_code"] = contradiction
        value["safe_next_action"] = _diagnostic(
            "RECONCILE_AND_READ_BACK", "reconcile_readback"
        )
    return validate_operation_result(value, sensitive=inp.sensitive)


def render_human(result: OperationResult) -> str:
    value = result.value
    status = value["status"]
    operation = value["operation"]
    if status != "success":
        error_code = value["error_code"]
        next_code = value["safe_next_action"]["code"]
        if not _RENDER_CODE.fullmatch(error_code) or not _RENDER_CODE.fullmatch(
            next_code
        ):
            raise OperationResultError("UNSAFE_RENDER_FIELD")
        return f"{operation}: {status} ({error_code}); next action: {next_code}."
    count = len(value["issue_ids"])
    if operation == "observe":
        text = f"Observed {count} issue(s); readback verified."
    elif operation in {"create_issue", "create_graph"}:
        text = f"Created {len(value['creation']['created_ids'])} issue(s); exact readback verified."
    elif operation == "edit_dependencies":
        text = "Dependency changes applied; exact edge readback verified."
    elif operation in {"execute_one", "execute_set", "finish"}:
        text = f"Operation succeeded for {count} issue(s); readback verified."
    else:
        text = f"{operation}: success; readback verified."
    lowered = text.casefold()
    if any(verb in lowered for verb in SUCCESS_VERBS):
        if (
            operation in {"create_issue", "create_graph"}
            and value.get("creation") is None
        ):
            raise OperationResultError("UNSUPPORTED_SUCCESS_VERB")
        if not set(value["authority"]["exercised"]) <= set(
            value["authority"]["readback_proven"]
        ):
            raise OperationResultError("UNSUPPORTED_SUCCESS_VERB")
    return text


# Plugin-free Hermes inserts arbitrary child summaries before this module can
# sanitize them. This is intentionally a release-blocking limitation.
PRE_MODEL_CHILD_OUTPUT_LIMITATION = "UNPREVENTABLE_PRE_INTERCEPTION_LEAK"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--input", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--sensitive-values-file", type=Path, required=True)
    build.add_argument("--json", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--result", type=Path, required=True)
    validate.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = args.input if args.command == "build" else args.result
        value = schema_runtime.strict_json_loads(
            schema_runtime.read_bounded(path, 65536), max_bytes=65536
        )
        sensitive = (
            _load_sensitive_values(args.sensitive_values_file)
            if args.command == "build"
            else safe_output.SensitiveSet(())
        )
        result = (
            build_operation_result(OperationBuildInput(value, sensitive))
            if args.command == "build"
            else validate_operation_result(value)
        )
        if args.command == "build":
            data = (
                json.dumps(
                    result.value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                + b"\n"
            )
            safe_output.write_new_artifact(args.output, data)
        print(
            json.dumps(
                {"status": result.value["status"], "human": render_human(result)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if result.value["status"] == "success" else 1
    except (OSError, ValueError, TypeError, schema_runtime.JsonLoadFailure):
        print('{"status":"invalid","error_code":"OPERATION_RESULT_INVALID"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
