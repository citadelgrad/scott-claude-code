#!/usr/bin/env python3
"""Packet-bound worker attempt outbox and result lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import safe_output
import schema_runtime
import validate_worker_execution_result
import validate_worker_packet

MAX_UINT64 = (1 << 64) - 1
ATTEMPT_STATE_NAME = "attempt.json"


class WorkerResultError(ValueError):
    """A worker operation failed closed before an unsafe effect."""


class CommandOutcomeUnknownError(WorkerResultError):
    """A command may have run, so only a new attempt can safely continue."""

    error_code = "COMMAND_OUTCOME_UNKNOWN"
    safe_next_action = "start_new_attempt"

    def __init__(self, artifacts: tuple[dict[str, Any], ...]) -> None:
        super().__init__(self.error_code)
        self.artifacts = artifacts


@dataclass(frozen=True)
class AttemptContext:
    packet: validate_worker_packet.ValidatedWorkerPacket
    outbox: Path
    ownership_epoch: int
    owner_uid: int
    worktree_device: int
    worktree_inode: int
    outbox_device: int
    outbox_inode: int

    @property
    def packet_sha256(self) -> str:
        return self.packet.packet_sha256


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


def _owned_canonical_directory(path: Path, error_code: str) -> os.stat_result:
    if not path.is_absolute() or path.is_symlink():
        raise WorkerResultError(error_code)
    try:
        if path.resolve(strict=True) != path:
            raise WorkerResultError(error_code)
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise WorkerResultError(error_code) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise WorkerResultError(error_code)
    return metadata


def _state_value(
    packet: validate_worker_packet.ValidatedWorkerPacket,
    ownership_epoch: int,
    worktree_stat: os.stat_result,
    outbox_stat: os.stat_result,
) -> dict[str, Any]:
    value = packet.value
    return {
        "schema_version": "beads.worker-attempt-state.v1",
        "run_id": value["run_id"],
        "attempt_id": value["attempt_id"],
        "issue_id": value["issue"]["id"],
        "issue_key": value["issue"]["key"],
        "packet_sha256": packet.packet_sha256,
        "ownership_epoch": ownership_epoch,
        "worktree": value["repository"]["worktree"],
        "branch": value["repository"]["branch"],
        "base_sha": value["repository"]["base_sha"],
        "outbox": value["verification"]["worker_outbox"],
        "owner_uid": os.getuid(),
        "worktree_device": worktree_stat.st_dev,
        "worktree_inode": worktree_stat.st_ino,
        "outbox_device": outbox_stat.st_dev,
        "outbox_inode": outbox_stat.st_ino,
    }


def _read_exact_state(path: Path, expected: bytes) -> None:
    if path.is_symlink():
        raise WorkerResultError("ATTEMPT_STATE_INVALID")
    try:
        metadata = path.stat(follow_symlinks=False)
        actual = schema_runtime.read_bounded(path, 65536)
    except OSError:
        raise WorkerResultError("ATTEMPT_STATE_INVALID") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise WorkerResultError("ATTEMPT_STATE_INVALID")
    if actual != expected:
        try:
            current = schema_runtime.strict_json_loads(actual, max_bytes=65536)
        except schema_runtime.JsonLoadFailure:
            raise WorkerResultError("ATTEMPT_STATE_INVALID") from None
        if (
            isinstance(current, dict)
            and current.get("ownership_epoch")
            != json.loads(expected)["ownership_epoch"]
        ):
            raise WorkerResultError("STALE_OWNERSHIP_EPOCH")
        raise WorkerResultError("ATTEMPT_STATE_CONFLICT")


def initialize_attempt(
    packet_bytes: bytes,
    *,
    expected_packet_sha256: str,
    ownership_epoch: int,
) -> AttemptContext:
    """Validate and immutably bind this process to one attempt outbox."""
    if (
        isinstance(ownership_epoch, bool)
        or not isinstance(ownership_epoch, int)
        or not 0 <= ownership_epoch <= MAX_UINT64
    ):
        raise WorkerResultError("OWNERSHIP_EPOCH_INVALID")
    try:
        packet = validate_worker_packet.validate_packet(
            packet_bytes, expected_packet_sha256=expected_packet_sha256
        )
    except validate_worker_packet.PacketValidationError as exc:
        raise WorkerResultError(str(exc)) from None
    worktree = Path(packet.value["repository"]["worktree"])
    try:
        current = Path.cwd().resolve(strict=True)
    except OSError:
        raise WorkerResultError("WRONG_WORKTREE") from None
    if current != worktree:
        raise WorkerResultError("WRONG_WORKTREE")
    worktree_stat = _owned_canonical_directory(worktree, "WORKTREE_OWNER_INVALID")
    outbox = Path(packet.value["verification"]["worker_outbox"])
    outbox_stat = _owned_canonical_directory(outbox, "OUTBOX_OWNER_INVALID")
    if len(set(packet.required_commands)) != len(packet.required_commands):
        raise WorkerResultError("DUPLICATE_DECLARED_COMMAND")
    state = _state_value(packet, ownership_epoch, worktree_stat, outbox_stat)
    data = _canonical_json(state)
    state_path = outbox / ATTEMPT_STATE_NAME
    if state_path.exists() or state_path.is_symlink():
        _read_exact_state(state_path, data)
    else:
        try:
            if any(outbox.iterdir()):
                raise WorkerResultError("OUTBOX_NOT_FRESH")
        except OSError:
            raise WorkerResultError("OUTBOX_INVALID") from None
        safe_output.write_new_artifact(state_path, data)
    return AttemptContext(
        packet,
        outbox,
        ownership_epoch,
        os.getuid(),
        worktree_stat.st_dev,
        worktree_stat.st_ino,
        outbox_stat.st_dev,
        outbox_stat.st_ino,
    )


def _ensure_context(context: AttemptContext) -> None:
    worktree = Path(context.packet.value["repository"]["worktree"])
    outbox = context.outbox
    try:
        if Path.cwd().resolve(strict=True) != worktree:
            raise WorkerResultError("WRONG_WORKTREE")
    except OSError:
        raise WorkerResultError("WRONG_WORKTREE") from None
    worktree_stat = _owned_canonical_directory(worktree, "WORKTREE_OWNER_INVALID")
    outbox_stat = _owned_canonical_directory(outbox, "OUTBOX_OWNER_INVALID")
    if os.getuid() != context.owner_uid or (
        worktree_stat.st_dev,
        worktree_stat.st_ino,
        outbox_stat.st_dev,
        outbox_stat.st_ino,
    ) != (
        context.worktree_device,
        context.worktree_inode,
        context.outbox_device,
        context.outbox_inode,
    ):
        raise WorkerResultError("ATTEMPT_IDENTITY_CHANGED")
    expected = _canonical_json(
        _state_value(
            context.packet,
            context.ownership_epoch,
            worktree_stat,
            outbox_stat,
        )
    )
    _read_exact_state(outbox / ATTEMPT_STATE_NAME, expected)


def _command_intent(
    context: AttemptContext, command_index: int, command: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "schema_version": "beads.worker-command-intent.v1",
        "run_id": context.packet.value["run_id"],
        "attempt_id": context.packet.value["attempt_id"],
        "issue_id": context.packet.value["issue"]["id"],
        "packet_sha256": context.packet.packet_sha256,
        "ownership_epoch": context.ownership_epoch,
        "command_index": command_index,
        "argv": list(command),
        "safe_next_action": "start_new_attempt",
    }


def _unknown_command_artifacts(
    paths: tuple[Path, ...], max_bytes: int
) -> tuple[dict[str, Any], ...]:
    artifacts: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        entry: dict[str, Any] = {"path": str(path), "size_bytes": None, "sha256": None}
        try:
            metadata = path.stat(follow_symlinks=False)
            if (
                not path.is_symlink()
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o600
            ):
                data = schema_runtime.read_bounded(path, max_bytes)
                entry.update(
                    size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
                )
        except (OSError, schema_runtime.JsonLoadFailure):
            pass
        artifacts.append(entry)
    return tuple(artifacts)


def _command_outcome_unknown(
    paths: tuple[Path, ...], max_bytes: int
) -> CommandOutcomeUnknownError:
    return CommandOutcomeUnknownError(_unknown_command_artifacts(paths, max_bytes))


def _read_completed_command(
    *,
    context: AttemptContext,
    command_index: int,
    command: tuple[str, ...],
    intent_path: Path,
    intent_bytes: bytes,
    record_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    artifact_limit: int,
) -> dict[str, Any]:
    paths = (intent_path, record_path, stdout_path, stderr_path)
    try:
        _read_exact_final(intent_path, intent_bytes, "COMMAND_OUTCOME_UNKNOWN")
        record_bytes = schema_runtime.read_bounded(record_path, artifact_limit)
        record_metadata = record_path.stat(follow_symlinks=False)
        if (
            record_path.is_symlink()
            or not stat.S_ISREG(record_metadata.st_mode)
            or record_metadata.st_uid != os.getuid()
            or stat.S_IMODE(record_metadata.st_mode) != 0o600
        ):
            raise ValueError("invalid command evidence file")
        evidence = schema_runtime.strict_json_loads(
            record_bytes, max_bytes=artifact_limit
        )
        if not isinstance(evidence, dict) or record_bytes != _canonical_json(evidence):
            raise ValueError("invalid command evidence")
        expected_identity = {
            "schema_version": "beads.worker-command-evidence.v1",
            "run_id": context.packet.value["run_id"],
            "attempt_id": context.packet.value["attempt_id"],
            "issue_id": context.packet.value["issue"]["id"],
            "packet_sha256": context.packet.packet_sha256,
            "ownership_epoch": context.ownership_epoch,
            "command_index": command_index,
            "argv": list(command),
            "safe_result": evidence.get("safe_result"),
        }
        if evidence != expected_identity:
            raise ValueError("command evidence identity mismatch")
        findings = schema_runtime.validate_instance(
            "safe-command-result-v1.schema.json", evidence["safe_result"]
        )
        if findings:
            raise ValueError("invalid safe command result")
        safe_result = evidence["safe_result"]
        argv_descriptor = json.dumps(
            list(command), ensure_ascii=False, separators=(",", ":")
        ).encode()
        if safe_result["profile"] != "verification" or not hmac.compare_digest(
            safe_result["argv_sha256"], hashlib.sha256(argv_descriptor).hexdigest()
        ):
            raise ValueError("safe command identity mismatch")
        for key, expected_path in (
            ("stdout_log", stdout_path),
            ("stderr_log", stderr_path),
        ):
            artifact = evidence["safe_result"][key]
            if artifact is None:
                if expected_path.exists() or expected_path.is_symlink():
                    raise ValueError("unexpected command log")
                continue
            if artifact["path"] != str(expected_path):
                raise ValueError("command log path mismatch")
            data = schema_runtime.read_bounded(expected_path, artifact_limit)
            metadata = expected_path.stat(follow_symlinks=False)
            if (
                expected_path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or artifact["size_bytes"] != len(data)
                or not hmac.compare_digest(
                    artifact["sha256"], hashlib.sha256(data).hexdigest()
                )
            ):
                raise ValueError("command log identity mismatch")
        return evidence
    except (OSError, ValueError, TypeError, schema_runtime.JsonLoadFailure):
        raise _command_outcome_unknown(paths, artifact_limit) from None


def run_declared_command(
    context: AttemptContext,
    *,
    command_index: int,
    sensitive_values_file: Path,
) -> dict[str, Any]:
    """Run one exact packet command through safe_output and persist its evidence."""
    _ensure_context(context)
    if isinstance(command_index, bool) or not isinstance(command_index, int):
        raise WorkerResultError("COMMAND_NOT_DECLARED")
    try:
        command = context.packet.required_commands[command_index]
    except IndexError:
        raise WorkerResultError("COMMAND_NOT_DECLARED") from None
    if command_index < 0:
        raise WorkerResultError("COMMAND_NOT_DECLARED")
    try:
        authorized_index = validate_worker_packet.authorize_command(
            context.packet, command
        )
    except validate_worker_packet.PacketValidationError as exc:
        raise WorkerResultError(str(exc)) from None
    if authorized_index != command_index:
        raise WorkerResultError("COMMAND_INDEX_MISMATCH")
    if not Path(command[0]).is_absolute():
        raise WorkerResultError("COMMAND_EXECUTABLE_NOT_ABSOLUTE")
    try:
        sensitive = safe_output._load_sensitive_values(sensitive_values_file)
    except (OSError, ValueError, TypeError) as exc:
        raise WorkerResultError("SENSITIVE_DESCRIPTOR_INVALID") from exc
    sanitized_argv = [
        safe_output.sanitize_value(
            arg, sensitive=sensitive, max_utf8_bytes=safe_output.MAX_ARG_BYTES
        )
        for arg in command
    ]
    if any(
        item.status != "OK" or item.value != arg
        for item, arg in zip(sanitized_argv, command)
    ):
        raise WorkerResultError("COMMAND_ARGV_SENSITIVE")

    stem = f"command-{command_index:03d}"
    intent_path = context.outbox / f"{stem}.intent.json"
    record_path = context.outbox / f"{stem}.json"
    stdout_path = context.outbox / f"{stem}.stdout.log"
    stderr_path = context.outbox / f"{stem}.stderr.log"

    artifact_limit = context.packet.value["verification"]["max_artifact_bytes"]
    stream_limit = min(1024 * 1024, artifact_limit)
    total_limit = min(safe_output.MAX_TOTAL_BYTES, stream_limit * 2)
    spec = safe_output.CommandSpec(
        profile="verification",
        argv=command,
        cwd=Path(context.packet.value["repository"]["worktree"]),
        stdin_bytes=None,
        timeout_seconds=min(
            safe_output.MAX_TIMEOUT_SECONDS,
            context.packet.value["verification"]["advisory_wall_clock_seconds"],
        ),
        stdout_limit_bytes=stream_limit,
        stderr_limit_bytes=stream_limit,
        total_limit_bytes=total_limit,
        output_codec="utf8_text",
        stdout_log=stdout_path,
        stderr_log=stderr_path,
    )
    intent_bytes = _canonical_json(_command_intent(context, command_index, command))
    if len(intent_bytes) > artifact_limit:
        raise WorkerResultError("COMMAND_INTENT_TOO_LARGE")
    paths = (intent_path, record_path, stdout_path, stderr_path)
    if record_path.exists() or record_path.is_symlink():
        return _read_completed_command(
            context=context,
            command_index=command_index,
            command=command,
            intent_path=intent_path,
            intent_bytes=intent_bytes,
            record_path=record_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            artifact_limit=artifact_limit,
        )
    if any(path.exists() or path.is_symlink() for path in paths):
        raise _command_outcome_unknown(paths, artifact_limit)
    try:
        safe_output.write_new_artifact(intent_path, intent_bytes)
    except (OSError, ValueError):
        if intent_path.exists() or intent_path.is_symlink():
            raise _command_outcome_unknown(paths, artifact_limit) from None
        raise WorkerResultError("COMMAND_INTENT_WRITE_FAILED") from None
    try:
        result, _ = safe_output.run_command(spec, sensitive=sensitive)
        _ensure_context(context)
        evidence = {
            "schema_version": "beads.worker-command-evidence.v1",
            "run_id": context.packet.value["run_id"],
            "attempt_id": context.packet.value["attempt_id"],
            "issue_id": context.packet.value["issue"]["id"],
            "packet_sha256": context.packet.packet_sha256,
            "ownership_epoch": context.ownership_epoch,
            "command_index": command_index,
            "argv": list(command),
            "safe_result": safe_output.result_dict(result),
        }
        data = _canonical_json(evidence)
        if len(data) > artifact_limit:
            raise WorkerResultError("COMMAND_EVIDENCE_TOO_LARGE")
        safe_output.write_new_artifact(record_path, data)
    except (OSError, ValueError, TypeError):
        raise _command_outcome_unknown(paths, artifact_limit) from None
    return evidence


def _sanitize_tree(value: Any, sensitive: safe_output.SensitiveSet) -> Any:
    if isinstance(value, str):
        clean = safe_output.sanitize_value(
            value, sensitive=sensitive, max_utf8_bytes=65536
        )
        if clean.value is None:
            raise WorkerResultError("RESULT_REDACTION_FAILED")
        return clean.value
    if isinstance(value, list):
        return [_sanitize_tree(item, sensitive) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or safe_output.is_sensitive_label(key):
                raise WorkerResultError("RESULT_REDACTION_FAILED")
            sanitized[key] = _sanitize_tree(item, sensitive)
        return sanitized
    return value


def _read_exact_final(path: Path, expected: bytes, code: str) -> None:
    if path.is_symlink():
        raise WorkerResultError(code)
    try:
        metadata = path.stat(follow_symlinks=False)
        actual = schema_runtime.read_bounded(path, max(65536, len(expected)))
    except OSError:
        raise WorkerResultError(code) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or actual != expected
    ):
        raise WorkerResultError(code)


def finalize_attempt(
    context: AttemptContext,
    candidate: dict[str, Any],
    *,
    sensitive_values_file: Path,
) -> dict[str, Any]:
    """Sanitize, validate, and crash-safely publish result then receipt."""
    _ensure_context(context)
    try:
        sensitive = safe_output._load_sensitive_values(sensitive_values_file)
    except (OSError, ValueError, TypeError) as exc:
        raise WorkerResultError("SENSITIVE_DESCRIPTOR_INVALID") from exc
    clean = _sanitize_tree(candidate, sensitive)
    if not isinstance(clean, dict):
        raise WorkerResultError("RESULT_NOT_OBJECT")
    result_bytes = _canonical_json(clean)
    max_manifest = context.packet.value["return_contract"]["max_manifest_bytes"]
    if len(result_bytes) > max_manifest:
        raise WorkerResultError("RESULT_TOO_LARGE")
    try:
        validated = validate_worker_execution_result.validate_worker_execution_result(
            result_bytes,
            packet=context.packet,
            ownership_epoch=context.ownership_epoch,
            outbox=context.outbox,
        )
    except validate_worker_execution_result.WorkerExecutionResultError as exc:
        raise WorkerResultError(str(exc)) from None

    result_path = context.outbox / "result.json"
    receipt = {
        "schema_version": "beads.worker-result-receipt.v1",
        "run_id": context.packet.value["run_id"],
        "attempt_id": context.packet.value["attempt_id"],
        "issue_id": context.packet.value["issue"]["id"],
        "packet_sha256": context.packet.packet_sha256,
        "ownership_epoch": context.ownership_epoch,
        "status": clean["status"],
        "result_path": str(result_path),
        "result_size_bytes": validated.result_size_bytes,
        "result_sha256": validated.result_sha256,
        "summary": clean["summary"],
    }
    if "\n" in receipt["summary"] or "\r" in receipt["summary"]:
        raise WorkerResultError("RECEIPT_SUMMARY_NOT_SINGLE_LINE")
    receipt_bytes = _canonical_json(receipt)
    if (
        len(receipt_bytes)
        > context.packet.value["return_contract"]["max_receipt_bytes"]
    ):
        raise WorkerResultError("RECEIPT_TOO_LARGE")
    receipt_path = context.outbox / "receipt.json"

    # Preflight the complete pair before publishing either half.  Publication
    # remains result-first so a crash can be retried without exposing a receipt
    # whose referenced result does not yet exist.
    result_preexisting = result_path.exists() or result_path.is_symlink()
    receipt_preexisting = receipt_path.exists() or receipt_path.is_symlink()
    if result_preexisting:
        _read_exact_final(result_path, result_bytes, "FINAL_RESULT_CONFLICT")
    if receipt_preexisting:
        _read_exact_final(receipt_path, receipt_bytes, "FINAL_RECEIPT_CONFLICT")
    if not result_preexisting:
        try:
            safe_output.write_new_artifact(result_path, result_bytes)
        except (OSError, ValueError):
            raise WorkerResultError("FINALIZE_WRITE_FAILED") from None
    _ensure_context(context)
    if not receipt_preexisting:
        try:
            safe_output.write_new_artifact(receipt_path, receipt_bytes)
        except (OSError, ValueError):
            raise WorkerResultError("FINALIZE_WRITE_FAILED") from None
    return receipt


def _add_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--worker-packet", type=Path, required=True)
    parser.add_argument("--expected-packet-sha256", required=True)
    parser.add_argument("--ownership-epoch", type=int, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("initialize")
    _add_binding_arguments(initialize)
    run = sub.add_parser("run-command")
    _add_binding_arguments(run)
    run.add_argument("--command-index", type=int, required=True)
    run.add_argument("--sensitive-values-file", type=Path, required=True)
    finalize = sub.add_parser("finalize")
    _add_binding_arguments(finalize)
    finalize.add_argument("--input", type=Path, required=True)
    finalize.add_argument("--sensitive-values-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        packet_bytes = schema_runtime.read_bounded(
            args.worker_packet, validate_worker_packet.MAX_PACKET_BYTES
        )
        context = initialize_attempt(
            packet_bytes,
            expected_packet_sha256=args.expected_packet_sha256,
            ownership_epoch=args.ownership_epoch,
        )
        if args.command == "initialize":
            output = {
                "status": "initialized",
                "attempt_id": context.packet.value["attempt_id"],
            }
            code = 0
        elif args.command == "run-command":
            evidence = run_declared_command(
                context,
                command_index=args.command_index,
                sensitive_values_file=args.sensitive_values_file,
            )
            output = {
                "status": evidence["safe_result"]["status"],
                "command_index": evidence["command_index"],
                "evidence_path": str(
                    context.outbox / f"command-{args.command_index:03d}.json"
                ),
            }
            code = 0 if evidence["safe_result"]["status"] == "SUCCESS" else 1
        else:
            candidate = schema_runtime.strict_json_loads(
                schema_runtime.read_bounded(
                    args.input,
                    context.packet.value["return_contract"]["max_manifest_bytes"],
                ),
                max_bytes=context.packet.value["return_contract"]["max_manifest_bytes"],
            )
            if not isinstance(candidate, dict):
                raise WorkerResultError("RESULT_NOT_OBJECT")
            output = finalize_attempt(
                context,
                candidate,
                sensitive_values_file=args.sensitive_values_file,
            )
            code = 0
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return code
    except CommandOutcomeUnknownError as exc:
        print(
            json.dumps(
                {
                    "error_code": exc.error_code,
                    "status": "REFUSED",
                    "safe_next_action": exc.safe_next_action,
                    "artifacts": list(exc.artifacts),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except (OSError, ValueError, TypeError, schema_runtime.JsonLoadFailure):
        print('{"error_code":"WORKER_RESULT_REFUSED","status":"REFUSED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
