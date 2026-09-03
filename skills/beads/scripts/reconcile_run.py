#!/usr/bin/env python3
"""Read-only status and evidence-preserving filesystem reconciliation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beads_ownership
import coordinator_state as state
import schema_runtime


class ReconciliationError(state.StateError):
    """Typed reconciliation refusal."""


@dataclass(frozen=True)
class ReconciliationPlan:
    disposition: str
    run_id: str
    accepted_generation: int | None
    journal_status: str
    ownership_status: str
    next_safe_action: str
    repairs: tuple[str, ...] = ()
    local_fencing_limit: str = "cooperative_local_filesystem_only"


def _translate(exc: state.StateError) -> ReconciliationError:
    return ReconciliationError(exc.code, status=exc.status)


def _ownership_status_without_creation(run_root: Path, issue_id: str) -> str:
    owner_root = run_root / "_ownership"
    if not owner_root.exists():
        return "unheld"
    try:
        state.validate_owner_directory(owner_root, root=run_root)
        issue_dir = owner_root / state.issue_key(issue_id)
        if not issue_dir.exists():
            return "unheld"
        return beads_ownership.OwnershipStore(run_root).inspect(issue_id).disposition
    except state.StateError as exc:
        raise _translate(exc) from exc


def status(run_directory: Path) -> ReconciliationPlan:
    """Validate current durable state without creating or repairing anything."""
    run = Path(run_directory)
    try:
        state.validate_owner_directory(run)
        manifest = state.load_run_manifest(run)
        state.run_size(run)
        journal = state.OperationJournal(run)
        journal_state = journal.read()
        if journal_state.torn_tail is not None:
            return ReconciliationPlan(
                "manual_decision_required",
                manifest["run_id"],
                None,
                "torn_tail",
                _ownership_status_without_creation(
                    run.parent, manifest["root_issue_id"]
                ),
                "recover_journal_tail_under_run_lock",
            )
        reference = state.CheckpointStore(run, journal).current(rebuild_pointer=False)
        ownership = _ownership_status_without_creation(
            run.parent, manifest["root_issue_id"]
        )
        dangling = _dangling_prepared(journal_state.records)
        if dangling:
            return ReconciliationPlan(
                "manual_decision_required",
                manifest["run_id"],
                reference.generation,
                "dangling_prepared",
                ownership,
                "execute_bound_recovery_probe",
            )
        return ReconciliationPlan(
            "consistent",
            manifest["run_id"],
            reference.generation,
            "valid",
            ownership,
            "continue_from_accepted_checkpoint",
        )
    except state.StateError as exc:
        raise _translate(exc) from exc


def _dangling_prepared(records: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    prepared = {
        record["operation_id"] for record in records if record["phase"] == "PREPARED"
    }
    resolved = {
        record["operation_id"] for record in records if record["phase"] == "RESOLUTION"
    }
    return tuple(sorted(prepared - resolved))


def _preserve_journal(run: Path, raw: bytes) -> tuple[Path, str]:
    digest = state.sha256_bytes(raw)
    directory = run / "recovery-evidence"
    state.ensure_owner_directory(directory, root=run)
    path = directory / f"journal-{digest}.bin"
    if path.exists():
        existing = state.validate_owner_file(path, root=run).read_bytes()
        if existing != raw:
            raise ReconciliationError("RECOVERY_EVIDENCE_COLLISION")
    else:
        state.create_owner_file(path, raw, root=run, max_bytes=state.MAX_RUN_BYTES)
    if state.sha256_bytes(path.read_bytes()) != digest:
        raise ReconciliationError("RECOVERY_EVIDENCE_HASH_MISMATCH")
    return path, digest


def _filesystem_classification(probe: dict[str, Any]) -> str:
    if probe.get("probe_type") != "filesystem_identity":
        return "insufficient_observation"
    target = Path(probe["target_identity"])
    try:
        if target.is_symlink() or not target.is_file():
            return "prestate_unchanged"
        raw = target.read_bytes()
    except OSError:
        return "insufficient_observation"
    digest = state.sha256_bytes(raw)
    if digest == probe["intended_after_sha256"]:
        return "intended_effect_present"
    if digest == probe["expected_before_sha256"]:
        return "prestate_unchanged"
    return "conflicting_effect"


def _replace_journal(run: Path, raw: bytes) -> None:
    state.atomic_write(
        run / "operations.jsonl",
        raw,
        root=run,
        max_bytes=state.MAX_RUN_BYTES,
    )


def _corrupt_tail_event(
    run: Path,
    previous_sha: str,
    evidence_path: Path,
    evidence_sha: str,
    offset: int,
) -> dict[str, Any]:
    operation_id = state.semantic_operation_id(
        {
            "schema": "beads.corrupt-tail.v1",
            "effect_type": "JOURNAL_TAIL_REPAIR",
            "target_identity": str((run / "operations.jsonl").resolve()),
            "immutable_input_sha256": evidence_sha,
            "ownership_epoch": None,
        }
    )
    probe = {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "filesystem_identity",
        "target_identity": str(evidence_path.resolve()),
        "expected_before_sha256": evidence_sha,
        "intended_after_identity": str(evidence_path.resolve()),
        "intended_after_sha256": evidence_sha,
        "descriptor": ["filesystem", "preserved-journal-evidence"],
        "timeout_seconds": 1,
        "required_authority": "local_write",
    }
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": operation_id,
        "run_id": run.name,
        "attempt_id": None,
        "issue_id": None,
        "ownership_epoch": None,
        "effect_type": "JOURNAL_TAIL_REPAIR",
        "immutable_input_path": str(evidence_path.resolve()),
        "immutable_input_sha256": evidence_sha,
        "expected_pre_state_sha256": previous_sha,
        "recovery_probe": probe,
        "timestamp": state.utc_timestamp(dt.datetime.now(dt.timezone.utc)),
        "authority_class": "local_write",
        "previous_event_sha256": previous_sha,
        "phase": "CORRUPT_TAIL",
        "evidence_path": str(evidence_path.resolve()),
        "evidence_sha256": evidence_sha,
        "byte_offset": offset,
        "status": "UNKNOWN",
        "error": {
            "code": "JOURNAL_FINAL_FRAGMENT_UNPARSEABLE",
            "template_id": "journal_tail_unknown",
            "field_path": "/operations.jsonl",
            "parameters": [],
        },
    }


def _repair_torn_tail(
    run: Path, journal: state.OperationJournal
) -> tuple[str, tuple[str, ...]]:
    raw = journal.path.read_bytes()
    parsed = journal.read()
    if parsed.torn_tail is None:
        return "valid", ()
    evidence_path, evidence_sha = _preserve_journal(run, raw)
    tail = parsed.torn_tail
    try:
        value = schema_runtime.strict_json_loads(
            tail, max_bytes=state.MAX_MANIFEST_BYTES
        )
        schema_runtime.require_valid("operation-journal-event-v1.schema.json", value)
        if state.canonical_payload_bytes(value) != tail:
            raise ValueError
        if value["previous_event_sha256"] != parsed.last_sha256:
            raise ReconciliationError("JOURNAL_HASH_CHAIN_INVALID")
    except ReconciliationError:
        raise
    except Exception:
        event = _corrupt_tail_event(
            run,
            parsed.last_sha256,
            evidence_path,
            evidence_sha,
            parsed.torn_offset or 0,
        )
        prefix = raw[: parsed.torn_offset]
        _replace_journal(run, prefix + state.canonical_bytes(event))
        return "unknown", ("journal_tail_preserved", "corrupt_tail_recorded")

    phase = value["phase"]
    if phase == "PREPARED":
        # Normalize the complete intent, but filesystem-only recovery cannot
        # fabricate a resolution for a non-filesystem probe.
        _replace_journal(run, raw + b"\n")
        classification = _filesystem_classification(value["recovery_probe"])
        if classification == "insufficient_observation":
            return "dangling_prepared", ("journal_tail_normalized",)
        return "dangling_prepared", ("journal_tail_normalized",)
    if phase == "RESOLUTION":
        mapping = {
            "intended_effect_present": "APPLIED",
            "prestate_unchanged": "NOT_APPLIED",
            "conflicting_effect": "CONFLICT",
            "insufficient_observation": "UNKNOWN",
        }
        if (
            mapping[_filesystem_classification(value["recovery_probe"])]
            != value["status"]
        ):
            event = _corrupt_tail_event(
                run,
                parsed.last_sha256,
                evidence_path,
                evidence_sha,
                parsed.torn_offset or 0,
            )
            event["status"] = "CONFLICT"
            event["error"]["code"] = "JOURNAL_RESOLUTION_PROBE_MISMATCH"
            prefix = raw[: parsed.torn_offset]
            _replace_journal(run, prefix + state.canonical_bytes(event))
            return "conflict", ("journal_tail_preserved", "corrupt_tail_recorded")
    elif phase == "CHECKPOINT_ACCEPTED":
        path = Path(value["checkpoint_path"])
        try:
            checkpoint_raw = state.validate_owner_file(path, root=run).read_bytes()
        except state.StateError as exc:
            raise ReconciliationError("ACCEPTED_CHECKPOINT_MISSING") from exc
        if state.sha256_bytes(checkpoint_raw) != value["checkpoint_sha256"]:
            raise ReconciliationError("ACCEPTED_CHECKPOINT_HASH_MISMATCH")
    elif phase == "CORRUPT_TAIL":
        path = Path(value["evidence_path"])
        try:
            preserved = state.validate_owner_file(path, root=run).read_bytes()
        except state.StateError as exc:
            raise ReconciliationError("RECOVERY_EVIDENCE_MISSING") from exc
        if state.sha256_bytes(preserved) != value["evidence_sha256"]:
            raise ReconciliationError("RECOVERY_EVIDENCE_HASH_MISMATCH")
    _replace_journal(run, raw + b"\n")
    return ("unknown" if phase == "CORRUPT_TAIL" else "valid"), (
        "journal_tail_normalized",
    )


def recover(run_directory: Path) -> ReconciliationPlan:
    """Apply only evidence-preserving journal and checkpoint-pointer repairs."""
    run = Path(run_directory)
    repairs: list[str] = []
    try:
        state.validate_owner_directory(run)
        manifest = state.load_run_manifest(run)
        state.run_size(run)
        lock_path = run / "run.lock"
        with state.exclusive_lock(lock_path, root=run):
            journal = state.OperationJournal(run)
            journal_status, tail_repairs = _repair_torn_tail(run, journal)
            repairs.extend(tail_repairs)
            if journal_status in {"unknown", "conflict"}:
                return ReconciliationPlan(
                    "manual_decision_required",
                    manifest["run_id"],
                    None,
                    journal_status,
                    _ownership_status_without_creation(
                        run.parent, manifest["root_issue_id"]
                    ),
                    "inspect_preserved_journal_evidence",
                    tuple(repairs),
                )
            store = state.CheckpointStore(run, journal)
            try:
                reference = store.current(rebuild_pointer=False)
            except state.StateError as exc:
                if exc.code != "CHECKPOINT_POINTER_STALE":
                    raise
                reference = store.current(rebuild_pointer=True)
                repairs.append("checkpoint_pointer_rebuilt")
            ownership = _ownership_status_without_creation(
                run.parent, manifest["root_issue_id"]
            )
            dangling = _dangling_prepared(journal.read().records)
            if dangling:
                return ReconciliationPlan(
                    "manual_decision_required",
                    manifest["run_id"],
                    reference.generation,
                    "dangling_prepared",
                    ownership,
                    "execute_bound_recovery_probe",
                    tuple(repairs),
                )
            return ReconciliationPlan(
                "safe_to_retry" if repairs else "consistent",
                manifest["run_id"],
                reference.generation,
                "valid",
                ownership,
                "continue_from_accepted_checkpoint",
                tuple(repairs),
            )
    except state.StateError as exc:
        raise _translate(exc) from exc
