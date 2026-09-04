#!/usr/bin/env python3
"""Stateful lane-freeze and integration-candidate runtime (spec §8.0 rows
freeze-lane / verify-lane / record-review / build-candidate / apply-candidate).

Every capability runs under the run lock with the write-ahead intent /
exact-readback / typed-resolution protocol of ``coordinator_state``.  The
module performs no Beads, Hermes, Dolt-remote, push, or secret-bearing
action; it never calls a tracker or a remote.  Crash recovery re-derives
classification through typed probes and never replays blindly.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).parent))
import beads_ownership
import coordinator_state as state
import lane_snapshot
import package_lane
import safe_output
import schema_runtime
import validate_lane_freeze
import validate_worker_execution_result
import validate_worker_packet

GENESIS = state.GENESIS_SHA256
GIT_TIMEOUT = lane_snapshot.GIT_TIMEOUT_SECONDS
EFFECT_FREEZE = "LANE_FREEZE_PREPARED"
EFFECT_CANDIDATE = "CANDIDATE_BUILD_PREPARED"
EFFECT_APPLY = "PRIMARY_INTEGRATION_PREPARED"

_HIGH_RISK_TOKENS = (
    "security",
    "auth",
    "permission",
    "schema",
    "migration",
    "concurrency",
    "atomic",
    "secret",
    "token",
    "crypto",
)


class IntegrationError(ValueError):
    """Typed fail-closed integration refusal (``.code``)."""

    def __init__(self, code: str, *, status: str = "refused") -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass
class RunContext:
    run_directory: Path
    manifest: dict[str, Any]
    journal: state.OperationJournal
    checkpoints: state.CheckpointStore
    crash_hook: Callable[[str], None]

    @property
    def run_id(self) -> str:
        return self.manifest["run_id"]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def open_run(
    run_directory: Path,
    *,
    crash_hook: Callable[[str], None] = state.NOOP_HOOK,
) -> RunContext:
    run_directory = Path(run_directory)
    manifest = state.load_run_manifest(run_directory)
    journal = state.OperationJournal.create(run_directory)
    checkpoints = state.CheckpointStore.open_existing(run_directory, journal)
    return RunContext(run_directory, manifest, journal, checkpoints, crash_hook)


def _canonical(value: Any) -> bytes:
    return state.canonical_bytes(value)


def _sha256(raw: bytes) -> str:
    return state.sha256_bytes(raw)


def _issue_key(issue_id: str) -> str:
    return state.issue_key(issue_id)


def _result(status: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, **fields}


def _failure(code: str, status: str = "refused", **fields: Any) -> dict[str, Any]:
    return _result(status, error_code=code, **fields)


def _issue_entry(current: state.CheckpointRef, issue_id: str) -> dict[str, Any]:
    entry = current.value["issues"].get(issue_id)
    if entry is None:
        raise IntegrationError("ISSUE_NOT_IN_RUN")
    return entry


def _git(repository: Path, *args: str) -> bytes:
    try:
        return lane_snapshot.git_query(repository, *args)
    except lane_snapshot.LaneSnapshotError as exc:
        raise IntegrationError("PRIMARY_REPOSITORY_INVALID") from exc


def _probe_lane_artifact(path: Path, intended_sha: str) -> dict[str, Any]:
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "lane_artifact",
        "target_identity": str(path.resolve()),
        "expected_before_sha256": GENESIS,
        "intended_after_identity": str(path.resolve()),
        "intended_after_sha256": intended_sha,
        "descriptor": ["lane_artifact", "freeze"],
        "timeout_seconds": 30,
        "required_authority": "coordinator_parent",
    }


def _probe_git_ref_tree(ref: str, intended: str) -> dict[str, Any]:
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "git_ref_tree",
        "target_identity": ref,
        "expected_before_sha256": GENESIS,
        "intended_after_identity": ref,
        "intended_after_sha256": intended,
        "descriptor": ["git_ref_tree", "primary"],
        "timeout_seconds": 30,
        "required_authority": "coordinator_parent",
    }


def _prepared(
    context: RunContext,
    *,
    effect: str,
    operation_id: str,
    input_path: Path,
    input_sha: str,
    expected_pre: str,
    probe: dict[str, Any],
    issue_id: str | None,
    epoch: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": operation_id,
        "run_id": context.run_id,
        "attempt_id": None,
        "issue_id": issue_id,
        "ownership_epoch": epoch,
        "effect_type": effect,
        "immutable_input_path": str(input_path.resolve()),
        "immutable_input_sha256": input_sha,
        "expected_pre_state_sha256": expected_pre,
        "recovery_probe": probe,
        "timestamp": utc_now(),
        "authority_class": "coordinator_parent",
        "previous_event_sha256": GENESIS,
        "phase": "PREPARED",
    }


def _resolution(
    prepared: Mapping[str, Any],
    *,
    status: str,
    observed_sha: str | None,
    evidence_path: Path | None,
    evidence_sha: str | None,
) -> dict[str, Any]:
    return {
        **prepared,
        "phase": "RESOLUTION",
        "timestamp": utc_now(),
        "observed_post_state_sha256": observed_sha,
        "readback_evidence_path": str(evidence_path.resolve())
        if evidence_path
        else None,
        "readback_evidence_sha256": evidence_sha,
        "status": status,
        "error": None
        if status in {"APPLIED", "NOT_APPLIED"}
        else {
            "code": f"INTEGRATION_{status}",
            "template_id": "integration_effect_not_applied",
            "field_path": "/integration",
            "parameters": [],
        },
    }


def _journal_records(context: RunContext) -> list[dict[str, Any]]:
    return list(context.journal.read().records)


def _dangling_prepared(
    context: RunContext, effect: str, issue_id: str | None = None
) -> dict[str, Any] | None:
    records = _journal_records(context)
    resolved = {
        record["operation_id"] for record in records if record["phase"] == "RESOLUTION"
    }
    for record in records:
        if (
            record["phase"] == "PREPARED"
            and record["effect_type"] == effect
            and (issue_id is None or record.get("issue_id") == issue_id)
            and record["operation_id"] not in resolved
        ):
            return record
    return None


def _append_checkpoint(
    context: RunContext,
    current: state.CheckpointRef,
    mutate_issue: Callable[[dict[str, Any]], None] | None = None,
    *,
    extra_issues: dict[str, dict[str, Any]] | None = None,
) -> state.CheckpointRef:
    value = copy.deepcopy(current.value)
    if mutate_issue is not None:
        for entry in value["issues"].values():
            mutate_issue(entry)
    if extra_issues:
        value["issues"].update(copy.deepcopy(extra_issues))
    value["generation"] = current.generation + 1
    value["previous_checkpoint_sha256"] = current.generation_sha256
    value["created_at"] = utc_now()
    return context.checkpoints.accept(value, hook=context.crash_hook)


def _update_one_issue(
    context: RunContext,
    current: state.CheckpointRef,
    issue_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> state.CheckpointRef:
    value = copy.deepcopy(current.value)
    entry = value["issues"].get(issue_id)
    if entry is None:
        raise IntegrationError("ISSUE_NOT_IN_RUN")
    mutate(entry)
    value["generation"] = current.generation + 1
    value["previous_checkpoint_sha256"] = current.generation_sha256
    value["created_at"] = utc_now()
    return context.checkpoints.accept(value, hook=context.crash_hook)


def _ownership(context: RunContext, issue_id: str) -> dict[str, Any]:
    store = beads_ownership.OwnershipStore.open_existing(
        Path(context.manifest["run_root"])
    )
    inspected = store.inspect_readonly(issue_id)
    if inspected.disposition != "held" or inspected.record is None:
        raise IntegrationError("ISSUE_OWNERSHIP_INVALID", status="conflict")
    if inspected.record["run_id"] != context.run_id:
        raise IntegrationError("ISSUE_OWNERSHIP_INVALID", status="conflict")
    return inspected.record


def _load_imports(context: RunContext, issue_id: str) -> tuple[Path, dict[str, Any]]:
    directory = context.run_directory / "imports" / _issue_key(issue_id)
    if not directory.is_dir():
        raise IntegrationError("IMPORT_MISSING")
    packet_path = directory / "packet.json"
    result_path = directory / "result.json"
    receipt_path = directory / "receipt.json"
    for path in (packet_path, result_path, receipt_path):
        if not path.is_file():
            raise IntegrationError("IMPORT_MISSING")
    try:
        packet_bytes = state.validate_owner_file(
            packet_path, root=context.run_directory
        ).read_bytes()
        result_bytes = state.validate_owner_file(
            result_path, root=context.run_directory
        ).read_bytes()
        receipt_bytes = state.validate_owner_file(
            receipt_path, root=context.run_directory
        ).read_bytes()
    except state.StateError as exc:
        raise IntegrationError("IMPORT_INVALID") from exc
    return directory, {
        "packet_path": packet_path,
        "packet_bytes": packet_bytes,
        "result_path": result_path,
        "result_bytes": result_bytes,
        "receipt_path": receipt_path,
        "receipt_bytes": receipt_bytes,
    }


def _validate_attempt(
    context: RunContext,
    issue_id: str,
    imports: dict[str, Any],
    entry: dict[str, Any],
    epoch: int,
) -> tuple[Any, Any]:
    expected_packet_sha = entry.get("packet_sha256")
    packet = validate_worker_packet.validate_packet(
        imports["packet_bytes"], expected_packet_sha256=expected_packet_sha
    )
    if issue_id != packet.value["issue"]["id"]:
        raise IntegrationError("LANE_FREEZE_IDENTITY_MISMATCH", status="conflict")
    outbox = Path(packet.value["verification"]["worker_outbox"])
    validated = validate_worker_execution_result.validate_worker_execution_result(
        imports["result_bytes"],
        packet=packet,
        ownership_epoch=epoch,
        outbox=outbox,
    )
    # The imported receipt must still bind the exact result pair.
    receipt = schema_runtime.strict_json_loads(
        imports["receipt_bytes"],
        max_bytes=packet.value["return_contract"]["max_receipt_bytes"],
    )
    expected_receipt = {
        "schema_version": "beads.worker-result-receipt.v1",
        "run_id": packet.value["run_id"],
        "attempt_id": packet.value["attempt_id"],
        "issue_id": packet.value["issue"]["id"],
        "packet_sha256": packet.packet_sha256,
        "ownership_epoch": epoch,
        "status": validated.value["status"],
        "result_path": str(outbox / "result.json"),
        "result_size_bytes": validated.result_size_bytes,
        "result_sha256": validated.result_sha256,
        "summary": validated.value["summary"],
    }
    if receipt != expected_receipt:
        raise IntegrationError("LANE_FREEZE_IDENTITY_MISMATCH", status="conflict")
    return packet, validated


def _recapture(packet: Any, validated: Any) -> lane_snapshot.LaneSnapshot:
    try:
        snapshot = lane_snapshot.capture(
            Path(packet.value["repository"]["worktree"]),
            packet.value["repository"]["base_sha"],
            exclude=Path(packet.value["verification"]["worker_outbox"]),
            max_untracked_file_bytes=lane_snapshot.budget_from_packet(packet.value),
        )
    except lane_snapshot.LaneSnapshotError:
        raise IntegrationError("LANE_FREEZE_LANE_DRIFT", status="conflict") from None
    if (
        snapshot.head_sha != validated.value["repository"]["head_sha"]
        or snapshot.lane_state != validated.value["lane_state"]
        or list(snapshot.changed_paths) != validated.value["changes"]["paths"]
    ):
        raise IntegrationError("LANE_FREEZE_LANE_DRIFT", status="conflict")
    return snapshot


# ---------------------------------------------------------------------------
# freeze_lane
# ---------------------------------------------------------------------------


def _recovery_freeze(
    context: RunContext, issue_id: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Classify one dangling LANE_FREEZE_PREPARED through its typed probe.

    Returns ("applied", result) when the freeze already landed, ("conflict",
    None) after journaling a CONFLICT resolution, or ("reuse", prepared) with
    the dangling prepared event for in-flight continuation.
    """
    prepared = _dangling_prepared(context, EFFECT_FREEZE, issue_id)
    if prepared is None:
        return ("none", None)
    probe = prepared["recovery_probe"]
    path = Path(probe["intended_after_identity"])
    intended = probe["intended_after_sha256"]
    if path.is_file():
        observed = _sha256(path.read_bytes())
        if observed == intended:
            context.journal.append(
                _resolution(
                    prepared,
                    status="APPLIED",
                    observed_sha=intended,
                    evidence_path=path,
                    evidence_sha=intended,
                ),
                hook=context.crash_hook,
            )
            current = context.checkpoints.current(rebuild_pointer=True)
            entry = current.value["issues"].get(prepared["issue_id"])
            freeze_sha = entry["artifact"]["lane_freeze_sha256"] if entry else None
            return (
                "applied",
                _result("success", freeze_sha256=freeze_sha, recovered=True),
            )
        context.journal.append(
            _resolution(
                prepared,
                status="CONFLICT",
                observed_sha=observed,
                evidence_path=None,
                evidence_sha=None,
            ),
            hook=context.crash_hook,
        )
        raise IntegrationError("LANE_FREEZE_RECOVERY_CONFLICT", status="conflict")
    # The freeze never landed: reuse the dangling prepared event in-flight.
    return ("reuse", prepared)


