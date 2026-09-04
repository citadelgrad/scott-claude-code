#!/usr/bin/env python3
"""Strict stateless validation for parent-owned lane-freeze records.

Re-derives everything from the freeze bytes, the validated worker result,
the validated packet, and (behind the explicit ``worktree_live`` recapture
path) the live lane.  No caller-supplied inventory or identity is ever
trusted.  Fails closed with typed refusals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import lane_snapshot
import package_lane
import schema_runtime
import validate_worker_execution_result
import validate_worker_packet

MAX_FREEZE_BYTES = 65_536


class LaneFreezeError(ValueError):
    """Typed fail-closed freeze refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedLaneFreeze:
    value: dict[str, Any]
    freeze_sha256: str


def _fail(code: str) -> None:
    raise LaneFreezeError(code)


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def validate_lane_freeze(
    freeze_bytes: bytes,
    *,
    result: validate_worker_execution_result.ValidatedWorkerExecutionResult,
    packet: validate_worker_packet.ValidatedWorkerPacket,
    ownership_epoch: int,
    worktree_live: bool = False,
) -> ValidatedLaneFreeze:
    """Validate one lane-freeze record against immutable identities."""
    if len(freeze_bytes) > MAX_FREEZE_BYTES:
        _fail("LANE_FREEZE_TOO_LARGE")
    try:
        value = schema_runtime.strict_json_loads(
            freeze_bytes, max_bytes=MAX_FREEZE_BYTES
        )
    except schema_runtime.JsonLoadFailure as exc:
        raise LaneFreezeError(str(exc)) from None
    if not isinstance(value, dict):
        _fail("LANE_FREEZE_SCHEMA_INVALID")
    findings = schema_runtime.validate_instance("lane-freeze-v1.schema.json", value)
    if findings:
        _fail("LANE_FREEZE_SCHEMA_INVALID")
    if freeze_bytes != _canonical(value):
        _fail("LANE_FREEZE_NOT_CANONICAL")

    # Identity: the freeze must bind exactly this run/issue/attempt/epoch/
    # worker result.
    expected_result = result.value
    if (
        value["run_id"] != expected_result["run_id"]
        or value["issue_id"] != expected_result["issue_id"]
        or value["attempt_id"] != expected_result["attempt_id"]
        or value["ownership_epoch"] != ownership_epoch
        or value["worker_result_sha256"] != result.result_sha256
    ):
        _fail("LANE_FREEZE_IDENTITY_MISMATCH")
    if value["transfer_mode"] != packet.value["scope"]["integration_mode"]:
        _fail("LANE_FREEZE_MODE_MISMATCH")
    if value["base_sha"] != packet.value["repository"]["base_sha"]:
        _fail("LANE_FREEZE_BASE_MISMATCH")
    if value["observed_head_sha"] != expected_result["repository"]["head_sha"]:
        _fail("LANE_FREEZE_HEAD_MISMATCH")

    # Accepted freezes must never claim failed/inconclusive reproduction.
    if value["reproduction_status"] not in {"reproduced", "verified_identity"}:
        _fail("LANE_FREEZE_REPRODUCTION")

    # Artifact identity is recomputed from bytes, never trusted.
    artifact_path = Path(value["artifact_path"])
    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError:
        _fail("LANE_FREEZE_ARTIFACT_INVALID")
    if (
        value["transfer_mode"] == "patch_package"
        and value["artifact_sha256"] != hashlib.sha256(artifact_bytes).hexdigest()
    ):
        _fail("LANE_FREEZE_ARTIFACT_INVALID")

    if value["transfer_mode"] == "patch_package":
        try:
            manifest = package_lane.verify_package(artifact_bytes)
        except package_lane.LanePackageError:
            _fail("LANE_FREEZE_ARTIFACT_INVALID")
        # Package identity binds to lane identity: a verbatim package from
        # another (co-frozen, same-head) lane must never validate here.
        manifest_identity = manifest.get("identity")
        if not isinstance(manifest_identity, dict) or any(
            manifest_identity.get(key) != value[key]
            for key in (
                "run_id",
                "issue_id",
                "attempt_id",
                "ownership_epoch",
                "worker_result_sha256",
            )
        ):
            _fail("LANE_FREEZE_IDENTITY_MISMATCH")
        if manifest["candidate_tree_sha256"] != value["candidate_tree_sha256"]:
            _fail("LANE_FREEZE_ARTIFACT_INVALID")
        if manifest["head_sha"] != value["observed_head_sha"]:
            _fail("LANE_FREEZE_HEAD_MISMATCH")
        inventory = [dict(item) for item in manifest["untracked_inventory"]]
    else:
        # commit/external_export: identity came from the already-validated
        # frozen artifact; the freeze record's inventory must still derive
        # from a live recapture of the lane as finalized.
        inventory = None

    if worktree_live:
        try:
            snapshot = lane_snapshot.capture(
                Path(packet.value["repository"]["worktree"]),
                packet.value["repository"]["base_sha"],
                exclude=Path(packet.value["verification"]["worker_outbox"]),
                max_untracked_file_bytes=lane_snapshot.budget_from_packet(packet.value),
            )
        except lane_snapshot.LaneSnapshotError:
            _fail("LANE_FREEZE_LANE_DRIFT")
        if (
            snapshot.head_sha != expected_result["repository"]["head_sha"]
            or snapshot.lane_state != expected_result["lane_state"]
            or list(snapshot.changed_paths) != expected_result["changes"]["paths"]
        ):
            _fail("LANE_FREEZE_LANE_DRIFT")
        if inventory is not None and [dict(e) for e in snapshot.inventory] != inventory:
            _fail("LANE_FREEZE_INVENTORY_MISMATCH")

    if inventory is not None and value["inventory"] != inventory:
        _fail("LANE_FREEZE_INVENTORY_MISMATCH")

    return ValidatedLaneFreeze(value, hashlib.sha256(freeze_bytes).hexdigest())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validate-lane-freeze")
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--worker-packet", type=Path, required=True)
    parser.add_argument("--expected-packet-sha256", required=True)
    parser.add_argument("--ownership-epoch", type=int, required=True)
    parser.add_argument("--worker-result", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    try:
        packet = validate_worker_packet.validate_packet(
            schema_runtime.read_bounded(
                args.worker_packet, validate_worker_packet.MAX_PACKET_BYTES
            ),
            expected_packet_sha256=args.expected_packet_sha256,
        )
        result = validate_worker_execution_result.validate_finalized_attempt(
            result_path=args.worker_result,
            receipt_path=args.receipt,
            packet=packet,
            ownership_epoch=args.ownership_epoch,
        )
        validated = validate_lane_freeze(
            schema_runtime.read_bounded(args.freeze, MAX_FREEZE_BYTES),
            result=result,
            packet=packet,
            ownership_epoch=args.ownership_epoch,
            worktree_live=args.live,
        )
        print(
            json.dumps(
                {
                    "status": "valid",
                    "freeze_sha256": validated.freeze_sha256,
                    "reproduction_status": validated.value["reproduction_status"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, TypeError, schema_runtime.JsonLoadFailure):
        print('{"error_code":"LANE_FREEZE_INVALID","status":"invalid"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
