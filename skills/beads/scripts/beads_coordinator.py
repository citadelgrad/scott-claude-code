#!/usr/bin/env python3
"""Thin public CLI over the frozen beads coordinator modules.

This dispatcher registers subcommands and sequences calls into the frozen
modules (coordinator_state, coordinator_integration, coordinator_tracker,
coordinator_front, coordinator_handoff, protected_action, direct_operation,
beads_ownership, reconcile_run). It does not reimplement any invariant those
modules already enforce: every subcommand parses CLI/JSON input, calls exactly
one frozen entry point, and folds the returned value or raised error into a
``beads.operation-result.v1`` envelope (or, for the original status/recover
surface, the exact legacy envelope those two subcommands have always used).
"""

from __future__ import annotations

import argparse
import functools
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).parent))
import beads_ownership
import coordinator_front
import coordinator_handoff
import coordinator_integration
import coordinator_state as state
import coordinator_tracker
import direct_operation
import operation_result
import protected_action
import reconcile_run

# ---------------------------------------------------------------------------
# Legacy Task-8 surface: status and filesystem-only recover. Unmodified from
# the original dispatcher so external callers see byte-identical output.
# ---------------------------------------------------------------------------


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
            "workspace_sha256": manifest["workspace_identity_sha256"],
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


def _run_status_recover(args: argparse.Namespace) -> int:
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


# ---------------------------------------------------------------------------
# Shared helpers for the new subcommand surface.
# ---------------------------------------------------------------------------

CLI_VERSION = "1.2.2"

# coordinator_integration / coordinator_state / beads_ownership / reconcile_run
# all raise or return a lowercase, operation_result-adjacent vocabulary, except
# for "refused" which is not a valid operation-result status.
_INTEGRATION_STATUS_MAP = {
    "refused": "blocked",
    "conflict": "conflict",
    "success": "success",
}

# direct_operation / protected_action / coordinator_handoff share direct_operation's
# uppercase four-way outcome taxonomy for both successful classifications and
# raised-error `.status` attributes. protected_action's HUMAN_ACTION_REQUIRED is
# already a valid, lowercase operation-result status and passes straight through.
_DIRECT_CLASSIFICATION_STATUS = {
    direct_operation.APPLIED: "success",
    direct_operation.NOT_APPLIED: "blocked",
    direct_operation.CONFLICT: "conflict",
    direct_operation.UNKNOWN: "inconclusive",
    protected_action.HUMAN_ACTION_REQUIRED: "human_action_required",
    "REFUSED": "blocked",
}


def _map_integration_status(raw_status: str) -> str:
    return _INTEGRATION_STATUS_MAP.get(raw_status, "blocked")


def _map_direct_status(raw_status: str) -> str:
    return _DIRECT_CLASSIFICATION_STATUS.get(raw_status, "blocked")