def freeze_lane(
    context: RunContext,
    issue_id: str,
    *,
    expected_result_sha256: str,
) -> dict[str, Any]:
    """Freeze one accepted worker lane into a parent-owned immutable record."""
    with state.exclusive_lock(
        context.run_directory / "run.lock",
        root=context.run_directory,
        hook=context.crash_hook,
    ):
        classification, recovered = _recovery_freeze(context, issue_id)
        if classification == "applied":
            assert recovered is not None
            return recovered
        current = context.checkpoints.current(rebuild_pointer=True)
        entry = _issue_entry(current, issue_id)
        if entry.get("result_sha256") != expected_result_sha256:
            raise IntegrationError("LANE_FREEZE_IDENTITY_MISMATCH", status="conflict")
        ownership = _ownership(context, issue_id)
        epoch = entry["ownership"]["epoch"]
        if ownership["epoch"] != epoch:
            raise IntegrationError("ISSUE_OWNERSHIP_INVALID", status="conflict")
        imports_directory, imports = _load_imports(context, issue_id)
        try:
            packet, validated = _validate_attempt(
                context, issue_id, imports, entry, epoch
            )
        except validate_worker_execution_result.WorkerExecutionResultError as exc:
            if str(exc) in {
                "RESULT_HEAD_DRIFT",
                "RESULT_LANE_STATE_DRIFT",
                "RESULT_CHANGED_PATHS_DRIFT",
            }:
                _update_one_issue(context, current, issue_id, _artifact_invalid)
                raise IntegrationError(
                    "LANE_FREEZE_LANE_DRIFT", status="conflict"
                ) from None
            raise IntegrationError(str(exc), status="conflict") from None
        if validated.result_sha256 != expected_result_sha256:
            raise IntegrationError("LANE_FREEZE_IDENTITY_MISMATCH", status="conflict")
        lanes_directory = context.run_directory / "lanes" / _issue_key(issue_id)
        operation_id = state.semantic_operation_id(
            {
                "schema": "beads.lane-freeze.v1",
                "effect_type": EFFECT_FREEZE,
                "target_identity": str(
                    (lanes_directory / "lane-freeze.json").resolve()
                ),
                "immutable_input_sha256": validated.result_sha256,
                "ownership_epoch": epoch,
            }
        )
        # Idempotent replay: this exact freeze already resolved APPLIED.
        records = _journal_records(context)
        for record in records:
            if (
                record["operation_id"] == operation_id
                and record["phase"] == "RESOLUTION"
                and record["status"] == "APPLIED"
            ):
                return _result(
                    "success",
                    freeze_sha256=entry["artifact"]["lane_freeze_sha256"],
                    candidate_tree_sha256=None,
                    replayed=True,
                )
        snapshot = _recapture(packet, validated)
        if not snapshot.inventory:
            _update_one_issue(context, current, issue_id, _artifact_invalid)
            raise IntegrationError("LANE_FREEZE_EMPTY_INVENTORY")

        mode = packet.value["scope"]["integration_mode"]
        state.ensure_owner_directory(lanes_directory, root=context.run_directory)
        if mode == "patch_package":
            freeze, artifact_bytes, artifact_path = _build_patch_freeze(
                context, packet, validated, issue_id, epoch, lanes_directory
            )
        elif mode == "commit":
            freeze, artifact_bytes, artifact_path = _normalize_commit_freeze(
                context,
                packet,
                validated,
                snapshot,
                issue_id,
                epoch,
                lanes_directory,
            )
        elif mode == "external_export":
            freeze, artifact_bytes, artifact_path = _normalize_export_freeze(
                context,
                packet,
                validated,
                snapshot,
                issue_id,
                epoch,
                lanes_directory,
            )
        else:  # pragma: no cover - packet schema pins the enum
            raise IntegrationError("LANE_FREEZE_MODE_MISMATCH")
        freeze_bytes = _canonical(freeze)
        freeze_sha = _sha256(freeze_bytes)

        if classification == "reuse":
            assert recovered is not None
            prepared = recovered
        else:
            prepared = _prepared(
                context,
                effect=EFFECT_FREEZE,
                operation_id=operation_id,
                input_path=imports["result_path"],
                input_sha=validated.result_sha256,
                expected_pre=GENESIS,
                probe=_probe_lane_artifact(
                    lanes_directory / "lane-freeze.json", freeze_sha
                ),
                issue_id=issue_id,
                epoch=epoch,
            )
            context.journal.append(prepared, hook=context.crash_hook)

        if artifact_bytes is not None:
            state.atomic_write(
                artifact_path,
                artifact_bytes,
                root=context.run_directory,
                max_bytes=state.MAX_RUN_BYTES,
                hook=context.crash_hook,
            )
        state.atomic_write(
            lanes_directory / "lane-freeze.json",
            freeze_bytes,
            root=context.run_directory,
            max_bytes=state.MAX_MANIFEST_BYTES,
            hook=context.crash_hook,
        )
        readback = (lanes_directory / "lane-freeze.json").read_bytes()
        if readback != freeze_bytes:
            context.journal.append(
                _resolution(
                    prepared,
                    status="UNKNOWN",
                    observed_sha=None,
                    evidence_path=None,
                    evidence_sha=None,
                ),
                hook=context.crash_hook,
            )
            raise IntegrationError("LANE_FREEZE_READBACK_UNKNOWN", status="unknown")
        context.journal.append(
            _resolution(
                prepared,
                status="APPLIED",
                observed_sha=freeze_sha,
                evidence_path=lanes_directory / "lane-freeze.json",
                evidence_sha=freeze_sha,
            ),
            hook=context.crash_hook,
        )
        fresh = context.checkpoints.current(rebuild_pointer=True)
        _update_one_issue(
            context,
            fresh,
            issue_id,
            lambda e: _entry_packaged(e, packet, validated, freeze_sha),
        )
        return _result(
            "success",
            freeze_sha256=freeze_sha,
            artifact_path=str(artifact_path),
            candidate_tree_sha256=freeze["candidate_tree_sha256"],
        )


