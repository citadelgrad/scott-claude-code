#!/usr/bin/env python3
"""Initial Task-8 coordinator surface: status and filesystem-only recover."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import coordinator_state as state
import operation_result
import reconcile_run


def _diagnostic(code: str, template: str) -> dict[str, Any]:
    return {
        "code": code,
        "template_id": template,
        "field_path": "/run",
        "parameters": [],
    }


def _result(
    run: Path, plan: reconcile_run.ReconciliationPlan, *, repaired: bool
) -> dict[str, Any]:
    manifest = state.load_run_manifest(run)
    healthy = plan.disposition in {"consistent", "safe_to_retry"}
    status = "success" if healthy else "inconclusive"
    code = None if healthy else "RECOVERY_MANUAL_DECISION_REQUIRED"
    requested = ["read", "local_write"] if repaired else ["read"]
    exercised = ["read", "local_write"] if plan.repairs else ["read"]
    checkpoint = (
        str((run / "checkpoints" / f"{plan.accepted_generation:06d}.json").resolve())
        if plan.accepted_generation is not None
        else None
    )
    value = {
        "schema_version": "beads.operation-result.v1",
        "operation": "recover",
        "status": status,
        "request_id": manifest["request_id"],
        "issue_ids": [manifest["root_issue_id"]],
        "root_issue_id": manifest["root_issue_id"],
        "workspace": {
            "repository_root": manifest["repository_root"],
            "workspace_sha256": state.sha256_bytes(manifest["workspace"].encode()),
            "cli_version": "1.2.2",
        },
        "observed_changes": [],
        "authority": {
            "requested": requested,
            "exercised": exercised,
            "readback_proven": exercised,
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
        "warnings": [_diagnostic("LOCAL_FENCING_ONLY", "local_fencing_limit")],
        "coverage_gaps": []
        if healthy
        else [
            _diagnostic("RECOVERY_MANUAL_DECISION_REQUIRED", "manual_reconciliation")
        ],
        "errors": [],
        "error_code": code,
        "safe_next_action": None
        if healthy
        else _diagnostic("INSPECT_PRESERVED_EVIDENCE", "inspect_evidence"),
        "run_id": manifest["run_id"],
        "checkpoint": checkpoint,
        "cancellation_reason": None,
    }
    return operation_result.validate_operation_result(value).value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "recover"):
        command = subcommands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        command.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        plan = (
            reconcile_run.status(args.run_dir)
            if args.command == "status"
            else reconcile_run.recover(args.run_dir)
        )
        value = _result(args.run_dir, plan, repaired=args.command == "recover")
    except reconcile_run.ReconciliationError as exc:
        print(
            json.dumps({"status": exc.status, "error_code": exc.code}, sort_keys=True)
        )
        return 4 if exc.status == "conflict" else 5
    except (state.StateError, operation_result.OperationResultError) as exc:
        code = getattr(exc, "code", str(exc))
        print(json.dumps({"status": "failed", "error_code": code}, sort_keys=True))
        return 1
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    if value["status"] == "success":
        return 0
    if value["status"] == "conflict":
        return 4
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
