from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKER_RESULT = ROOT / "skills/beads/scripts/worker_result.py"
PACKET_TEST = ROOT / "scripts/tests/beads_contract/test_worker_packet.py"
LANE_SNAPSHOT = ROOT / "skills/beads/scripts/lane_snapshot.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    integration_mode: str = "patch_package",
) -> tuple[Any, Any, dict]:
    worker = _load(WORKER_RESULT, f"beads_worker_finalize_{tmp_path.name}")
    factory = _load(PACKET_TEST, f"finalize_packet_factory_{tmp_path.name}")
    snapshot_module = _load(LANE_SNAPSHOT, f"beads_lane_snapshot_{tmp_path.name}")
    repo, lane, head = factory._git_lane(tmp_path)
    packet = factory._packet(tmp_path)
    packet["repository"]["base_sha"] = head
    packet["scope"]["integration_mode"] = integration_mode
    packet["scope"]["external_io"] = integration_mode == "external_export"
    packet["scope"]["local_commit"] = integration_mode == "commit"
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "ok"]]
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    Path(packet["verification"]["worker_outbox"]).chmod(0o700)
    monkeypatch.chdir(packet["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=21
    )
    evidence = worker.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
    )
    return worker, context, _candidate(packet, digest, evidence, snapshot_module)


def _descriptor(tmp_path: Path, values: list[str]) -> Path:
    path = tmp_path / "sensitive-values.json"
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    return path


def _identity(path: Path, artifact_type: str) -> dict:
    data = path.read_bytes()
    return {
        "type": artifact_type,
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _candidate(
    packet: dict,
    packet_digest: str,
    evidence: dict,
    snapshot_module: Any = None,
) -> dict:
    outbox = Path(packet["verification"]["worker_outbox"])
    if snapshot_module is None:
        snapshot_module = _load(
            LANE_SNAPSHOT,
            f"beads_lane_snapshot_candidate_{packet['attempt_id']}",
        )
    snapshot = snapshot_module.capture(
        Path(packet["repository"]["worktree"]),
        packet["repository"]["base_sha"],
        exclude=outbox,
    )
    record = outbox / "command-000.json"
    stdout = Path(evidence["safe_result"]["stdout_log"]["path"])
    stderr = Path(evidence["safe_result"]["stderr_log"]["path"])
    record_digest = hashlib.sha256(record.read_bytes()).hexdigest()
    argv_text = json.dumps(evidence["argv"], separators=(",", ":"))
    return {
        "schema_version": "beads.worker-execution-result.v1",
        "run_id": packet["run_id"],
        "attempt_id": packet["attempt_id"],
        "issue_id": packet["issue"]["id"],
        "packet_sha256": packet_digest,
        "status": "completed",
        "repository": {
            "worktree": packet["repository"]["worktree"],
            "branch": packet["repository"]["branch"],
            "base_sha": packet["repository"]["base_sha"],
            "head_sha": snapshot.head_sha,
        },
        "lane_state": snapshot.lane_state,
        "changes": {
            "paths": list(snapshot.changed_paths),
            "outside_allowed_scope": [],
        },
        "verification": [
            {
                "command": argv_text,
                "exit_code": evidence["safe_result"]["exit_code"],
                "started_at": evidence["safe_result"]["started_at"],
                "finished_at": evidence["safe_result"]["finished_at"],
                "log_path": str(record),
                "log_sha256": record_digest,
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
            _identity(record, "command_evidence"),
            _identity(stdout, "stdout_log"),
            _identity(stderr, "stderr_log"),
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


def test_finalize_publishes_immutable_result_and_epoch_bound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
    )
    repeated = worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
    )

    assert receipt == repeated
    result_path = Path(receipt["result_path"])
    assert result_path == context.outbox / "result.json"
    result_bytes = result_path.read_bytes()
    assert receipt["result_sha256"] == hashlib.sha256(result_bytes).hexdigest()
    assert receipt["result_size_bytes"] == len(result_bytes) <= 65536
    assert receipt["ownership_epoch"] == 21
    receipt_bytes = (context.outbox / "receipt.json").read_bytes()
    assert len(receipt_bytes) <= 2048
    assert json.loads(result_bytes) == candidate


def test_finalize_recovers_after_crash_between_result_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    original = worker.safe_output.write_new_artifact
    failed = False

    def fail_receipt_once(path: Path, data: bytes):
        nonlocal failed
        if path.name == "receipt.json" and not failed:
            failed = True
            raise OSError("injected crash")
        return original(path, data)

    monkeypatch.setattr(worker.safe_output, "write_new_artifact", fail_receipt_once)
    with pytest.raises(worker.WorkerResultError, match="FINALIZE_WRITE_FAILED"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert (context.outbox / "result.json").is_file()
    assert not (context.outbox / "receipt.json").exists()

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
    )
    assert Path(receipt["result_path"]).is_file()
    assert (context.outbox / "receipt.json").is_file()


def test_newline_receipt_refusal_has_no_publication_and_retry_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    descriptor = _descriptor(tmp_path, [])
    original = worker.safe_output.write_new_artifact
    final_publications: list[str] = []

    def spy(path: Path, data: bytes):
        if path.name in {"result.json", "receipt.json"}:
            final_publications.append(path.name)
        return original(path, data)

    monkeypatch.setattr(worker.safe_output, "write_new_artifact", spy)
    candidate["summary"] = "line one\nline two"
    with pytest.raises(
        worker.WorkerResultError, match="RECEIPT_SUMMARY_NOT_SINGLE_LINE"
    ):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)

    assert final_publications == []
    assert not (context.outbox / "result.json").exists()
    assert not (context.outbox / "receipt.json").exists()

    candidate["summary"] = "corrected summary"
    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=descriptor
    )
    assert receipt["summary"] == "corrected summary"