def _artifact_invalid(entry: dict[str, Any]) -> None:
    entry["artifact"]["state"] = "invalid"


def _entry_packaged(
    entry: dict[str, Any], packet: Any, validated: Any, freeze_sha: str
) -> None:
    entry["artifact"]["state"] = "packaged"
    entry["artifact"]["lane_freeze_sha256"] = freeze_sha
    entry["worker_result"]["state"] = "accepted"
    entry["worker_result"]["outcome"] = validated.value["status"]
    entry["worker_result"]["record_sha256"] = validated.result_sha256
    entry["packet_sha256"] = packet.packet_sha256
    entry["result_sha256"] = validated.result_sha256
    entry["worktree"] = packet.value["repository"]["worktree"]
    entry["branch"] = packet.value["repository"]["branch"]
    entry["base_sha"] = packet.value["repository"]["base_sha"]
    entry["head_sha"] = validated.value["repository"]["head_sha"]
    entry["attempt"]["attempt_id"] = packet.value["attempt_id"]


def _freeze_identity(
    packet: Any, validated: Any, issue_id: str, epoch: int
) -> dict[str, Any]:
    return {
        "run_id": packet.value["run_id"],
        "issue_id": issue_id,
        "attempt_id": packet.value["attempt_id"],
        "ownership_epoch": epoch,
        "worker_result_sha256": validated.result_sha256,
    }


def _build_patch_freeze(
    context: RunContext,
    packet: Any,
    validated: Any,
    issue_id: str,
    epoch: int,
    lanes_directory: Path,
) -> tuple[dict[str, Any], bytes, Path]:
    artifact_path = lanes_directory / "lane-package.tar"
    try:
        built = package_lane.build_lane_package(
            Path(packet.value["repository"]["worktree"]),
            packet.value["repository"]["base_sha"],
            outbox=Path(packet.value["verification"]["worker_outbox"]),
            scope={
                "allowed_paths": packet.value["scope"]["allowed_paths"],
                "forbidden_paths": packet.value["scope"]["forbidden_paths"],
            },
            packet_budgets=packet.value["verification"],
            identity={
                **_freeze_identity(packet, validated, issue_id, epoch),
                "artifact_path": str(artifact_path),
            },
        )
    except package_lane.LanePackageError as exc:
        if exc.code in {"LANE_SCOPE_ESCAPE", "LANE_FILE_FORBIDDEN"}:
            raise IntegrationError("LANE_SCOPE_ESCAPE", status="conflict") from None
        if exc.code == "LANE_FREEZE_EMPTY_INVENTORY":
            raise IntegrationError("LANE_FREEZE_EMPTY_INVENTORY") from None
        if exc.code == "LANE_REPRODUCTION_FAILED":
            raise IntegrationError(
                "LANE_FREEZE_UNREPRODUCIBLE", status="conflict"
            ) from None
        raise IntegrationError("LANE_FREEZE_REFUSED") from None
    return dict(built.lane_freeze), built.archive_bytes, artifact_path


