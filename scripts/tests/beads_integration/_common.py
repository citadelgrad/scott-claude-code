"""Shared fixtures for coordinator_integration tests (real repos, real runs)."""

from __future__ import annotations

import hashlib
import os
import importlib.util
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"
PACKET_TEST = ROOT / "scripts/tests/beads_contract/test_worker_packet.py"

_counter = {"n": 0}
GIT = "git"


def load(path: Path):
    _counter["n"] += 1
    spec = importlib.util.spec_from_file_location(
        f"beads_common_{_counter['n']}_{path.stem}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def modules():
    sys.path.insert(0, str(SCRIPTS))

    class Bundle:
        pass

    bundle = Bundle()
    for name in (
        "coordinator_state",
        "coordinator_integration",
        "beads_ownership",
        "lane_snapshot",
        "package_lane",
        "safe_output",
        "schema_runtime",
        "validate_lane_freeze",
        "validate_worker_execution_result",
        "validate_worker_packet",
        "worker_result",
    ):
        setattr(bundle, name.replace("-", "_"), load(SCRIPTS / f"{name}.py"))
    return bundle


def factory():
    return load(PACKET_TEST)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=cwd, capture_output=True, check=check)


def issue_entry(m, *, epoch: int, result_sha: str | None, packet_sha: str) -> dict:
    return {
        "tracker_status_observed": "in_progress",
        "readiness": {"state": "ready", "reason": "claimed by run"},
        "ownership": {
            "state": "held",
            "epoch": epoch,
            "token_sha256": "1" * 64,
            "actor": "parent",
        },
        "attempt": {
            "state": "joined",
            "attempt_id": "attempt-001",
            "delegation_id": "delegation-1",
            "subagent_id": None,
            "child_session_id": None,
        },
        "worker_result": {
            "state": "accepted" if result_sha else "received",
            "outcome": "completed" if result_sha else None,
            "record_sha256": result_sha,
        },
        "artifact": {"state": "worker_returned", "lane_freeze_sha256": None},
        "verification": {"state": "not_started", "record_sha256": None},
        "review": {"state": "not_required", "record_sha256": None},
        "integration": {
            "state": "not_started",
            "candidate_sha256": None,
            "event_id": None,
        },
        "gate": {"state": "none", "gate_id": None},
        "packet_sha256": packet_sha,
        "result_sha256": result_sha,
        "worktree": None,
        "branch": None,
        "base_sha": None,
        "head_sha": None,
    }


def make_run(m, repo: Path, run_id: str, issue_ids: list[str]) -> dict:
    """Bootstrap a run directory, journal, and cooperative ownership."""
    state = m.coordinator_state
    hermes = repo / ".hermes"
    hermes.mkdir(mode=0o700, exist_ok=True)
    (hermes / "beads-runs").mkdir(mode=0o700, exist_ok=True)
    for path in (hermes, hermes / "beads-runs"):
        path.chmod(0o700)
    run_root = hermes / "beads-runs"
    run_directory = run_root / run_id
    state.ensure_owner_directory(run_directory, root=run_root)
    manifest = state.make_run_manifest(
        run_id=run_id,
        request_id="request-0001-aaaaaaaaaaaa",
        repository_root=str(repo),
        workspace=str(repo),
        workspace_identity_sha256=hashlib.sha256(str(repo).encode()).hexdigest(),
        root_issue_id="scc-root",
        created_at=now(),
        coordinator_version="beads-coordinator-v1",
        run_root=str(run_root),
        run_secret_hex=secrets.token_bytes(32).hex(),
    )
    state.publish_run_manifest(run_directory, manifest)
    journal = state.OperationJournal.create(run_directory)
    ownership = m.beads_ownership.OwnershipStore(run_root)
    epochs: dict[str, int] = {}
    for issue_id in issue_ids:
        record = ownership.acquire(
            issue_id=issue_id,
            actor="parent",
            run_directory=run_directory,
            tracker_state_sha256="0" * 64,
            operation_id="0" * 64,
            now=datetime.now(timezone.utc),
        )
        assert record.disposition == "held", record.disposition
        epochs[issue_id] = record.record["epoch"]

    def finish(entries: dict) -> Any:
        checkpoints = state.CheckpointStore(run_directory, journal)
        checkpoint = {
            "schema_version": "beads.run-checkpoint.v1",
            "run_id": run_id,
            "generation": 1,
            "root_issue_id": "scc-root",
            "workspace": manifest["workspace"],
            "workspace_identity_sha256": manifest["workspace_identity_sha256"],
            "repository_root": manifest["repository_root"],
            "coordinator_session_id": None,
            "authority_snapshot_sha256": "0" * 64,
            "phase": "active",
            "budget": {
                "max_parallel": 3,
                "max_ready_fronts": 10,
                "max_worker_attempts_per_issue": 2,
                "max_nonprogress_rounds": 2,
            },
            "issues": entries,
            "operation_journal_path": str(
                (run_directory / "operations.jsonl").resolve()
            ),
            "issue_snapshot_sha256": "0" * 64,
            "ready_front_sha256": "0" * 64,
            "previous_checkpoint_sha256": state.GENESIS_SHA256,
            "created_at": now(),
        }
        checkpoints.accept(checkpoint)
        return checkpoints

    return {
        "run_directory": run_directory,
        "manifest": manifest,
        "journal": journal,
        "epochs": epochs,
        "finish": finish,
    }


def issue_key(m, issue_id: str) -> str:
    return m.coordinator_state.issue_key(issue_id)