def test_oversized_receipt_refusal_has_no_publication_and_retry_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    descriptor = _descriptor(tmp_path, [])
    candidate["summary"] = "x" * 2000

    with pytest.raises(worker.WorkerResultError, match="RECEIPT_TOO_LARGE"):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)
    assert not (context.outbox / "result.json").exists()
    assert not (context.outbox / "receipt.json").exists()

    candidate["summary"] = "corrected summary"
    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=descriptor
    )
    assert receipt["summary"] == "corrected summary"


def test_conflicting_receipt_is_preflighted_before_result_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    descriptor = _descriptor(tmp_path, [])
    receipt_path = context.outbox / "receipt.json"
    receipt_path.write_text("{}\n")
    receipt_path.chmod(0o600)
    original = worker.safe_output.write_new_artifact
    final_publications: list[str] = []

    def spy(path: Path, data: bytes):
        if path.name in {"result.json", "receipt.json"}:
            final_publications.append(path.name)
        return original(path, data)

    monkeypatch.setattr(worker.safe_output, "write_new_artifact", spy)
    with pytest.raises(worker.WorkerResultError, match="FINAL_RECEIPT_CONFLICT"):
        worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)
    assert final_publications == []
    assert not (context.outbox / "result.json").exists()

    receipt_path.unlink()
    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=descriptor
    )
    assert receipt["summary"] == candidate["summary"]


EXPORT_MODULE = ROOT / "skills/beads/scripts/external_export.py"


def _prepare_canonical_export(worker: Any, context: Any, candidate: dict) -> dict:
    """Create a real changed file and freeze the canonical export package."""
    export_module = _load(EXPORT_MODULE, f"beads_export_{candidate['attempt_id']}")
    snapshot_module = _load(
        LANE_SNAPSHOT, f"beads_export_snapshot_{candidate['attempt_id']}"
    )
    lane = Path(candidate["repository"]["worktree"])
    (lane / "src").mkdir()
    (lane / "src" / "change.py").write_text("change\n")
    package, manifest = export_module.build_package(
        lane, candidate["repository"]["base_sha"], ["src/change.py"]
    )
    export = context.outbox / "worker-export.tar"
    export.write_bytes(package)
    export.chmod(0o600)
    artifact = _identity(export, "external_export")
    candidate["artifacts"].append(artifact)
    candidate["worker_frozen_artifact"] = {
        "type": "external_export",
        "identity": manifest["tree_sha256"],
        "path": str(export),
        "sha256": artifact["sha256"],
        "tree_sha": manifest["tree_sha256"],
    }
    snapshot = snapshot_module.capture(
        lane, candidate["repository"]["base_sha"], exclude=context.outbox
    )
    candidate["repository"]["head_sha"] = snapshot.head_sha
    candidate["lane_state"] = snapshot.lane_state
    candidate["changes"] = {
        "paths": list(snapshot.changed_paths),
        "outside_allowed_scope": [],
    }
    return manifest