def _normalize_commit_freeze(
    context: RunContext,
    packet: Any,
    validated: Any,
    snapshot: lane_snapshot.LaneSnapshot,
    issue_id: str,
    epoch: int,
    lanes_directory: Path,
) -> tuple[dict[str, Any], bytes, Path]:
    """Normalize the already-validated worker commit without repackaging."""
    frozen = validated.value["worker_frozen_artifact"]
    if frozen is None or frozen["type"] != "commit":
        raise IntegrationError("LANE_FREEZE_ARTIFACT_INVALID", status="conflict")
    descriptor_path = lanes_directory / "commit-artifact.json"
    descriptor = {
        "schema_version": "beads.lane-freeze-commit.v1",
        "commit": frozen["identity"],
        "tree_sha256": frozen["tree_sha"],
        "base_sha": packet.value["repository"]["base_sha"],
        "worktree": packet.value["repository"]["worktree"],
    }
    freeze = {
        "schema_version": "beads.lane-freeze.v1",
        **_freeze_identity(packet, validated, issue_id, epoch),
        "transfer_mode": "commit",
        "base_sha": packet.value["repository"]["base_sha"],
        "observed_head_sha": validated.value["repository"]["head_sha"],
        "artifact_path": str(descriptor_path),
        "artifact_sha256": _sha256(_canonical(descriptor)),
        "candidate_tree_sha256": frozen["tree_sha"],
        "inventory": [dict(item) for item in snapshot.inventory],
        "packaging_tool_version": package_lane.PACKAGING_TOOL_VERSION,
        "reproduction_status": "verified_identity",
    }
    return dict(freeze), _canonical(descriptor), descriptor_path


def _normalize_export_freeze(
    context: RunContext,
    packet: Any,
    validated: Any,
    snapshot: lane_snapshot.LaneSnapshot,
    issue_id: str,
    epoch: int,
    lanes_directory: Path,
) -> tuple[dict[str, Any], bytes, Path]:
    """Normalize the already-validated external export package."""
    frozen = validated.value["worker_frozen_artifact"]
    if frozen is None or frozen["type"] != "external_export":
        raise IntegrationError("LANE_FREEZE_ARTIFACT_INVALID", status="conflict")
    source = Path(frozen["path"])
    try:
        export_bytes = source.read_bytes()
    except OSError as exc:
        raise IntegrationError("LANE_FREEZE_ARTIFACT_INVALID") from exc
    artifact_path = lanes_directory / "external-export.tar"
    freeze = {
        "schema_version": "beads.lane-freeze.v1",
        **_freeze_identity(packet, validated, issue_id, epoch),
        "transfer_mode": "external_export",
        "base_sha": packet.value["repository"]["base_sha"],
        "observed_head_sha": validated.value["repository"]["head_sha"],
        "artifact_path": str(artifact_path),
        "artifact_sha256": _sha256(export_bytes),
        "candidate_tree_sha256": frozen["tree_sha"],
        "inventory": [dict(item) for item in snapshot.inventory],
        "packaging_tool_version": package_lane.PACKAGING_TOOL_VERSION,
        "reproduction_status": "verified_identity",
    }
    return dict(freeze), export_bytes, artifact_path


# ---------------------------------------------------------------------------
# verify_lane / record_review
# ---------------------------------------------------------------------------

_HIGH_RISK = (
    "security",
    "auth",
    "permission",
    "schema",
    "migration",
    "concurrency",
    "atomic",
    "secret",
    "token",
    "crypto",
)


def _lane_requires_review(changed_paths: list[str]) -> bool:
    if not changed_paths:
        return False
    if all(path.endswith(".md") for path in changed_paths):
        return False
    return any(
        token in path.casefold() for path in changed_paths for token in _HIGH_RISK
    )