class _CliInputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _load_json_input(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _CliInputError("INPUT_FILE_UNREADABLE") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _CliInputError("INPUT_FILE_NOT_JSON") from exc
    if not isinstance(decoded, dict):
        raise _CliInputError("INPUT_FILE_NOT_OBJECT")
    return decoded


def _require_fields(data: Mapping[str, Any], fields: Sequence[str]) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise _CliInputError(f"INPUT_MISSING_FIELDS:{','.join(sorted(missing))}")


def _base_envelope(
    operation: str,
    manifest: Mapping[str, Any],
    *,
    issue_ids: Sequence[str] = (),
    requested: Sequence[str] = ("read",),
    exercised: Sequence[str] = ("read",),
) -> dict[str, Any]:
    return {
        "schema_version": "beads.operation-result.v1",
        "operation": operation,
        "status": "success",
        "request_id": manifest["request_id"],
        "issue_ids": list(issue_ids),
        "root_issue_id": manifest["root_issue_id"],
        "workspace": {
            "repository_root": manifest["repository_root"],
            "workspace_sha256": manifest["workspace_identity_sha256"],
            "cli_version": CLI_VERSION,
        },
        "observed_changes": [],
        "authority": {
            "requested": list(requested),
            "exercised": list(exercised),
            "readback_proven": list(exercised),
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
        "run_id": manifest["run_id"],
        "checkpoint": None,
        "cancellation_reason": None,
    }


def _build(candidate: dict[str, Any]) -> operation_result.OperationResult:
    return operation_result.build_operation_result(
        operation_result.OperationBuildInput(candidate=candidate)
    )


def _emit(result: operation_result.OperationResult, *, full: bool) -> int:
    sys.stdout.buffer.write(operation_result.encode_cli_output(result, full=full))
    return operation_result.result_exit_code(result)


# ---------------------------------------------------------------------------
# start-run
# ---------------------------------------------------------------------------

_START_RUN_FIELDS = (
    "request_id",
    "repository_root",
    "git_common_dir",
    "workspace",
    "run_root",
    "root_issue_id",
    "scope_issue_ids",
    "actor",
    "base_git_commit",
    "authority_snapshot_sha256",
    "workspace_identity_sha256",
)


def _handle_start_run(args: argparse.Namespace) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, _START_RUN_FIELDS)
    request = state.StartRunInput(
        request_id=data["request_id"],
        repository_root=data["repository_root"],
        git_common_dir=data["git_common_dir"],
        workspace=data["workspace"],
        run_root=data["run_root"],
        root_issue_id=data["root_issue_id"],
        scope_issue_ids=tuple(data["scope_issue_ids"]),
        actor=data["actor"],
        base_git_commit=data["base_git_commit"],
        authority_snapshot_sha256=data["authority_snapshot_sha256"],
        workspace_identity_sha256=data["workspace_identity_sha256"],
    )
    bootstrap = coordinator_tracker.start_run(
        request, actor=args.actor, now=direct_operation.utc_now()
    )
    healthy = bootstrap.disposition == "active"
    candidate = {
        "schema_version": "beads.operation-result.v1",
        "operation": "start_run",
        "status": "success" if healthy else "blocked",
        "request_id": data["request_id"],
        "issue_ids": [data["root_issue_id"]],
        "root_issue_id": data["root_issue_id"],
        "workspace": {
            "repository_root": data["repository_root"],
            "workspace_sha256": data["workspace_identity_sha256"],
            "cli_version": CLI_VERSION,
        },
        "observed_changes": [],
        "authority": {
            "requested": ["read", "local_write"],
            "exercised": ["read", "local_write"],
            "readback_proven": ["read", "local_write"],
        },
        "native_events": [],
        "verification": {
            "required": False,
            "disposition": "not_required",
            "target_sha256": None,
            "observed_at": None,
            "evidence": [],
        },
        # "creation" is reserved by the schema for issue-creation/dependency-
        # editing operations (an object with requested_ids/created_ids/etc, or
        # null) -- start_run has no such shape available, so it must stay
        # null here. The bootstrap disposition is already fully represented
        # via status/error_code/coverage_gaps below; run_directory is not
        # reported separately since the caller already knows run_root (their
        # own --input) and receives run_id, so it can reconstruct the path
        # without the envelope needing a non-conforming field for it.
        "creation": None,
        "pending_actions": [],
        "blockers": [] if healthy else [bootstrap.disposition.upper()],
        "warnings": [],
        "coverage_gaps": []
        if healthy
        else [_diagnostic(bootstrap.disposition.upper(), "start_run_blocked")],
        "errors": [],
        "error_code": None if healthy else bootstrap.disposition.upper(),
        "safe_next_action": None
        if healthy
        else _diagnostic(bootstrap.disposition.upper(), "start_run_blocked"),
        "run_id": bootstrap.run_id,
        "checkpoint": None,
        "cancellation_reason": None,
    }
    return _build(candidate)


# ---------------------------------------------------------------------------
# action prepare / action resolve
# ---------------------------------------------------------------------------

_ACTION_PREPARE_FIELDS = (
    "effect_type",
    "target_identity",
    "target_sha256",
    "precondition_sha256",
    "summary",
    "probe_type",
    "probe_argv",
    "issue_id",
    "ownership_epoch",
)


def _handle_action_prepare(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, _ACTION_PREPARE_FIELDS)
    manifest = state.load_run_manifest(args.run_dir)
    context = direct_operation.open_run(args.run_dir)
    prepared = protected_action.prepare_protected_action(
        context,
        effect_type=data["effect_type"],
        target_identity=data["target_identity"],
        target_sha256=data["target_sha256"],
        precondition_sha256=data["precondition_sha256"],
        summary=data["summary"],
        probe_type=data["probe_type"],
        probe_argv=list(data["probe_argv"]),
        issue_id=data["issue_id"],
        ownership_epoch=data["ownership_epoch"],
        parameters=tuple(data.get("parameters", ())),
        artifact_path=data.get("artifact_path"),
        expires_at=data.get("expires_at"),
    )
    candidate = _base_envelope(
        "prepare_action",
        manifest,
        issue_ids=[data["issue_id"]] if data["issue_id"] else [],
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    candidate["status"] = "human_action_required"
    candidate["pending_actions"] = [prepared["pending_action"]]
    candidate["error_code"] = "PROTECTED_ACTION_PENDING"
    candidate["safe_next_action"] = _diagnostic(
        "SUBMIT_ACTION_RECEIPT", "action_prepare_pending"
    )
    return _build(candidate)


_ACTION_RESOLVE_FIELDS = ("prepared", "pending_action", "receipt")


def _handle_action_resolve(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, _ACTION_RESOLVE_FIELDS)
    manifest = state.load_run_manifest(args.run_dir)
    context = direct_operation.open_run(args.run_dir)
    resolution = protected_action.resolve_protected_action(
        context,
        prepared=data["prepared"],
        pending_action=data["pending_action"],
        receipt=data["receipt"],
    )
    raw_status = resolution.get("status", "UNKNOWN")
    issue_id = data["pending_action"].get("issue_id")
    candidate = _base_envelope(
        "resolve_action",
        manifest,
        issue_ids=[issue_id] if issue_id else [],
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    candidate["status"] = _map_direct_status(raw_status)
    if candidate["status"] not in {"success"}:
        candidate["error_code"] = resolution.get("error_code", raw_status)
        candidate["coverage_gaps"] = [
            _diagnostic(candidate["error_code"], "action_resolve_incomplete")
        ]
        candidate["safe_next_action"] = _diagnostic(
            "RETRY_ACTION_RESOLVE", "action_resolve_next_action"
        )
        if candidate["status"] == "blocked":
            candidate["blockers"] = [candidate["error_code"]]
    return _build(candidate)


# ---------------------------------------------------------------------------
# freeze / verify / review / review-combined / build-candidate / apply-candidate
# ---------------------------------------------------------------------------


def _integration_candidate(
    label: str,
    manifest: Mapping[str, Any],
    issue_ids: Sequence[str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an envelope for one of the lane gate-evaluation subcommands.

    ``label`` (``"freeze"``, ``"verify"``, ``"review"``, ``"review_combined"``,
    ``"build_candidate"``, ``"apply_candidate"``) is a diagnostic-only name
    used purely to keep ``_diagnostic(...)`` template ids distinguishable per
    subcommand -- it is NOT a legal value of the schema's ``operation`` enum
    (``beads.operation-result.v1`` closes that enum to 16 fixed strings; none
    of these six ad hoc names are members of it). All six of these
    subcommands are, per the spec (`hermes-beads-skill-spec.md` S7.7),
    "gate" operations, so the schema-facing ``operation`` field is always the
    single shared enum value ``"evaluate_gate"`` regardless of which lane
    step produced this candidate.
    """
    raw_status = payload.get("status", "refused")
    candidate = _base_envelope(
        "evaluate_gate",
        manifest,
        issue_ids=issue_ids,
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    candidate["status"] = _map_integration_status(raw_status)
    if candidate["status"] != "success":
        candidate["error_code"] = payload.get("error_code", raw_status.upper())
        candidate["coverage_gaps"] = [
            _diagnostic(candidate["error_code"], f"{label}_refused")
        ]
        candidate["safe_next_action"] = _diagnostic(
            candidate["error_code"], f"{label}_next_action"
        )
        if candidate["status"] == "blocked":
            candidate["blockers"] = [candidate["error_code"]]
    return candidate


def _handle_freeze(args: argparse.Namespace) -> operation_result.OperationResult:
    manifest = state.load_run_manifest(args.run_dir)
    context = coordinator_integration.open_run(args.run_dir)
    payload = coordinator_integration.freeze_lane(
        context, args.issue_id, expected_result_sha256=args.expected_result_sha256
    )
    return _build(_integration_candidate("freeze", manifest, [args.issue_id], payload))


def _handle_verify(args: argparse.Namespace) -> operation_result.OperationResult:
    manifest = state.load_run_manifest(args.run_dir)
    context = coordinator_integration.open_run(args.run_dir)
    payload = coordinator_integration.verify_lane(context, args.issue_id)
    return _build(_integration_candidate("verify", manifest, [args.issue_id], payload))


def _handle_review(args: argparse.Namespace) -> operation_result.OperationResult:
    manifest = state.load_run_manifest(args.run_dir)
    context = coordinator_integration.open_run(args.run_dir)
    payload = coordinator_integration.record_review(
        context, args.issue_id, args.review_path
    )
    return _build(_integration_candidate("review", manifest, [args.issue_id], payload))


def _handle_review_combined(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    manifest = state.load_run_manifest(args.run_dir)
    context = coordinator_integration.open_run(args.run_dir)
    payload = coordinator_integration.record_combined_review(context, args.review_path)
    return _build(_integration_candidate("review_combined", manifest, [], payload))


def _handle_build_candidate(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, ("lane_freeze_sha256s",))
    manifest = state.load_run_manifest(args.run_dir)
    context = coordinator_integration.open_run(args.run_dir)
    payload = coordinator_integration.build_candidate(
        context,
        lane_freeze_sha256s=list(data["lane_freeze_sha256s"]),
        expected_predecessor=data.get("expected_predecessor"),
    )
    return _build(_integration_candidate("build_candidate", manifest, [], payload))


def _handle_apply_candidate(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    manifest = state.load_run_manifest(args.run_dir)
    context = coordinator_integration.open_run(args.run_dir)
    payload = coordinator_integration.apply_candidate(
        context, args.candidate_id, expected_predecessor=args.expected_predecessor
    )
    return _build(_integration_candidate("apply_candidate", manifest, [], payload))


# ---------------------------------------------------------------------------
# tracker-update (claim_front)
# ---------------------------------------------------------------------------


def _handle_tracker_update(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, ("issues",))
    manifest = state.load_run_manifest(args.run_dir)
    context = direct_operation.open_run(args.run_dir)
    ownership = beads_ownership.OwnershipStore(args.run_dir.parent)
    results = coordinator_tracker.claim_front(
        context,
        ownership=ownership,
        issues=list(data["issues"]),
        actor=args.actor,
    )
    candidate = _base_envelope(
        "execute_set",
        manifest,
        issue_ids=list(data["issues"]),
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    mapped = {_map_direct_status(entry.status) for entry in results}
    if not mapped or mapped == {"success"}:
        candidate["status"] = "success" if mapped else "partial"
    elif "conflict" in mapped:
        candidate["status"] = "conflict"
    elif "inconclusive" in mapped:
        candidate["status"] = "inconclusive"
    else:
        candidate["status"] = "partial"
    if candidate["status"] != "success":
        incomplete = [
            entry for entry in results if entry.status not in {direct_operation.APPLIED}
        ]
        candidate["coverage_gaps"] = [
            _diagnostic(entry.error_code or entry.status, "lane_claim_incomplete")
            for entry in incomplete
        ]
        candidate["error_code"] = (
            incomplete[0].error_code or incomplete[0].status
            if incomplete
            else candidate["status"].upper()
        )
        candidate["safe_next_action"] = _diagnostic(
            "RETRY_TRACKER_UPDATE", "tracker_update_next_action"
        )
    return _build(candidate)


# ---------------------------------------------------------------------------
# gate (coordinator_front)
# ---------------------------------------------------------------------------


def _handle_gate(args: argparse.Namespace) -> operation_result.OperationResult:
    data = _load_json_input(args.input) if args.input else {}
    _snapshot, front = coordinator_front.capture_and_build_front(
        repository_root=args.repository_root,
        root_issue_id=data.get("root_issue_id"),
        limit=data.get("limit", 100),
        hard_cap=data["hard_cap"],
        requested_cap=data.get("requested_cap"),
    )
    # ``gate`` is a stateless, read-only front computation with no run
    # directory and no persisted manifest, so unlike every other subcommand
    # (which either requires the caller to supply ``request_id`` directly or
    # reads it back off a manifest that was itself populated from caller
    # input at start-run time), a caller here may reasonably omit both
    # ``request_id`` and ``run_id`` from ``--input``. Both fields are
    # envelope-only for this handler (neither feeds coordinator_state's
    # request-dedup path), but the schema still requires ``request_id`` to be
    # a 16-128-char printable-ASCII string unconditionally (no null variant),
    # so a short literal placeholder like the old ``"gate"`` (4 chars) fails
    # schema validation on every call that doesn't explicitly supply one.
    # ``run_id`` does allow null, so its fallback is simply ``None`` rather
    # than an invalid placeholder string.
    fallback_request_id = f"gate-{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%f}Z"
    manifest = {
        "request_id": data.get("request_id") or fallback_request_id,
        "root_issue_id": data.get("root_issue_id") or "unknown",
        "repository_root": str(args.repository_root),
        "workspace_identity_sha256": data.get("workspace_identity_sha256", "0" * 64),
        "run_id": data.get("run_id"),
    }
    candidate = _base_envelope(
        "front",
        manifest,
        issue_ids=list(front.selected),
        requested=["read"],
        exercised=["read"],
    )
    candidate["status"] = "success" if front.refusal is None else "blocked"
    if front.refusal is not None:
        candidate["error_code"] = front.refusal
        candidate["blockers"] = [front.refusal]
        candidate["coverage_gaps"] = [_diagnostic(front.refusal, "gate_refused")]
        candidate["safe_next_action"] = _diagnostic(front.refusal, "gate_next_action")
    return _build(candidate)


# ---------------------------------------------------------------------------
# handoff-launch / handoff-accept
# ---------------------------------------------------------------------------


def _handle_handoff_launch(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, ("handoff",))
    manifest = state.load_run_manifest(args.run_dir)
    context = direct_operation.open_run(args.run_dir)
    launch = coordinator_handoff.launch(
        context, handoff=data["handoff"], actor=args.actor
    )
    candidate = _base_envelope(
        "execute_one",
        manifest,
        issue_ids=[data["handoff"].get("issue_id")]
        if data["handoff"].get("issue_id")
        else [],
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    candidate["status"] = _map_direct_status(launch.status)
    if candidate["status"] != "success":
        candidate["error_code"] = launch.status
        candidate["coverage_gaps"] = [
            _diagnostic(launch.status, "handoff_launch_incomplete")
        ]
        candidate["safe_next_action"] = _diagnostic(
            "RETRY_HANDOFF_LAUNCH", "handoff_launch_next_action"
        )
        if candidate["status"] == "blocked":
            candidate["blockers"] = [candidate["error_code"]]
    return _build(candidate)


def _reconstruct_direct_operation(
    spec: Mapping[str, Any],
) -> direct_operation.DirectOperation:
    readback_spec = spec["readback"]
    readback = direct_operation.Readback(
        profile=readback_spec["profile"],
        arguments=readback_spec["arguments"],
        intended=readback_spec["intended"],
        prestate=readback_spec["prestate"],
        select=functools.partial(
            direct_operation.default_select, issue_id=spec.get("issue_id")
        ),
    )
    return direct_operation.DirectOperation(
        caller_key=spec["caller_key"],
        effect_type=spec["effect_type"],
        target_identity=spec["target_identity"],
        issue_id=spec.get("issue_id"),
        ownership_epoch=spec.get("ownership_epoch"),
        arguments=spec.get("arguments", {}),
        readback=readback,
        marker_issue_id=spec.get("marker_issue_id"),
        marker_enabled=spec.get("marker_enabled", True),
    )


def _handle_handoff_accept(
    args: argparse.Namespace,
) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, ("handoff", "result", "operation"))
    manifest = state.load_run_manifest(args.run_dir)
    context = direct_operation.open_run(args.run_dir)
    ownership = beads_ownership.OwnershipStore(args.run_dir.parent)
    operation = _reconstruct_direct_operation(data["operation"])
    acceptance = coordinator_handoff.accept(
        context,
        handoff=data["handoff"],
        result=data["result"],
        operation=operation,
        actor=args.actor,
        ownership=ownership,
    )
    candidate = _base_envelope(
        "execute_one",
        manifest,
        issue_ids=[data["handoff"].get("issue_id")]
        if data["handoff"].get("issue_id")
        else [],
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    candidate["status"] = _map_direct_status(acceptance.status)
    if candidate["status"] != "success":
        candidate["error_code"] = getattr(acceptance, "error_code", acceptance.status)
        candidate["coverage_gaps"] = [
            _diagnostic(candidate["error_code"], "handoff_accept_incomplete")
        ]
        candidate["safe_next_action"] = _diagnostic(
            "RETRY_HANDOFF_ACCEPT", "handoff_accept_next_action"
        )
        if candidate["status"] == "blocked":
            candidate["blockers"] = [candidate["error_code"]]
    return _build(candidate)


# ---------------------------------------------------------------------------
# finish
# ---------------------------------------------------------------------------

# run-manifest-v1 never persists the full original StartRunInput (only a
# sha256 of it, for allocation-time dedup); the caller must resupply the
# fields the frozen manifest schema does not carry, exactly as start-run's
# own --input does. Fields already present on a real manifest (request_id,
# repository_root, workspace, root_issue_id, workspace_identity_sha256) are
# read from the manifest instead, so finish cannot be pointed at a run other
# than the one --run-dir names.
_FINISH_FIELDS = (
    "terminal_status",
    "git_common_dir",
    "scope_issue_ids",
    "base_git_commit",
    "authority_snapshot_sha256",
    "ownership_epoch",
)


def _handle_finish(args: argparse.Namespace) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, _FINISH_FIELDS)
    manifest = state.load_run_manifest(args.run_dir)
    request = state.StartRunInput(
        request_id=manifest["request_id"],
        repository_root=manifest["repository_root"],
        git_common_dir=data["git_common_dir"],
        workspace=manifest["workspace"],
        run_root=str(args.run_dir.parent),
        root_issue_id=manifest["root_issue_id"],
        scope_issue_ids=tuple(data["scope_issue_ids"]),
        actor=args.actor,
        base_git_commit=data["base_git_commit"],
        authority_snapshot_sha256=data["authority_snapshot_sha256"],
        workspace_identity_sha256=manifest["workspace_identity_sha256"],
    )
    callbacks = coordinator_tracker.pointer_callbacks(request, actor=args.actor)
    pointer = {
        "schema_version": "beads.run-pointer.v1",
        "run_id": manifest["run_id"],
        "ownership_epoch": data["ownership_epoch"],
        "status": data["terminal_status"],
    }
    before = callbacks.observe(pointer)
    if before.classification == direct_operation.INTENDED_EFFECT_PRESENT:
        observation = before
    else:
        observation = callbacks.publish(pointer)
    raw_status = direct_operation.CLASSIFICATION_STATUS.get(
        observation.classification, direct_operation.UNKNOWN
    )
    candidate = _base_envelope(
        "finish",
        manifest,
        issue_ids=[manifest["root_issue_id"]],
        requested=["read", "local_write"],
        exercised=["read", "local_write"],
    )
    candidate["status"] = _map_direct_status(raw_status)
    if candidate["status"] != "success":
        candidate["error_code"] = raw_status
        candidate["coverage_gaps"] = [
            _diagnostic("TERMINAL_POINTER_UNCONFIRMED", "finish_unconfirmed")
        ]
        candidate["safe_next_action"] = _diagnostic(
            "RECONCILE_TERMINAL_POINTER", "finish_next_action"
        )
        if candidate["status"] == "blocked":
            candidate["blockers"] = [candidate["error_code"]]
    return _build(candidate)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

# run-manifest-v1 has no scope_issue_ids field (only a sha256 of the original
# StartRunInput is ever persisted), so the set of issues to ownership-check
# before deleting a run directory must be resupplied by the caller, exactly
# as finish's --input resupplies fields the manifest cannot carry. No frozen
# module exposes a run-directory deletion primitive either (reconcile_run
# only ever reasons about journal/ownership consistency, never deletes), so
# the retention-window check and the deletion itself are implemented here.
_CLEANUP_FIELDS = ("scope_issue_ids",)


def _run_age_seconds(manifest: Mapping[str, Any], *, now: datetime) -> float:
    created_at = datetime.strptime(
        manifest["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=timezone.utc)
    return (now - created_at).total_seconds()


def _handle_cleanup(args: argparse.Namespace) -> operation_result.OperationResult:
    data = _load_json_input(args.input)
    _require_fields(data, _CLEANUP_FIELDS)
    manifest = state.load_run_manifest(args.run_dir)
    plan = reconcile_run.status(args.run_dir)
    ownership = beads_ownership.OwnershipStore(args.run_dir.parent)
    candidate = _base_envelope(
        "cleanup_run",
        manifest,
        issue_ids=[manifest["root_issue_id"]],
        requested=["read"],
        exercised=["read"],
    )
    refusals: list[dict[str, Any]] = []
    if plan.disposition not in {"consistent"}:
        refusals.append(_diagnostic("RUN_NOT_RECONCILED", "cleanup_refused"))
    for issue_id in data["scope_issue_ids"]:
        disposition = ownership.inspect_readonly(issue_id).disposition
        if disposition in {"held", "unknown", "conflict"}:
            refusals.append(
                _diagnostic(f"OWNERSHIP_{disposition.upper()}", "cleanup_refused")
            )
    try:
        resolved_root = state.validate_owner_directory(args.run_dir)
    except state.StateError as exc:
        refusals.append(_diagnostic(exc.code, "cleanup_refused"))
        resolved_root = None
    if resolved_root is not None and str(resolved_root) == resolved_root.anchor:
        refusals.append(_diagnostic("CLEANUP_TARGET_TOO_BROAD", "cleanup_refused"))
    age_seconds = _run_age_seconds(manifest, now=datetime.now(timezone.utc))
    if age_seconds < args.retention_seconds:
        refusals.append(_diagnostic("CLEANUP_RETENTION_NOT_ELAPSED", "cleanup_refused"))
    if refusals:
        candidate["status"] = "blocked"
        candidate["coverage_gaps"] = refusals
        candidate["blockers"] = [entry["code"] for entry in refusals]
        candidate["error_code"] = refusals[0]["code"]
        candidate["safe_next_action"] = refusals[0]
        return _build(candidate)
    if args.dry_run:
        return _build(candidate)
    assert resolved_root is not None  # no refusals means validation succeeded
    shutil.rmtree(resolved_root)
    return _build(candidate)


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

_HANDLERS: dict[
    str, Callable[[argparse.Namespace], operation_result.OperationResult]
] = {
    "start-run": _handle_start_run,
    "freeze": _handle_freeze,
    "verify": _handle_verify,
    "review": _handle_review,
    "review-combined": _handle_review_combined,
    "build-candidate": _handle_build_candidate,
    "apply-candidate": _handle_apply_candidate,
    "tracker-update": _handle_tracker_update,
    "gate": _handle_gate,
    "handoff-launch": _handle_handoff_launch,
    "handoff-accept": _handle_handoff_accept,
    "finish": _handle_finish,
    "cleanup": _handle_cleanup,
}

# Exceptions raised by the frozen modules, mapped to an operation-result status.
_INTEGRATION_ERRORS = (
    coordinator_integration.IntegrationError,
    state.StateError,
    beads_ownership.OwnershipError,
    reconcile_run.ReconciliationError,
)
_DIRECT_ERRORS = (
    direct_operation.DirectOperationError,
    protected_action.ProtectedActionError,
    coordinator_handoff.HandoffError,
)


def _status_for_exception(exc: Exception) -> str:
    if isinstance(exc, _DIRECT_ERRORS):
        raw_status: str | None = getattr(exc, "status", None)
        if raw_status == protected_action.HUMAN_ACTION_REQUIRED:
            return "human_action_required"
        if raw_status == direct_operation.APPLIED:
            return "failed"
        if raw_status is None:
            return "blocked"
        return _DIRECT_CLASSIFICATION_STATUS.get(raw_status, "blocked")
    if isinstance(exc, _INTEGRATION_ERRORS):
        return _map_integration_status(getattr(exc, "status", "refused"))
    if isinstance(exc, coordinator_front.CoordinatorFrontError):
        return "blocked"
    return "failed"


_EXIT_CODE_FOR_STATUS = {
    "success": 0,
    "human_action_required": 6,
    "conflict": 4,
    "inconclusive": 5,
}


def _exit_code_for_status(status: str) -> int:
    return _EXIT_CODE_FOR_STATUS.get(status, 1)


def _add_run_dir(command: argparse.ArgumentParser) -> None:
    command.add_argument("--run-dir", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    for name in ("status", "recover"):
        command = subcommands.add_parser(name)
        _add_run_dir(command)
        command.add_argument("--json", action="store_true")

    start_run = subcommands.add_parser("start-run")
    start_run.add_argument("--input", type=Path, required=True)
    start_run.add_argument("--actor", required=True)
    start_run.add_argument("--json", action="store_true")

    action = subcommands.add_parser("action")
    action_sub = action.add_subparsers(dest="action_command", required=True)
    action_prepare = action_sub.add_parser("prepare")
    _add_run_dir(action_prepare)
    action_prepare.add_argument("--input", type=Path, required=True)
    action_prepare.add_argument("--json", action="store_true")
    action_resolve = action_sub.add_parser("resolve")
    _add_run_dir(action_resolve)
    action_resolve.add_argument("--input", type=Path, required=True)
    action_resolve.add_argument("--json", action="store_true")

    for name in ("freeze", "verify"):
        command = subcommands.add_parser(name)
        _add_run_dir(command)
        command.add_argument("--issue-id", required=True)
        command.add_argument("--json", action="store_true")
    freeze = next(
        c
        for c in subcommands.choices.values()
        if getattr(c, "prog", "").endswith(" freeze")
    )
    freeze.add_argument("--expected-result-sha256", required=True)

    review = subcommands.add_parser("review")
    _add_run_dir(review)
    review.add_argument("--issue-id", required=True)
    review.add_argument("--review-path", type=Path, required=True)
    review.add_argument("--json", action="store_true")

    review_combined = subcommands.add_parser("review-combined")
    _add_run_dir(review_combined)
    review_combined.add_argument("--review-path", type=Path, required=True)
    review_combined.add_argument("--json", action="store_true")

    build_candidate = subcommands.add_parser("build-candidate")
    _add_run_dir(build_candidate)
    build_candidate.add_argument("--input", type=Path, required=True)
    build_candidate.add_argument("--json", action="store_true")

    apply_candidate = subcommands.add_parser("apply-candidate")
    _add_run_dir(apply_candidate)
    apply_candidate.add_argument("--candidate-id", required=True)
    apply_candidate.add_argument("--expected-predecessor")
    apply_candidate.add_argument("--json", action="store_true")

    tracker_update = subcommands.add_parser("tracker-update")
    _add_run_dir(tracker_update)
    tracker_update.add_argument("--input", type=Path, required=True)
    tracker_update.add_argument("--actor", required=True)
    tracker_update.add_argument("--json", action="store_true")

    gate = subcommands.add_parser("gate")
    gate.add_argument("--repository-root", type=Path, required=True)
    gate.add_argument("--input", type=Path)
    gate.add_argument("--json", action="store_true")

    handoff_launch = subcommands.add_parser("handoff-launch")
    _add_run_dir(handoff_launch)
    handoff_launch.add_argument("--input", type=Path, required=True)
    handoff_launch.add_argument("--actor", required=True)
    handoff_launch.add_argument("--json", action="store_true")

    handoff_accept = subcommands.add_parser("handoff-accept")
    _add_run_dir(handoff_accept)
    handoff_accept.add_argument("--input", type=Path, required=True)
    handoff_accept.add_argument("--actor", required=True)
    handoff_accept.add_argument("--json", action="store_true")

    finish = subcommands.add_parser("finish")
    _add_run_dir(finish)
    finish.add_argument("--input", type=Path, required=True)
    finish.add_argument("--actor", required=True)
    finish.add_argument("--json", action="store_true")

    cleanup = subcommands.add_parser("cleanup")
    _add_run_dir(cleanup)
    cleanup.add_argument("--input", type=Path, required=True)
    cleanup.add_argument("--retention-seconds", type=int, required=True)
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command in {"status", "recover"}:
        return _run_status_recover(args)

    command = (
        f"action {args.action_command}" if args.command == "action" else args.command
    )
    handler = _HANDLERS.get(command) or {
        "action prepare": _handle_action_prepare,
        "action resolve": _handle_action_resolve,
    }.get(command)
    if handler is None:
        # Unreachable by construction: every subparser registered in
        # _parser() has a matching entry above. Kept as an explicit,
        # typed guard rather than an assumption a future subcommand could
        # silently violate.
        raise TypeError(f"no handler registered for command {command!r}")
    full = getattr(args, "json", False)
    try:
        result = handler(args)
    except _CliInputError as exc:
        envelope = operation_result.cli_error_envelope(exc.code.split(":", 1)[0], 2)
        print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        return 2
    except (
        _INTEGRATION_ERRORS
        + _DIRECT_ERRORS
        + (coordinator_front.CoordinatorFrontError,)
    ) as exc:  # type: ignore[misc]
        status = _status_for_exception(exc)
        error_code = getattr(exc, "code", type(exc).__name__)
        print(
            json.dumps(
                {"status": status, "error_code": error_code},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return _exit_code_for_status(status)
    except (KeyError, TypeError, ValueError):
        envelope = operation_result.cli_error_envelope("INPUT_INVALID", 2)
        print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        return 2
    return _emit(result, full=full)


if __name__ == "__main__":
    raise SystemExit(main())