def test_external_export_frozen_sha_must_match_verified_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(
        tmp_path, monkeypatch, integration_mode="external_export"
    )
    _prepare_canonical_export(worker, context, candidate)
    candidate["worker_frozen_artifact"]["sha256"] = "0" * 64

    with pytest.raises(worker.WorkerResultError, match="EXPORT_ARTIFACT_HASH_MISMATCH"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert not (context.outbox / "result.json").exists()


@pytest.mark.parametrize(
    ("defect", "error_code"),
    [
        ("path", "EXPORT_ARTIFACT_MISSING"),
        ("type", "EXPORT_ARTIFACT_TYPE_MISMATCH"),
        ("tree_sha", "EXPORT_PACKAGE_TREE_MISMATCH"),
        ("actual_size", "ARTIFACT_SIZE_MISMATCH"),
        ("actual_hash", "ARTIFACT_HASH_MISMATCH"),
    ],
)
def test_external_export_contract_precedes_actual_file_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
    error_code: str,
) -> None:
    worker, context, candidate = _setup(
        tmp_path, monkeypatch, integration_mode="external_export"
    )
    _prepare_canonical_export(worker, context, candidate)
    export = Path(candidate["worker_frozen_artifact"]["path"])
    if defect == "path":
        candidate["worker_frozen_artifact"]["path"] = str(
            context.outbox / "different-export.tar"
        )
    elif defect == "type":
        candidate["artifacts"][-1]["type"] = "test_log"
    elif defect == "tree_sha":
        candidate["worker_frozen_artifact"]["tree_sha"] = "a" * 40
    elif defect == "actual_size":
        with export.open("ab") as stream:
            stream.write(b"x")
    else:
        raw = bytearray(export.read_bytes())
        raw[0] ^= 1
        export.write_bytes(bytes(raw))

    with pytest.raises(worker.WorkerResultError, match=error_code):
        worker.finalize_attempt(
            context,
            copy.deepcopy(candidate),
            sensitive_values_file=_descriptor(tmp_path, []),
        )


def test_external_export_exact_contract_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(
        tmp_path, monkeypatch, integration_mode="external_export"
    )
    manifest = _prepare_canonical_export(worker, context, candidate)

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
    )
    assert receipt["status"] == "completed"
    result = json.loads((context.outbox / "result.json").read_text())
    assert result["worker_frozen_artifact"]["tree_sha"] == manifest["tree_sha256"]
    assert result["worker_frozen_artifact"]["identity"] == manifest["tree_sha256"]