def _rerun_commands(
    worktree: Path, commands: tuple[tuple[str, ...], ...], logs_directory: Path
) -> list[dict[str, Any]]:
    logs_directory.mkdir(parents=True, exist_ok=True)
    outcomes: list[dict[str, Any]] = []
    for index, argv in enumerate(commands):
        stdout_log = logs_directory / f"command-{index:03d}.stdout.log"
        stderr_log = logs_directory / f"command-{index:03d}.stderr.log"
        spec = safe_output.CommandSpec(
            profile="verification",
            argv=tuple(argv),
            cwd=worktree,
            stdin_bytes=None,
            timeout_seconds=GIT_TIMEOUT * 10,
            stdout_limit_bytes=1_048_576,
            stderr_limit_bytes=1_048_576,
            total_limit_bytes=2_097_152,
            output_codec="utf8_text",
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
        result, _ = safe_output.run_command(
            spec, sensitive=safe_output.SensitiveSet(())
        )
        entry = {
            "command": json.dumps(list(argv), separators=(",", ":")),
            "exit_code": result.exit_code,
            "log_path": str(stdout_log),
            "log_sha256": _sha256(stdout_log.read_bytes())
            if stdout_log.exists()
            else None,
        }
        outcomes.append(entry)
    return outcomes


def verify_lane(context: RunContext, issue_id: str) -> dict[str, Any]:
    """Independently verify a frozen lane and emit review actions."""
    with state.exclusive_lock(
        context.run_directory / "run.lock",
        root=context.run_directory,
        hook=context.crash_hook,
    ):
        current = context.checkpoints.current(rebuild_pointer=True)
        entry = _issue_entry(current, issue_id)
        if entry["artifact"]["state"] != "packaged":
            raise IntegrationError("LANE_NOT_PACKAGED", status="conflict")
        freeze_sha = entry["artifact"]["lane_freeze_sha256"]
        lanes_directory = context.run_directory / "lanes" / _issue_key(issue_id)
        freeze_bytes = state.validate_owner_file(
            lanes_directory / "lane-freeze.json", root=context.run_directory
        ).read_bytes()
        imports_directory, imports = _load_imports(context, issue_id)
        packet, validated = _validate_attempt(
            context, issue_id, imports, entry, entry["ownership"]["epoch"]
        )
        freeze = validate_lane_freeze.validate_lane_freeze(
            freeze_bytes,
            result=validated,
            packet=packet,
            ownership_epoch=entry["ownership"]["epoch"],
            worktree_live=True,
        )
        if freeze.freeze_sha256 != freeze_sha:
            raise IntegrationError("LANE_FREEZE_STALE", status="conflict")
        worktree = Path(packet.value["repository"]["worktree"])
        rerun = _rerun_commands(
            worktree,
            packet.required_commands,
            lanes_directory / "verification",
        )
        completed = validated.value["status"] == "completed"
        commands_passed = all(item["exit_code"] == 0 for item in rerun)
        disposition = (
            "accept"
            if completed and commands_passed
            else "reject"
            if completed
            else "inconclusive"
        )
        record = {
            "schema_version": "beads.parent-verification.v1",
            "run_id": context.run_id,
            "attempt_id": packet.value["attempt_id"],
            "issue_id": issue_id,
            "ownership_epoch": entry["ownership"]["epoch"],
            "worker_result_sha256": validated.result_sha256,
            "lane_freeze_sha256": freeze_sha,
            "lane_review_sha256": None,
            "observed_changed_paths": validated.value["changes"]["paths"],
            "commands": rerun,
            "acceptance_evidence": [
                {
                    "acceptance_id": item["acceptance_id"],
                    "status": item["status"],
                    "evidence_paths": item["evidence_paths"],
                }
                for item in validated.value["acceptance_evidence"]
            ],
            "failure_classification": "none" if commands_passed else "introduced",
            "coverage_gaps": [] if commands_passed else ["REQUIRED_COMMAND_FAILED"],
            "disposition": disposition,
        }
        record_path = lanes_directory / "parent-verification.json"
        state.atomic_write(
            record_path,
            _canonical(record),
            root=context.run_directory,
            max_bytes=state.MAX_MANIFEST_BYTES,
            hook=context.crash_hook,
        )
        requires_review = _lane_requires_review(validated.value["changes"]["paths"])
        if disposition != "accept":
            _update_one_issue(
                context,
                context.checkpoints.current(rebuild_pointer=True),
                issue_id,
                lambda e: e["verification"].update(
                    {
                        "state": "failed",
                        "record_sha256": _sha256(_canonical(record)),
                    }
                ),
            )
            return _failure("LANE_VERIFICATION_FAILED", record_path=str(record_path))
        if requires_review:
            _update_one_issue(
                context,
                context.checkpoints.current(rebuild_pointer=True),
                issue_id,
                lambda e: e.update(
                    {
                        "verification": {
                            "state": "passed",
                            "record_sha256": _sha256(_canonical(record)),
                        },
                        "review": {"state": "required", "record_sha256": None},
                        "artifact": {
                            "state": "verified",
                            "lane_freeze_sha256": freeze_sha,
                        },
                    }
                ),
            )
            return _result(
                "partial",
                pending_actions=[
                    {
                        "action": "review_request",
                        "target_kind": "lane_freeze",
                        "target_sha256": freeze_sha,
                        "reason": "lane_risk_matrix_high_risk",
                    }
                ],
                verification_path=str(record_path),
            )
        _update_one_issue(
            context,
            context.checkpoints.current(rebuild_pointer=True),
            issue_id,
            lambda e: e.update(
                {
                    "verification": {
                        "state": "passed",
                        "record_sha256": _sha256(_canonical(record)),
                    },
                    "review": {"state": "not_required", "record_sha256": None},
                    "artifact": {
                        "state": "verified",
                        "lane_freeze_sha256": freeze_sha,
                    },
                }
            ),
        )
        return _result("success", verification_path=str(record_path))


def record_review(
    context: RunContext, issue_id: str, review_path: Path
) -> dict[str, Any]:
    """Validate one reviewer result against the frozen target."""
    with state.exclusive_lock(
        context.run_directory / "run.lock",
        root=context.run_directory,
        hook=context.crash_hook,
    ):
        current = context.checkpoints.current(rebuild_pointer=True)
        entry = _issue_entry(current, issue_id)
        freeze_sha = entry["artifact"]["lane_freeze_sha256"]
        if freeze_sha is None:
            raise IntegrationError("LANE_NOT_PACKAGED", status="conflict")
        try:
            review_bytes = state.validate_owner_file(
                Path(review_path), root=context.run_directory
            ).read_bytes()
        except (OSError, state.StateError) as exc:
            raise IntegrationError("REVIEW_RECORD_INVALID") from exc
        try:
            review = schema_runtime.strict_json_loads(
                review_bytes, max_bytes=state.MAX_MANIFEST_BYTES
            )
        except schema_runtime.JsonLoadFailure:
            raise IntegrationError("REVIEW_RECORD_INVALID") from None
        findings = schema_runtime.validate_instance(
            "reviewer-result-v1.schema.json", review
        )
        if findings:
            raise IntegrationError("REVIEW_RECORD_INVALID")
        if (
            review["run_id"] != context.run_id
            or review["target_kind"] not in {"lane_freeze", "combined_candidate"}
            or not review["independent"]
        ):
            raise IntegrationError("REVIEW_RECORD_INVALID")
        if (
            review["target_kind"] == "lane_freeze"
            and review["target_sha256"] != freeze_sha
        ):
            raise IntegrationError("REVIEW_TARGET_STALE", status="conflict")
        reviewer = review["reviewer_id"]
        if reviewer in set(review["implementer_ids"]):
            raise IntegrationError("REVIEW_NOT_INDEPENDENT", status="conflict")
        verdict = review["verdict"]
        record_sha = _sha256(review_bytes)
        _update_one_issue(
            context,
            context.checkpoints.current(rebuild_pointer=True),
            issue_id,
            lambda e: e["review"].update(
                {
                    "state": "passed" if verdict == "pass" else "failed",
                    "record_sha256": record_sha,
                }
            ),
        )
        if verdict != "pass":
            return _failure("REVIEW_FAILED", verdict=verdict)
        return _result("success", verdict=verdict, record_sha256=record_sha)


def record_combined_review(context: RunContext, review_path: Path) -> dict[str, Any]:
    """Validate and store the mandatory combined-candidate reviewer result."""
    with state.exclusive_lock(
        context.run_directory / "run.lock",
        root=context.run_directory,
        hook=context.crash_hook,
    ):
        review_bytes = Path(review_path).read_bytes()
        try:
            review = schema_runtime.strict_json_loads(
                review_bytes, max_bytes=state.MAX_MANIFEST_BYTES
            )
        except schema_runtime.JsonLoadFailure:
            raise IntegrationError("CANDIDATE_REVIEW_INVALID") from None
        findings = schema_runtime.validate_instance(
            "reviewer-result-v1.schema.json", review
        )
        if findings:
            raise IntegrationError("CANDIDATE_REVIEW_INVALID")
        if (
            review["run_id"] != context.run_id
            or review["target_kind"] != "combined_candidate"
            or not review["independent"]
            or review["reviewer_id"] in set(review["implementer_ids"])
        ):
            raise IntegrationError("CANDIDATE_REVIEW_INVALID")
        candidates_root = context.run_directory / "candidates"
        matched = None
        if candidates_root.is_dir():
            for directory in candidates_root.iterdir():
                record_path = directory / "candidate.json"
                if not record_path.is_file():
                    continue
                if _sha256(record_path.read_bytes()) == review["target_sha256"]:
                    matched = directory
        if matched is None:
            raise IntegrationError("CANDIDATE_REVIEW_TARGET_STALE", status="conflict")
        state.atomic_write(
            matched / "combined-review.json",
            _canonical(review),
            root=context.run_directory,
            max_bytes=state.MAX_MANIFEST_BYTES,
            hook=context.crash_hook,
        )
        if review["verdict"] != "pass":
            return _failure("CANDIDATE_REVIEW_FAILED", verdict=review["verdict"])
        return _result("success", verdict=review["verdict"])


# ---------------------------------------------------------------------------
# build_candidate / apply_candidate
# ---------------------------------------------------------------------------


def _candidate_directory(context: RunContext, candidate_id: str) -> Path:
    return context.run_directory / "candidates" / candidate_id


def _lane_freeze_record(context: RunContext, freeze_sha: str) -> dict[str, Any]:
    for path in (context.run_directory / "lanes").iterdir():
        freeze_path = path / "lane-freeze.json"
        if freeze_path.is_file():
            raw = freeze_path.read_bytes()
            if _sha256(raw) == freeze_sha:
                value = schema_runtime.strict_json_loads(
                    raw, max_bytes=state.MAX_MANIFEST_BYTES
                )
                return value
    raise IntegrationError("LANE_FREEZE_MISSING", status="conflict")


def _primary_repository(context: RunContext) -> Path:
    return Path(context.manifest["repository_root"])


def _primary_head(context: RunContext) -> str:
    return _git(_primary_repository(context), "rev-parse", "HEAD").decode().strip()


def build_candidate(
    context: RunContext,
    *,
    lane_freeze_sha256s: list[str],
    expected_predecessor: str | None = None,
) -> dict[str, Any]:
    """Build a disposable combined candidate from verified lane freezes."""
    with state.exclusive_lock(
        context.run_directory / "run.lock",
        root=context.run_directory,
        hook=context.crash_hook,
    ):
        classification, recovered = _recover_candidate(context)
        if classification == "applied":
            return recovered
        if classification == "conflict":
            return recovered
        current = context.checkpoints.current(rebuild_pointer=True)
        ordered = sorted(set(lane_freeze_sha256s))
        if not ordered:
            raise IntegrationError("CANDIDATE_LANES_EMPTY")
        repository = _primary_repository(context)
        predecessor = _primary_head(context)
        if expected_predecessor is not None and expected_predecessor != predecessor:
            raise IntegrationError("CANDIDATE_PREDECESSOR_MISMATCH", status="conflict")
        issues: list[str] = []
        for freeze_sha in ordered:
            entry = _find_issue_by_freeze(current, freeze_sha)
            if entry is None:
                raise IntegrationError("LANE_FREEZE_MISSING", status="conflict")
            if entry["artifact"]["state"] != "verified":
                raise IntegrationError("LANE_NOT_VERIFIED", status="conflict")
            if entry["review"]["state"] not in {"not_required", "passed"}:
                raise IntegrationError("LANE_REVIEW_REQUIRED", status="conflict")
        issues = [
            issue_id
            for issue_id, entry in current.value["issues"].items()
            if entry["artifact"].get("lane_freeze_sha256") in ordered
        ]
        candidate_id = _sha256(
            _canonical({"predecessor": predecessor, "lanes": ordered})
        )[:16]
        directory = _candidate_directory(context, candidate_id)
        state.ensure_owner_directory(directory, root=context.run_directory)
        existing_record = directory / "candidate.json"
        if existing_record.is_file():
            # Idempotent replay: this exact candidate already exists.
            record = schema_runtime.strict_json_loads(
                existing_record.read_bytes(), max_bytes=state.MAX_MANIFEST_BYTES
            )
            if (
                record["lane_freeze_sha256s"] != ordered
                or record["expected_predecessor"] != predecessor
            ):
                raise IntegrationError("CANDIDATE_EXISTS", status="conflict")
            return _result(
                "success",
                candidate_id=candidate_id,
                candidate_tree_sha256=record["candidate_tree_sha256"],
                tests_passed=all(item["exit_code"] == 0 for item in record["tests"]),
                review_required=record["review_required"],
                replayed=True,
            )
        inventory_paths: list[str] = []
        for freeze_sha in ordered:
            freeze = _lane_freeze_record(context, freeze_sha)
            inventory_paths.extend(item["path"] for item in freeze["inventory"])
        review_required = len(ordered) >= 2 or _lane_requires_review(inventory_paths)
        input_record = {
            "schema_version": "beads.candidate-build-input.v1",
            "expected_predecessor": predecessor,
            "lane_freeze_sha256s": ordered,
        }
        input_path = directory / "candidate-input.json"
        input_bytes = _canonical(input_record)
        state.atomic_write(
            input_path,
            input_bytes,
            root=context.run_directory,
            max_bytes=state.MAX_MANIFEST_BYTES,
            hook=context.crash_hook,
        )
        tree_dir = directory / "tree"
        if tree_dir.exists():
            raise IntegrationError("CANDIDATE_TREE_EXISTS", status="conflict")
        try:
            completed = subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(tree_dir),
                    predecessor,
                ],
                cwd=repository,
                capture_output=True,
                timeout=GIT_TIMEOUT,
            )
            if completed.returncode != 0:
                raise IntegrationError("CANDIDATE_WORKTREE_FAILED")
            applied = _apply_lanes(context, tree_dir, ordered)
            candidate_tree = _write_tree(tree_dir)
            tests = _run_candidate_tests(context, tree_dir, issues)
        except IntegrationError:
            _update_all(
                context,
                issues,
                lambda e: e["integration"].update({"state": "failed"}),
            )
            raise
        record = {
            "schema_version": "beads.integration-candidate.v1",
            "candidate_id": candidate_id,
            "expected_predecessor": predecessor,
            "candidate_tree_sha256": candidate_tree,
            "lane_freeze_sha256s": ordered,
            "issue_ids": issues,
            "applied_lanes": applied,
            "tests": tests,
            "review_required": review_required,
            "built_at": utc_now(),
        }
        record_bytes = _canonical(record)
        record_path = directory / "candidate.json"
        operation_id = state.semantic_operation_id(
            {
                "schema": "beads.candidate-build-input.v1",
                "effect_type": EFFECT_CANDIDATE,
                "target_identity": candidate_id,
                "immutable_input_sha256": _sha256(input_bytes),
                "ownership_epoch": None,
            }
        )
        if classification == "reuse":
            prepared = recovered
        else:
            prepared = _prepared(
                context,
                effect=EFFECT_CANDIDATE,
                operation_id=operation_id,
                input_path=input_path,
                input_sha=_sha256(input_bytes),
                expected_pre=GENESIS,
                probe=_probe_lane_artifact(record_path, _sha256(record_bytes)),
                issue_id=None,
                epoch=None,
            )
            context.journal.append(prepared, hook=context.crash_hook)
        state.atomic_write(
            record_path,
            record_bytes,
            root=context.run_directory,
            max_bytes=state.MAX_MANIFEST_BYTES,
            hook=context.crash_hook,
        )
        observed = _sha256(record_path.read_bytes())
        if observed != _sha256(record_bytes):
            context.journal.append(
                _resolution(
                    prepared,
                    status="UNKNOWN",
                    observed_sha=observed,
                    evidence_path=record_path,
                    evidence_sha=observed,
                ),
                hook=context.crash_hook,
            )
            raise IntegrationError("CANDIDATE_READBACK_UNKNOWN", status="unknown")
        context.journal.append(
            _resolution(
                prepared,
                status="APPLIED",
                observed_sha=observed,
                evidence_path=record_path,
                evidence_sha=observed,
            ),
            hook=context.crash_hook,
        )
        tests_passed = all(item["exit_code"] == 0 for item in record["tests"])
        integration_state = "candidate_built" if tests_passed else "failed"
        _update_all(
            context,
            issues,
            lambda e: e["integration"].update(
                {
                    "state": integration_state,
                    "candidate_sha256": _sha256(record_bytes),
                    "event_id": _sha256(record_bytes),
                }
            ),
        )
        result = _result(
            "success",
            candidate_id=candidate_id,
            candidate_tree_sha256=candidate_tree,
            candidate_record_sha256=_sha256(record_bytes),
            tests_passed=tests_passed,
            review_required=review_required,
        )
        if tests_passed and review_required:
            result["status"] = "partial"
            result["pending_actions"] = [
                {
                    "action": "review_request",
                    "target_kind": "combined_candidate",
                    "target_sha256": _sha256(record_bytes),
                    "reason": "cross_issue_combined_candidate",
                }
            ]
        if not tests_passed:
            result["status"] = "refused"
            result["error_code"] = "CANDIDATE_TESTS_FAILED"
        return result


