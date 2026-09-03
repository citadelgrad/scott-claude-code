from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path

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


def _packet(tmp_path: Path) -> tuple[dict, bytes, str]:
    factory = _load(PACKET_TEST, "worker_result_packet_factory")
    value = factory._packet(tmp_path)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return value, raw, hashlib.sha256(raw).hexdigest()


def test_initialize_binds_attempt_exclusively_and_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_lifecycle")
    value, raw, digest = _packet(tmp_path)
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])

    first = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=7
    )
    second = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=7
    )

    assert first == second
    assert first.packet_sha256 == digest
    assert first.ownership_epoch == 7
    state_path = outbox / "attempt.json"
    assert state_path.is_file() and not state_path.is_symlink()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    state = json.loads(state_path.read_text())
    assert state["run_id"] == value["run_id"]
    assert state["attempt_id"] == value["attempt_id"]
    assert state["issue_id"] == value["issue"]["id"]
    assert state["worktree"] == value["repository"]["worktree"]


def test_initialize_rejects_wrong_worktree_stale_epoch_and_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_identity")
    value, raw, digest = _packet(tmp_path)
    Path(value["verification"]["worker_outbox"]).chmod(0o700)

    with pytest.raises(worker.WorkerResultError, match="WRONG_WORKTREE"):
        worker.initialize_attempt(raw, expected_packet_sha256=digest, ownership_epoch=7)

    monkeypatch.chdir(value["repository"]["worktree"])
    worker.initialize_attempt(raw, expected_packet_sha256=digest, ownership_epoch=7)
    with pytest.raises(worker.WorkerResultError, match="STALE_OWNERSHIP_EPOCH"):
        worker.initialize_attempt(raw, expected_packet_sha256=digest, ownership_epoch=8)
    with pytest.raises(worker.WorkerResultError, match="PACKET_HASH_MISMATCH"):
        worker.initialize_attempt(
            raw, expected_packet_sha256="0" * 64, ownership_epoch=7
        )


def test_initialize_rejects_preexisting_outbox_data_and_duplicate_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_fresh_outbox")
    value, raw, digest = _packet(tmp_path)
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    (outbox / "prewritten.log").write_text("unsafe")
    monkeypatch.chdir(value["repository"]["worktree"])
    with pytest.raises(worker.WorkerResultError, match="OUTBOX_NOT_FRESH"):
        worker.initialize_attempt(raw, expected_packet_sha256=digest, ownership_epoch=9)

    (outbox / "prewritten.log").unlink()
    value["verification"]["required_commands"] *= 2
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(worker.WorkerResultError, match="DUPLICATE_DECLARED_COMMAND"):
        worker.initialize_attempt(raw, expected_packet_sha256=digest, ownership_epoch=9)


def test_attempt_rejects_changed_outbox_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_outbox_owner")
    value, raw, digest = _packet(tmp_path)
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=10
    )
    outbox.chmod(0o720)

    with pytest.raises(worker.WorkerResultError, match="OUTBOX_OWNER_INVALID"):
        worker.run_declared_command(
            context,
            command_index=0,
            sensitive_values_file=tmp_path / "not-read.json",
        )
