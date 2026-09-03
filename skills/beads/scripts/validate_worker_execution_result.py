#!/usr/bin/env python3
"""Strict packet-bound validation for worker execution results."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NoReturn

sys.path.insert(0, str(Path(__file__).parent))
import schema_runtime
import validate_worker_packet


class WorkerExecutionResultError(ValueError):
    """The result is not safe to accept or import."""


@dataclass(frozen=True)
class ValidatedWorkerExecutionResult:
    value: dict[str, Any]
    result_sha256: str
    result_size_bytes: int


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _fail(code: str) -> NoReturn:
    raise WorkerExecutionResultError(code)


def _under(path: Path, root: Path) -> bool:
    try:
        return bool(path.relative_to(root).parts)
    except ValueError:
        return False


def _verify_artifact(path: Path, size: int, digest: str, outbox: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not _under(path, outbox):
        _fail("ARTIFACT_PATH_INVALID")
    try:
        if path.resolve(strict=True) != path:
            _fail("ARTIFACT_PATH_INVALID")
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            _fail("ARTIFACT_OWNER_INVALID")
        if metadata.st_size != size:
            _fail("ARTIFACT_SIZE_MISMATCH")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        _fail("ARTIFACT_PATH_INVALID")
    if not hmac.compare_digest(actual, digest):
        _fail("ARTIFACT_HASH_MISMATCH")


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        root = pattern[:-3].rstrip("/")
        return path == root or path.startswith(root + "/")
    return path == pattern or PurePosixPath(path).match(pattern)


def _outside_scope(value: Mapping[str, Any], packet: Mapping[str, Any]) -> list[str]:
    allowed = packet["scope"]["allowed_paths"]
    forbidden = packet["scope"]["forbidden_paths"]
    return sorted(
        path
        for path in value["changes"]["paths"]
        if not any(_matches(path, item) for item in allowed)
        or any(_matches(path, item) for item in forbidden)
    )


def _command_text(argv: tuple[str, ...]) -> str:
    return json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))


def _argv_sha256(argv: tuple[str, ...]) -> str:
    return hashlib.sha256(_command_text(argv).encode("utf-8")).hexdigest()


def _read_command_record(path: Path, max_bytes: int) -> dict[str, Any]:
    try:
        raw = schema_runtime.read_bounded(path, max_bytes)
        value = schema_runtime.strict_json_loads(raw, max_bytes=max_bytes)
    except (OSError, schema_runtime.JsonLoadFailure):
        _fail("COMMAND_EVIDENCE_INVALID")
    if not isinstance(value, dict):
        _fail("COMMAND_EVIDENCE_INVALID")
    if raw != _canonical_json(value):
        _fail("COMMAND_EVIDENCE_NOT_CANONICAL")
    return value


def _validate_commands(
    value: Mapping[str, Any],
    packet: validate_worker_packet.ValidatedWorkerPacket,
    ownership_epoch: int,
    outbox: Path,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    verification = value["verification"]
    if len(verification) > len(packet.required_commands):
        _fail("COMMAND_EVIDENCE_CARDINALITY")
    expected_artifact_paths: set[str] = set()
    attempted: set[int] = set()
    by_log_path = {item["log_path"]: item for item in verification}
    artifact_limit = packet.value["verification"]["max_artifact_bytes"]
    for index, argv in enumerate(packet.required_commands):
        record_path = outbox / f"command-{index:03d}.json"
        if not record_path.exists() and not record_path.is_symlink():
            continue
        record = _read_command_record(record_path, artifact_limit)
        required_keys = {
            "schema_version",
            "run_id",
            "attempt_id",
            "issue_id",
            "packet_sha256",
            "ownership_epoch",
            "command_index",
            "argv",
            "safe_result",
        }
        if set(record) != required_keys or record != {
            "schema_version": "beads.worker-command-evidence.v1",
            "run_id": packet.value["run_id"],
            "attempt_id": packet.value["attempt_id"],
            "issue_id": packet.value["issue"]["id"],
            "packet_sha256": packet.packet_sha256,
            "ownership_epoch": ownership_epoch,
            "command_index": index,
            "argv": list(argv),
            "safe_result": record.get("safe_result"),
        }:
            _fail("COMMAND_EVIDENCE_IDENTITY_MISMATCH")
        findings = schema_runtime.validate_instance(
            "safe-command-result-v1.schema.json", record["safe_result"]
        )
        if findings:
            _fail("SAFE_COMMAND_RESULT_INVALID")
        safe_result = record["safe_result"]
        if safe_result["profile"] != "verification":
            _fail("SAFE_COMMAND_PROFILE_INVALID")
        if not hmac.compare_digest(safe_result["argv_sha256"], _argv_sha256(argv)):
            _fail("COMMAND_ARGV_HASH_MISMATCH")
        item = by_log_path.get(str(record_path))
        if item is None or item["command"] != _command_text(argv):
            _fail("COMMAND_VERIFICATION_MISSING")
        if item["exit_code"] != safe_result["exit_code"] or (
            item["started_at"],
            item["finished_at"],
        ) != (safe_result["started_at"], safe_result["finished_at"]):
            _fail("COMMAND_VERIFICATION_MISMATCH")
        record_bytes = record_path.read_bytes()
        if not hmac.compare_digest(
            item["log_sha256"], hashlib.sha256(record_bytes).hexdigest()
        ):
            _fail("COMMAND_VERIFICATION_HASH_MISMATCH")
        attempted.add(index)
        expected_artifact_paths.add(str(record_path))
        if artifacts.get(str(record_path), {}).get("type") != "command_evidence":
            _fail("COMMAND_ARTIFACT_TYPE_MISMATCH")
        for stream_name in ("stdout_log", "stderr_log"):
            stream = safe_result[stream_name]
            if stream is not None:
                stream_stem = stream_name.removesuffix("_log")
                expected_path = outbox / f"command-{index:03d}.{stream_stem}.log"
                if stream["path"] != str(expected_path):
                    _fail("COMMAND_LOG_PATH_MISMATCH")
                expected_artifact_paths.add(stream["path"])
                artifact = artifacts.get(stream["path"])
                if artifact is None or (artifact["size_bytes"], artifact["sha256"]) != (
                    stream["size_bytes"],
                    stream["sha256"],
                ):
                    _fail("COMMAND_LOG_ARTIFACT_MISMATCH")
                if artifact["type"] != stream_name:
                    _fail("COMMAND_ARTIFACT_TYPE_MISMATCH")
    if len(attempted) != len(verification):
        _fail("COMMAND_EVIDENCE_CARDINALITY")
    if value["status"] == "completed":
        if attempted != set(range(len(packet.required_commands))):
            _fail("REQUIRED_COMMAND_MISSING")
        if any(
            item["exit_code"] != 0
            or _read_command_record(Path(item["log_path"]), artifact_limit)[
                "safe_result"
            ]["status"]
            != "SUCCESS"
            for item in verification
        ):
            _fail("COMPLETED_COMMAND_FAILED")
    extra = set(artifacts) - expected_artifact_paths
    frozen = value["worker_frozen_artifact"]
    if frozen is not None and frozen["path"] is not None:
        expected_artifact_paths.add(frozen["path"])
        extra.discard(frozen["path"])
    if extra or set(artifacts) != expected_artifact_paths:
        _fail("UNREGISTERED_ARTIFACT")


def _validate_status(value: Mapping[str, Any], packet: Mapping[str, Any]) -> None:
    status = value["status"]
    if status == "blocked" and not value["blockers"]:
        _fail("BLOCKED_WITHOUT_BLOCKER")
    if status == "failed" and not value["errors"]:
        _fail("FAILED_WITHOUT_ERROR")
    if status == "inconclusive" and not value["residual_risks"]:
        _fail("INCONCLUSIVE_WITHOUT_RISK")
    if status == "cancelled" and not value["cancellation_reason"]:
        _fail("CANCELLED_WITHOUT_REASON")
    evidence = value["acceptance_evidence"]
    ids = [item["acceptance_id"] for item in evidence]
    if len(ids) != len(set(ids)) or set(ids) != set(packet["issue"]["acceptance_ids"]):
        _fail("AC_EVIDENCE_IDENTITY_MISMATCH")
    if status == "completed" and any(
        item["status"] != "supported" for item in evidence
    ):
        _fail("COMPLETED_AC_UNSUPPORTED")


def _validate_frozen_artifact(value: Mapping[str, Any]) -> None:
    mode = value["integration_mode"]
    frozen = value["worker_frozen_artifact"]
    if mode == "patch_package":
        if frozen is not None:
            _fail("PATCH_MODE_HAS_FROZEN_ARTIFACT")
        return
    if frozen is None:
        _fail("FROZEN_ARTIFACT_MISSING")
    if mode == "commit":
        if (
            frozen["type"] != "commit"
            or frozen["path"] is not None
            or frozen["identity"] != value["repository"]["head_sha"]
            or frozen["tree_sha"] is None
        ):
            _fail("COMMIT_ARTIFACT_INVALID")
        descriptor = json.dumps(
            {
                "identity": frozen["identity"],
                "tree_sha": frozen["tree_sha"],
                "type": "commit",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if not hmac.compare_digest(
            frozen["sha256"], hashlib.sha256(descriptor).hexdigest()
        ):
            _fail("COMMIT_ARTIFACT_HASH_MISMATCH")
    elif frozen["type"] != "external_export" or frozen["path"] is None:
        _fail("EXPORT_ARTIFACT_INVALID")


def validate_worker_execution_result(
    result_bytes: bytes,
    *,
    packet: validate_worker_packet.ValidatedWorkerPacket,
    ownership_epoch: int,
    outbox: Path,
) -> ValidatedWorkerExecutionResult:
    if (
        isinstance(ownership_epoch, bool)
        or not isinstance(ownership_epoch, int)
        or not 0 <= ownership_epoch <= (1 << 64) - 1
    ):
        _fail("OWNERSHIP_EPOCH_INVALID")
    expected_outbox = Path(packet.value["verification"]["worker_outbox"])
    if outbox != expected_outbox or outbox.is_symlink():
        _fail("OUTBOX_IDENTITY_MISMATCH")
    try:
        outbox_metadata = outbox.stat(follow_symlinks=False)
        if outbox.resolve(strict=True) != outbox:
            _fail("OUTBOX_IDENTITY_MISMATCH")
    except OSError:
        _fail("OUTBOX_IDENTITY_MISMATCH")
    if (
        not stat.S_ISDIR(outbox_metadata.st_mode)
        or outbox_metadata.st_uid != os.getuid()
        or stat.S_IMODE(outbox_metadata.st_mode) & 0o022
    ):
        _fail("OUTBOX_OWNER_INVALID")
    max_bytes = packet.value["return_contract"]["max_manifest_bytes"]
    if len(result_bytes) > max_bytes:
        _fail("RESULT_TOO_LARGE")
    try:
        value = schema_runtime.strict_json_loads(result_bytes, max_bytes=max_bytes)
    except schema_runtime.JsonLoadFailure as exc:
        raise WorkerExecutionResultError(str(exc)) from None
    if not isinstance(value, dict):
        _fail("RESULT_NOT_OBJECT")
    findings = schema_runtime.validate_instance(
        "worker-execution-result-v1.schema.json", value
    )
    if findings:
        _fail(f"RESULT_SCHEMA_INVALID:{findings[0].code}:{findings[0].instance_path}")
    expected = packet.value
    if (
        value["run_id"] != expected["run_id"]
        or value["attempt_id"] != expected["attempt_id"]
        or value["issue_id"] != expected["issue"]["id"]
        or value["packet_sha256"] != packet.packet_sha256
    ):
        _fail("RESULT_IDENTITY_MISMATCH")
    repository = value["repository"]
    if (
        repository["worktree"] != expected["repository"]["worktree"]
        or repository["branch"] != expected["repository"]["branch"]
        or repository["base_sha"] != expected["repository"]["base_sha"]
        or value["integration_mode"] != expected["scope"]["integration_mode"]
    ):
        _fail("RESULT_REPOSITORY_MISMATCH")
    actual_outside = _outside_scope(value, expected)
    if value["changes"]["outside_allowed_scope"] != actual_outside:
        _fail("SCOPE_DECLARATION_MISMATCH")
    if actual_outside and value["status"] == "completed":
        _fail("COMPLETED_OUTSIDE_SCOPE")
    artifacts = {item["path"]: item for item in value["artifacts"]}
    if len(artifacts) != len(value["artifacts"]):
        _fail("ARTIFACT_PATH_DUPLICATE")
    for artifact in artifacts.values():
        if artifact["size_bytes"] > expected["verification"]["max_artifact_bytes"]:
            _fail("ARTIFACT_TOO_LARGE")
        _verify_artifact(
            Path(artifact["path"]),
            artifact["size_bytes"],
            artifact["sha256"],
            outbox,
        )
    _validate_status(value, expected)
    _validate_frozen_artifact(value)
    _validate_commands(value, packet, ownership_epoch, outbox, artifacts)
    allowed_entries = {
        "attempt.json",
        "result.json",
        "receipt.json",
        *(Path(path).name for path in artifacts),
    }
    try:
        if any(entry.name not in allowed_entries for entry in outbox.iterdir()):
            _fail("OUTBOX_UNDECLARED_ENTRY")
    except OSError:
        _fail("OUTBOX_IDENTITY_MISMATCH")
    return ValidatedWorkerExecutionResult(
        value, hashlib.sha256(result_bytes).hexdigest(), len(result_bytes)
    )


def _read_owned_file(path: Path, max_bytes: int, code: str) -> bytes:
    if path.is_symlink():
        _fail(code)
    try:
        metadata = path.stat(follow_symlinks=False)
        data = schema_runtime.read_bounded(path, max_bytes)
    except OSError:
        _fail(code)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail(code)
    return data


def validate_finalized_attempt(
    *,
    result_path: Path,
    receipt_path: Path,
    packet: validate_worker_packet.ValidatedWorkerPacket,
    ownership_epoch: int,
) -> ValidatedWorkerExecutionResult:
    """Validate the exact immutable result/receipt pair for one attempt."""
    outbox = Path(packet.value["verification"]["worker_outbox"])
    if result_path != outbox / "result.json" or receipt_path != outbox / "receipt.json":
        _fail("FINAL_PATH_MISMATCH")
    result_bytes = _read_owned_file(
        result_path,
        packet.value["return_contract"]["max_manifest_bytes"],
        "FINAL_RESULT_INVALID",
    )
    validated = validate_worker_execution_result(
        result_bytes,
        packet=packet,
        ownership_epoch=ownership_epoch,
        outbox=outbox,
    )
    if result_bytes != _canonical_json(validated.value):
        _fail("RESULT_NOT_CANONICAL")
    receipt_bytes = _read_owned_file(
        receipt_path,
        packet.value["return_contract"]["max_receipt_bytes"],
        "FINAL_RECEIPT_INVALID",
    )
    try:
        receipt = schema_runtime.strict_json_loads(
            receipt_bytes,
            max_bytes=packet.value["return_contract"]["max_receipt_bytes"],
        )
    except schema_runtime.JsonLoadFailure as exc:
        raise WorkerExecutionResultError(str(exc)) from None
    expected_keys = {
        "schema_version",
        "run_id",
        "attempt_id",
        "issue_id",
        "packet_sha256",
        "ownership_epoch",
        "status",
        "result_path",
        "result_size_bytes",
        "result_sha256",
        "summary",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        _fail("RECEIPT_INVALID")
    expected_receipt = {
        "schema_version": "beads.worker-result-receipt.v1",
        "run_id": packet.value["run_id"],
        "attempt_id": packet.value["attempt_id"],
        "issue_id": packet.value["issue"]["id"],
        "packet_sha256": packet.packet_sha256,
        "ownership_epoch": ownership_epoch,
        "status": validated.value["status"],
        "result_path": str(result_path),
        "result_size_bytes": validated.result_size_bytes,
        "result_sha256": validated.result_sha256,
        "summary": validated.value["summary"],
    }
    if receipt != expected_receipt:
        _fail("RECEIPT_IDENTITY_MISMATCH")
    if receipt_bytes != _canonical_json(receipt):
        _fail("RECEIPT_NOT_CANONICAL")
    if "\n" in receipt["summary"] or "\r" in receipt["summary"]:
        _fail("RECEIPT_SUMMARY_NOT_SINGLE_LINE")
    return validated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-packet", type=Path, required=True)
    parser.add_argument("--expected-packet-sha256", required=True)
    parser.add_argument("--ownership-epoch", type=int, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        packet = validate_worker_packet.validate_packet(
            schema_runtime.read_bounded(
                args.worker_packet, validate_worker_packet.MAX_PACKET_BYTES
            ),
            expected_packet_sha256=args.expected_packet_sha256,
        )
        validated = validate_finalized_attempt(
            result_path=args.result,
            receipt_path=args.receipt,
            packet=packet,
            ownership_epoch=args.ownership_epoch,
        )
        print(
            json.dumps(
                {"status": "valid", "result_sha256": validated.result_sha256},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, TypeError, schema_runtime.JsonLoadFailure):
        print('{"error_code":"WORKER_RESULT_INVALID","status":"invalid"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