def _find_issue_by_freeze(
    current: state.CheckpointRef, freeze_sha: str
) -> dict[str, Any] | None:
    for entry in current.value["issues"].values():
        if entry["artifact"].get("lane_freeze_sha256") == freeze_sha:
            return entry
    return None


def _update_all(
    context: RunContext,
    issue_ids: list[str],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    current = context.checkpoints.current(rebuild_pointer=True)
    value = copy.deepcopy(current.value)
    for issue_id in issue_ids:
        entry = value["issues"].get(issue_id)
        if entry is None:
            raise IntegrationError("ISSUE_NOT_IN_RUN")
        mutate(entry)
    value["generation"] = current.generation + 1
    value["previous_checkpoint_sha256"] = current.generation_sha256
    value["created_at"] = utc_now()
    context.checkpoints.accept(value, hook=context.crash_hook)


def _apply_lanes(context: RunContext, tree_dir: Path, ordered: list[str]) -> list[str]:
    # Ambiguous overlap between lanes stops without auto-resolution: the
    # same candidate path with different content in two lanes is a typed
    # conflict before any candidate mutation.
    seen: dict[str, tuple[str, str]] = {}
    for freeze_sha in ordered:
        freeze = _lane_freeze_record(context, freeze_sha)
        archive = Path(freeze["artifact_path"])
        with open(archive, "rb") as stream:
            manifest = package_lane.verify_package(stream.read())
        for entry in manifest["candidate_entries"]:
            relative = entry["path"]
            if entry["kind"] == "deleted":
                continue
            previous = seen.get(relative)
            if previous is not None and previous[0] != entry["sha256"]:
                raise IntegrationError("CANDIDATE_CONFLICT", status="conflict")
            seen[relative] = (entry["sha256"], freeze_sha)
    applied: list[str] = []
    for freeze_sha in ordered:
        freeze = _lane_freeze_record(context, freeze_sha)
        mode = freeze["transfer_mode"]
        if mode == "patch_package":
            archive = Path(freeze["artifact_path"])
            _apply_patch_package(tree_dir, archive)
        elif mode == "commit":
            completed = subprocess.run(
                ["git", "cherry-pick", freeze["observed_head_sha"]],
                cwd=tree_dir,
                capture_output=True,
                timeout=GIT_TIMEOUT,
            )
            if completed.returncode != 0:
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=tree_dir,
                    capture_output=True,
                    timeout=GIT_TIMEOUT,
                )
                raise IntegrationError("CANDIDATE_CONFLICT", status="conflict")
        elif mode == "external_export":
            _apply_patch_package(tree_dir, Path(freeze["artifact_path"]))
        else:  # pragma: no cover
            raise IntegrationError("CANDIDATE_CONFLICT", status="conflict")
        applied.append(freeze_sha)
    return applied


