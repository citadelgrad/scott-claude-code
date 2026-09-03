from __future__ import annotations

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


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, dict]:
    worker = _load(WORKER_RESULT, f"beads_worker_finalize_{tmp_path.name}")
    factory = _load(PACKET_TEST, f"finalize_packet_factory_{tmp_path.name}")
    packet = factory._packet(tmp_path)
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
    return worker, context, _candidate(packet, digest, evidence)


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


def _candidate(packet: dict, packet_digest: str, evidence: dict) -> dict:
    outbox = Path(packet["verification"]["worker_outbox"])
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
            "head_sha": packet["repository"]["base_sha"],
        },
        "lane_state": {
            "tracked_diff_sha256": hashlib.sha256(b"").hexdigest(),
            "untracked_inventory_sha256": hashlib.sha256(b"").hexdigest(),
            "dirty": False,
        },
        "changes": {"paths": [], "outside_allowed_scope": []},
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