def import_attempt(
    m,
    run_directory: Path,
    issue_id: str,
    *,
    epoch: int,
    worktree: Path,
    outbox: Path,
    head: str,
    changed_file: str | None,
    file_content: str,
    packet: dict,
    status: str = "completed",
) -> tuple:
    """Run one honest worker attempt and import result/receipt into the run."""
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    previous_cwd = os.getcwd()
    os.chdir(worktree)
    monkey_context = m.worker_result.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=epoch
    )
    evidence = m.worker_result.run_declared_command(
        monkey_context, command_index=0, sensitive_values_file=descriptor(worktree)
    )
    if changed_file:
        target = worktree / changed_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_content)
    snapshot = m.lane_snapshot.capture(worktree, head, exclude=outbox)
    record = outbox / "command-000.json"
    stdout = Path(evidence["safe_result"]["stdout_log"]["path"])
    stderr = Path(evidence["safe_result"]["stderr_log"]["path"])

    def identity(path: Path, artifact_type: str) -> dict:
        data = path.read_bytes()
        return {
            "type": artifact_type,
            "path": str(path),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    candidate = {
        "schema_version": "beads.worker-execution-result.v1",
        "run_id": packet["run_id"],
        "attempt_id": packet["attempt_id"],
        "issue_id": packet["issue"]["id"],
        "packet_sha256": digest,
        "status": status,
        "repository": {
            "worktree": str(worktree),
            "branch": packet["repository"]["branch"],
            "base_sha": head,
            "head_sha": snapshot.head_sha,
        },
        "lane_state": snapshot.lane_state,
        "changes": {
            "paths": list(snapshot.changed_paths),
            "outside_allowed_scope": [],
        },
        "verification": [
            {
                "command": json.dumps(evidence["argv"], separators=(",", ":")),
                "exit_code": evidence["safe_result"]["exit_code"],
                "started_at": evidence["safe_result"]["started_at"],
                "finished_at": evidence["safe_result"]["finished_at"],
                "log_path": str(record),
                "log_sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
            }
        ],
        "acceptance_evidence": [
            {
                "acceptance_id": packet["issue"]["acceptance_ids"][0],
                "status": "supported",
                "evidence_paths": [str(record)],
            }
        ],
        "artifacts": [
            identity(record, "command_evidence"),
            identity(stdout, "stdout_log"),
            identity(stderr, "stderr_log"),
        ],
        "blockers": [],
        "errors": [],
        "cancellation_reason": None,
        "skipped_checks": [],
        "residual_risks": [],
        "summary": "scoped work verified",
        "integration_mode": packet["scope"]["integration_mode"],
        "worker_frozen_artifact": None,
    }
    m.worker_result.finalize_attempt(
        monkey_context, candidate, sensitive_values_file=descriptor(worktree)
    )
    try:
        if run_directory is not None:
            imports = run_directory / "imports" / issue_key(m, issue_id)
            m.coordinator_state.ensure_owner_directory(imports, root=run_directory)
            for name, data in (
                ("packet.json", raw),
                ("result.json", (outbox / "result.json").read_bytes()),
                ("receipt.json", (outbox / "receipt.json").read_bytes()),
            ):
                target = imports / name
                target.write_bytes(data)
                target.chmod(0o600)
    finally:
        os.chdir(previous_cwd)
    return digest, hashlib.sha256((outbox / "result.json").read_bytes()).hexdigest()


def descriptor(worktree: Path) -> Path:
    # Keep the descriptor outside the lane worktree so the untracked-file
    # inventory never sees it.
    path = worktree.parent / "sensitive-values.json"
    if not path.exists():
        path.write_text("[]")
    path.chmod(0o600)
    return path


def lane_packet(
    factory,
    tmp_path: Path,
    *,
    issue_id: str,
    run_id: str,
    worktree: Path,
    outbox: Path,
    head: str,
    allowed: list[str],
) -> dict:
    packet = factory._packet(tmp_path)
    packet["issue"]["id"] = issue_id
    packet["issue"]["key"] = hashlib.sha256(issue_id.encode()).hexdigest()
    packet["run_id"] = run_id
    packet["repository"]["base_sha"] = head
    packet["repository"]["worktree"] = str(worktree)
    packet["verification"]["worker_outbox"] = str(outbox)
    packet["scope"]["allowed_paths"] = allowed
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "ok"]]
    outbox.mkdir(parents=True, exist_ok=True)
    outbox.chmod(0o700)
    return packet


def reviewer_record(
    *,
    run_id: str,
    target_kind: str,
    target_sha256: str,
    reviewer_id: str,
    implementer_ids: list[str],
    verdict: str = "pass",
) -> dict:
    return {
        "schema_version": "beads.reviewer-result.v1",
        "run_id": run_id,
        "reviewer_id": reviewer_id,
        "target_kind": target_kind,
        "target_sha256": target_sha256,
        "packet_sha256": "0" * 64,
        "worker_result_sha256": "0" * 64,
        "lane_freeze_sha256": "0" * 64,
        "base_sha": "0" * 40,
        "target_identity": target_kind,
        "review_scope": ["diff"],
        "excluded_coverage": [],
        "independent": True,
        "implementer_ids": implementer_ids,
        "findings": [],
        "verdict": verdict,
        "artifact_sha256": "0" * 64,
    }


def write_review(m, run_directory: Path, review: dict) -> Path:
    path = (
        run_directory / f"review-{review['target_kind']}-{review['reviewer_id']}.json"
    )
    path.write_bytes(
        json.dumps(review, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    path.chmod(0o600)
    return path