def _apply_patch_package(tree_dir: Path, archive: Path) -> None:
    with open(archive, "rb") as stream:
        manifest = package_lane.verify_package(stream.read())
    raw = archive.read_bytes()
    import io as _io
    import tarfile

    with tarfile.open(fileobj=_io.BytesIO(raw)) as tar:
        diff_stream = tar.extractfile("tracked.diff")
        assert diff_stream is not None
        diff = diff_stream.read()
        for entry in manifest["candidate_entries"]:
            relative = entry["path"]
            target = tree_dir.joinpath(*relative.split("/"))
            if entry["kind"] == "deleted":
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "symlink":
                info = tar.getmember(f"symlink/{relative}")
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.symlink_to(info.linkname)
                continue
            blob_stream = tar.extractfile(f"blob/{relative}")
            assert blob_stream is not None
            data = blob_stream.read()
            with open(target, "wb") as stream2:
                stream2.write(data)
            os.chmod(target, entry["mode"] & 0o7777)
    if diff.strip():
        completed = subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            cwd=tree_dir,
            input=diff,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        if completed.returncode != 0:
            raise IntegrationError("CANDIDATE_CONFLICT", status="conflict")


def _write_tree(tree_dir: Path) -> str:
    for args in (("add", "-A"), ("write-tree",)):
        completed = subprocess.run(
            ["git", *args],
            cwd=tree_dir,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        if completed.returncode != 0:
            raise IntegrationError("CANDIDATE_TREE_FAILED")
    return completed.stdout.decode().strip()


def _run_candidate_tests(
    context: RunContext,
    tree_dir: Path,
    issue_ids: list[str],
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    logs_directory = tree_dir / "candidate-logs"
    logs_directory.mkdir(mode=0o700, exist_ok=True)
    logs_directory.chmod(0o700)
    for issue_index, issue_id in enumerate(issue_ids):
        imports_directory, imports = _load_imports(context, issue_id)
        packet = validate_worker_packet.validate_packet(
            imports["packet_bytes"], expected_packet_sha256=None
        )
        for command_index, argv in enumerate(packet.required_commands):
            stdout_log = (
                logs_directory
                / f"issue-{issue_index:03d}-cmd-{command_index:03d}.stdout.log"
            )
            spec = safe_output.CommandSpec(
                profile="verification",
                argv=tuple(argv),
                cwd=tree_dir,
                stdin_bytes=None,
                timeout_seconds=GIT_TIMEOUT * 10,
                stdout_limit_bytes=1_048_576,
                stderr_limit_bytes=1_048_576,
                total_limit_bytes=2_097_152,
                output_codec="utf8_text",
                stdout_log=stdout_log,
                stderr_log=(
                    logs_directory
                    / f"issue-{issue_index:03d}-cmd-{command_index:03d}.stderr.log"
                ),
            )
            result, _ = safe_output.run_command(
                spec, sensitive=safe_output.SensitiveSet(())
            )
            outcomes.append(
                {
                    "issue_id": issue_id,
                    "command": json.dumps(list(argv), separators=(",", ":")),
                    "exit_code": result.exit_code,
                }
            )
    return outcomes


def _recover_candidate(context: RunContext) -> tuple[str, Any]:
    """Returns ("none"|"applied"|"conflict"|"reuse", payload)."""
    prepared = _dangling_prepared(context, EFFECT_CANDIDATE)
    if prepared is None:
        return ("none", None)
    probe = prepared["recovery_probe"]
    path = Path(probe["intended_after_identity"])
    if path.is_file():
        observed = _sha256(path.read_bytes())
        if observed == probe["intended_after_sha256"]:
            context.journal.append(
                _resolution(
                    prepared,
                    status="APPLIED",
                    observed_sha=observed,
                    evidence_path=path,
                    evidence_sha=observed,
                ),
                hook=context.crash_hook,
            )
            return ("applied", _result("success", recovered=True))
        context.journal.append(
            _resolution(
                prepared,
                status="CONFLICT",
                observed_sha=observed,
                evidence_path=None,
                evidence_sha=None,
            ),
            hook=context.crash_hook,
        )
        return (
            "conflict",
            _failure("CANDIDATE_RECOVERY_CONFLICT", status="conflict"),
        )
    # No candidate record: the effect never landed; reuse in-flight.
    return ("reuse", prepared)


def apply_candidate(
    context: RunContext,
    candidate_id: str,
    *,
    expected_predecessor: str,
) -> dict[str, Any]:
    """Apply the exact reviewed candidate to the primary checkout."""
    with state.exclusive_lock(
        context.run_directory / "run.lock",
        root=context.run_directory,
        hook=context.crash_hook,
    ):
        classification, recovered = _recover_apply(context, expected_predecessor)
        if classification == "applied":
            return recovered
        directory = _candidate_directory(context, candidate_id)
        record_path = directory / "candidate.json"
        if not record_path.is_file():
            raise IntegrationError("CANDIDATE_MISSING", status="conflict")
        record_bytes = record_path.read_bytes()
        record = schema_runtime.strict_json_loads(
            record_bytes, max_bytes=state.MAX_MANIFEST_BYTES
        )
        if record["candidate_id"] != candidate_id:
            raise IntegrationError("CANDIDATE_IDENTITY_MISMATCH", status="conflict")
        if record["expected_predecessor"] != expected_predecessor:
            raise IntegrationError("CANDIDATE_PREDECESSOR_MISMATCH", status="conflict")
        if not all(item["exit_code"] == 0 for item in record["tests"]):
            raise IntegrationError("CANDIDATE_TESTS_FAILED", status="conflict")
        if record["review_required"]:
            review_path = directory / "combined-review.json"
            if not review_path.is_file():
                raise IntegrationError("CANDIDATE_REVIEW_REQUIRED", status="conflict")
            _validate_combined_review(context, review_path, record)
        repository = _primary_repository(context)
        observed_head = _primary_head(context)
        if observed_head != expected_predecessor:
            raise IntegrationError("PRIMARY_PREDECESSOR_MISMATCH", status="conflict")
        status_output = _git(repository, "status", "--porcelain")
        if status_output.strip():
            raise IntegrationError("PRIMARY_DIRTY", status="conflict")
        for issue_id in record["issue_ids"]:
            _ownership(context, issue_id)
        operation_id = state.semantic_operation_id(
            {
                "schema": "beads.primary-integration.v1",
                "effect_type": EFFECT_APPLY,
                "target_identity": candidate_id,
                "immutable_input_sha256": _sha256(record_bytes),
                "ownership_epoch": None,
            }
        )
        if classification == "reuse":
            prepared = recovered
        else:
            prepared = _prepared(
                context,
                effect=EFFECT_APPLY,
                operation_id=operation_id,
                input_path=record_path,
                input_sha=_sha256(record_bytes),
                expected_pre=_sha256(_canonical({"head": expected_predecessor})),
                probe=_probe_git_ref_tree(
                    f"primary:{repository}:HEAD",
                    _tree_state(record["candidate_tree_sha256"]),
                ),
                issue_id=None,
                epoch=None,
            )
            context.journal.append(prepared, hook=context.crash_hook)

        branch = _git(repository, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        commit = (
            subprocess.run(
                [
                    "git",
                    "commit-tree",
                    record["candidate_tree_sha256"],
                    "-p",
                    expected_predecessor,
                    "-m",
                    f"beads: apply candidate {candidate_id}",
                ],
                cwd=repository,
                capture_output=True,
                timeout=GIT_TIMEOUT,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{branch}", commit],
            cwd=repository,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=True,
        )
        # Materialize the exact candidate tree in the primary index and
        # worktree (the guarded primary mutation this operation exists for).
        subprocess.run(
            ["git", "reset", "--hard", commit],
            cwd=repository,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=True,
        )
        head_after = _primary_head(context)
        tree_after = _git(repository, "rev-parse", "HEAD^{tree}").decode().strip()
        status_after = _git(repository, "status", "--porcelain")
        exact = (
            head_after == commit
            and tree_after == record["candidate_tree_sha256"]
            and not status_after.strip()
        )
        if not exact:
            context.journal.append(
                _resolution(
                    prepared,
                    status="UNKNOWN",
                    observed_sha=_sha256(
                        _canonical({"head": head_after, "tree": tree_after})
                    ),
                    evidence_path=None,
                    evidence_sha=None,
                ),
                hook=context.crash_hook,
            )
            raise IntegrationError("PRIMARY_READBACK_UNKNOWN", status="unknown")
        observed_state = _sha256(_canonical({"head": head_after, "tree": tree_after}))
        context.journal.append(
            _resolution(
                prepared,
                status="APPLIED",
                observed_sha=observed_state,
                evidence_path=record_path,
                evidence_sha=_sha256(record_bytes),
            ),
            hook=context.crash_hook,
        )
        _update_all(
            context,
            record["issue_ids"],
            lambda e: e["integration"].update({"state": "primary_prepared"}),
        )
        _update_all(
            context,
            record["issue_ids"],
            lambda e: e["integration"].update({"state": "primary_integrated"}),
        )
        return _result(
            "success",
            applied_commit=commit,
            candidate_tree_sha256=tree_after,
        )


def _validate_combined_review(
    context: RunContext, review_path: Path, record: Mapping[str, Any]
) -> None:
    directory = review_path.parent
    review_bytes = review_path.read_bytes()
    try:
        review = schema_runtime.strict_json_loads(
            review_bytes, max_bytes=state.MAX_MANIFEST_BYTES
        )
    except schema_runtime.JsonLoadFailure:
        raise IntegrationError("CANDIDATE_REVIEW_INVALID") from None
    findings = schema_runtime.validate_instance(
        "reviewer-result-v1.schema.json", review
    )
    if findings:
        raise IntegrationError("CANDIDATE_REVIEW_INVALID")
    if (
        review["run_id"] != context.run_id
        or review["target_kind"] != "combined_candidate"
        or review["target_sha256"]
        != _sha256((directory / "candidate.json").read_bytes())
        or not review["independent"]
        or review["reviewer_id"] in set(review["implementer_ids"])
    ):
        raise IntegrationError("CANDIDATE_REVIEW_INVALID", status="conflict")
    if review["verdict"] != "pass":
        raise IntegrationError("CANDIDATE_REVIEW_FAILED", status="conflict")


def _tree_state(tree: str) -> str:
    """Canonical 64-hex digest of a primary tree identity (git trees are 40)."""
    return _sha256(_canonical({"tree": tree}))


def _recover_apply(context: RunContext, expected_predecessor: str) -> tuple[str, Any]:
    prepared = _dangling_prepared(context, EFFECT_APPLY)
    if prepared is None:
        return ("none", None)
    intended_tree = prepared["recovery_probe"]["intended_after_sha256"]
    repository = _primary_repository(context)
    head = _primary_head(context)
    tree = _git(repository, "rev-parse", "HEAD^{tree}").decode().strip()
    if _tree_state(tree) == intended_tree:
        status = "APPLIED"
    elif head == expected_predecessor:
        status = "NOT_APPLIED"
    else:
        status = "CONFLICT"
    if status == "APPLIED":
        context.journal.append(
            _resolution(
                prepared,
                status="APPLIED",
                observed_sha=_sha256(_canonical({"head": head, "tree": tree})),
                evidence_path=None,
                evidence_sha=None,
            ),
            hook=context.crash_hook,
        )
        return ("applied", _result("success", recovered=True))
    if status == "CONFLICT":
        context.journal.append(
            _resolution(
                prepared,
                status="CONFLICT",
                observed_sha=_sha256(_canonical({"head": head, "tree": tree})),
                evidence_path=None,
                evidence_sha=None,
            ),
            hook=context.crash_hook,
        )
        raise IntegrationError("PRIMARY_RECOVERY_CONFLICT", status="conflict")
    # NOT_APPLIED: the primary is still at the predecessor; reuse in-flight.
    return ("reuse", prepared)