def _commit_artifact(head: str, tree: str) -> dict:
    descriptor = json.dumps(
        {"identity": head, "tree_sha": tree, "type": "commit"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "type": "commit",
        "identity": head,
        "path": None,
        "sha256": hashlib.sha256(descriptor).hexdigest(),
        "tree_sha": tree,
    }


def _lane_commit(worker: Any, context: Any, candidate: dict) -> tuple[str, str]:
    """Make one real commit in the lane and refresh the candidate snapshot."""
    import subprocess

    factory = _load(PACKET_TEST, f"commit_lane_factory_{candidate['attempt_id']}")
    lane = Path(candidate["repository"]["worktree"])
    (lane / "src").mkdir()
    (lane / "src" / "change.py").write_text("change\n")
    for argv in (
        [factory.GIT, "add", "-A"],
        [factory.GIT, "commit", "-q", "-m", "work"],
    ):
        subprocess.run(argv, cwd=lane, check=True, capture_output=True)
    head = subprocess.run(
        [factory.GIT, "rev-parse", "HEAD"],
        cwd=lane,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        [factory.GIT, "rev-parse", "HEAD^{tree}"],
        cwd=lane,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot_module = _load(
        LANE_SNAPSHOT, f"beads_lane_snapshot_recheck_{candidate['attempt_id']}"
    )
    snapshot = snapshot_module.capture(
        lane, candidate["repository"]["base_sha"], exclude=context.outbox
    )
    candidate["repository"]["head_sha"] = snapshot.head_sha
    candidate["lane_state"] = snapshot.lane_state
    candidate["changes"] = {
        "paths": list(snapshot.changed_paths),
        "outside_allowed_scope": [],
    }
    return head, tree


def test_commit_artifact_tree_must_match_real_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(
        tmp_path, monkeypatch, integration_mode="commit"
    )
    head, tree = _lane_commit(worker, context, candidate)
    candidate["worker_frozen_artifact"] = _commit_artifact(head, "0" * 40)

    with pytest.raises(worker.WorkerResultError, match="COMMIT_TREE_MISMATCH"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert not (context.outbox / "result.json").exists()


def test_commit_artifact_requires_base_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    worker = _load(WORKER_RESULT, f"beads_worker_finalize_anc_{tmp_path.name}")
    factory = _load(PACKET_TEST, f"anc_factory_{tmp_path.name}")
    snapshot_module = _load(LANE_SNAPSHOT, f"anc_snapshot_{tmp_path.name}")
    repo, lane, head = factory._git_lane(tmp_path)
    orphan = subprocess.run(
        [factory.GIT, "commit-tree", f"{head}^{{tree}}", "-m", "orphan"],
        cwd=lane,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert orphan != head
    packet = factory._packet(tmp_path)
    packet["repository"]["base_sha"] = orphan
    packet["scope"]["integration_mode"] = "commit"
    packet["scope"]["code_write"] = True
    packet["scope"]["local_commit"] = True
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "ok"]]
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    Path(packet["verification"]["worker_outbox"]).chmod(0o700)
    monkeypatch.chdir(lane)
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=41
    )
    evidence = worker.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
    )
    candidate = _candidate(packet, digest, evidence, snapshot_module)
    (lane / "src").mkdir()
    (lane / "src" / "change.py").write_text("change\n")
    for argv in (
        [factory.GIT, "add", "-A"],
        [factory.GIT, "commit", "-q", "-m", "work"],
    ):
        subprocess.run(argv, cwd=lane, check=True, capture_output=True)
    lane_head = subprocess.run(
        [factory.GIT, "rev-parse", "HEAD"],
        cwd=lane,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    lane_tree = subprocess.run(
        [factory.GIT, "rev-parse", "HEAD^{tree}"],
        cwd=lane,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot = snapshot_module.capture(lane, orphan, exclude=context.outbox)
    candidate["repository"]["head_sha"] = snapshot.head_sha
    candidate["lane_state"] = snapshot.lane_state
    candidate["changes"] = {
        "paths": list(snapshot.changed_paths),
        "outside_allowed_scope": [],
    }
    candidate["worker_frozen_artifact"] = _commit_artifact(lane_head, lane_tree)

    with pytest.raises(worker.WorkerResultError, match="COMMIT_BASE_NOT_ANCESTOR"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )


def test_commit_artifact_of_real_commit_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(
        tmp_path, monkeypatch, integration_mode="commit"
    )
    head, tree = _lane_commit(worker, context, candidate)
    candidate["worker_frozen_artifact"] = _commit_artifact(head, tree)

    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
    )
    assert receipt["status"] == "completed"


def test_external_export_rejects_arbitrary_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(
        tmp_path, monkeypatch, integration_mode="external_export"
    )
    export = context.outbox / "worker-export.tar"
    export.write_bytes(b"verified export")
    export.chmod(0o600)
    artifact = _identity(export, "external_export")
    candidate["artifacts"].append(artifact)
    candidate["worker_frozen_artifact"] = {
        "type": "external_export",
        "identity": "arbitrary-bytes",
        "path": str(export),
        "sha256": artifact["sha256"],
        "tree_sha": "a" * 40,
    }

    with pytest.raises(worker.WorkerResultError, match="EXPORT_PACKAGE_INVALID"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert not (context.outbox / "result.json").exists()


def test_finalize_rejects_caller_lane_state_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    candidate["lane_state"]["tracked_diff_sha256"] = "0" * 64

    with pytest.raises(worker.WorkerResultError, match="RESULT_LANE_STATE_DRIFT"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert not (context.outbox / "result.json").exists()


def test_finalize_rejects_caller_head_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    candidate["repository"]["head_sha"] = "0" * 40

    with pytest.raises(worker.WorkerResultError, match="RESULT_HEAD_DRIFT"):
        worker.finalize_attempt(
            context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert not (context.outbox / "result.json").exists()


def test_parent_validation_rejects_post_finalization_worktree_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, context, candidate = _setup(tmp_path, monkeypatch)
    receipt = worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path, [])
    )
    assert receipt["status"] == "completed"
    (Path(candidate["repository"]["worktree"]) / "late.txt").write_text("late\n")

    with pytest.raises(
        worker.validate_worker_execution_result.WorkerExecutionResultError,
        match="RESULT_LANE_STATE_DRIFT",
    ):
        worker.validate_worker_execution_result.validate_finalized_attempt(
            result_path=context.outbox / "result.json",
            receipt_path=context.outbox / "receipt.json",
            packet=context.packet,
            ownership_epoch=21,
        )
