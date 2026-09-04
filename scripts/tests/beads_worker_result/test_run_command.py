from __future__ import annotations

import hashlib
import importlib.util
import subprocess
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


def _packet(tmp_path: Path, command: list[str]) -> tuple[dict, bytes, str]:
    factory = _load(PACKET_TEST, f"command_packet_factory_{tmp_path.name}")
    value = factory._packet(tmp_path)
    value["verification"]["required_commands"] = [command]
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return value, raw, hashlib.sha256(raw).hexdigest()


def _descriptor(tmp_path: Path, values: list[str]) -> Path:
    path = tmp_path / "sensitive-values.json"
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    return path


def test_run_command_uses_safe_output_and_records_immutable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_command")
    value, raw, digest = _packet(tmp_path, ["/usr/bin/printf", "safe output"])
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=11
    )

    evidence = worker.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
    )

    assert evidence["argv"] == ["/usr/bin/printf", "safe output"]
    assert evidence["safe_result"]["status"] == "SUCCESS"
    assert evidence["safe_result"]["exit_code"] == 0
    stdout = Path(evidence["safe_result"]["stdout_log"]["path"])
    assert stdout.read_text() == "safe output"
    assert stat.S_IMODE(stdout.stat().st_mode) == 0o600
    record = outbox / "command-000.json"
    assert record.is_file() and stat.S_IMODE(record.stat().st_mode) == 0o600
    repeated = worker.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
    )
    assert repeated == evidence


def test_run_command_rejects_undeclared_and_redacts_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_redaction")
    sentinel = "synthetic-worker-secret-0123456789"
    executable = tmp_path / "print-secret"
    executable.write_text(f"#!/bin/sh\nprintf '%s' '{sentinel}'\n")
    executable.chmod(0o700)
    value, raw, digest = _packet(tmp_path, [str(executable)])
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=12
    )

    with pytest.raises(worker.WorkerResultError, match="COMMAND_NOT_DECLARED"):
        worker.run_declared_command(
            context,
            command_index=1,
            sensitive_values_file=_descriptor(tmp_path, [sentinel]),
        )
    evidence = worker.run_declared_command(
        context,
        command_index=0,
        sensitive_values_file=_descriptor(tmp_path, [sentinel]),
    )

    persisted = b"".join(
        path.read_bytes() for path in outbox.iterdir() if path.is_file()
    )
    assert sentinel.encode() not in persisted
    assert (
        Path(evidence["safe_result"]["stdout_log"]["path"]).read_text() == "[REDACTED]"
    )


def test_run_command_never_dispatches_protected_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_denial")
    value, raw, digest = _packet(tmp_path, ["/usr/bin/true"])
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=13
    )
    called = False

    def reject(*args, **kwargs):
        raise worker.validate_worker_packet.PacketValidationError("FORBIDDEN_COMMAND")

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("safe_output must not receive protected command")

    monkeypatch.setattr(worker.validate_worker_packet, "authorize_command", reject)
    monkeypatch.setattr(worker.safe_output, "run_command", forbidden)
    with pytest.raises(worker.WorkerResultError, match="FORBIDDEN_COMMAND"):
        worker.run_declared_command(
            context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert called is False


def test_run_command_rejects_sensitive_argv_before_spawn_or_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_sensitive_argv")
    sentinel = "synthetic-argv-secret-0123456789"
    value, raw, digest = _packet(tmp_path, ["/usr/bin/printf", sentinel])
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=14
    )
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("sensitive argv must be rejected before spawn")

    monkeypatch.setattr(worker.safe_output, "run_command", forbidden)
    with pytest.raises(worker.WorkerResultError, match="COMMAND_ARGV_SENSITIVE"):
        worker.run_declared_command(
            context,
            command_index=0,
            sensitive_values_file=_descriptor(tmp_path, [sentinel]),
        )
    assert called is False
    assert not list(outbox.glob("command-*"))


def test_command_intent_is_immutable_before_spawn_and_unknown_is_never_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_unknown_outcome")
    command = ["/usr/bin/printf", "possibly ran"]
    value, raw, digest = _packet(tmp_path, command)
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=15
    )
    intent_path = outbox / "command-000.intent.json"
    calls = 0

    def fail_after_observing_intent(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert intent_path.is_file()
        assert stat.S_IMODE(intent_path.stat().st_mode) == 0o600
        intent = json.loads(intent_path.read_text())
        assert intent["command_index"] == 0
        assert intent["argv"] == command
        assert intent["packet_sha256"] == digest
        raise OSError("publication failed after command outcome became unknowable")

    monkeypatch.setattr(worker.safe_output, "run_command", fail_after_observing_intent)
    with pytest.raises(worker.WorkerResultError, match="COMMAND_OUTCOME_UNKNOWN"):
        worker.run_declared_command(
            context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
        )
    with pytest.raises(worker.WorkerResultError, match="COMMAND_OUTCOME_UNKNOWN"):
        worker.run_declared_command(
            context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert calls == 1


def test_partial_log_publication_preserves_hashes_and_refuses_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_partial_logs")
    value, raw, digest = _packet(tmp_path, ["/usr/bin/printf", "partial output"])
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=16
    )
    original_atomic_write = worker.safe_output._atomic_write
    spawn_calls = 0

    def fail_stderr(path: Path, data: bytes):
        if path.name == "command-000.stderr.log":
            raise OSError("injected stderr publication failure")
        return original_atomic_write(path, data)

    original_run_command = worker.safe_output.run_command

    def count_spawn(*args, **kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        return original_run_command(*args, **kwargs)

    monkeypatch.setattr(worker.safe_output, "_atomic_write", fail_stderr)
    monkeypatch.setattr(worker.safe_output, "run_command", count_spawn)
    with pytest.raises(worker.CommandOutcomeUnknownError) as captured:
        worker.run_declared_command(
            context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
        )

    stdout = outbox / "command-000.stdout.log"
    assert stdout.read_text() == "partial output"
    by_name = {Path(item["path"]).name: item for item in captured.value.artifacts}
    assert by_name[stdout.name] == {
        "path": str(stdout),
        "size_bytes": len(b"partial output"),
        "sha256": hashlib.sha256(b"partial output").hexdigest(),
    }
    assert captured.value.safe_next_action == "start_new_attempt"
    assert not (outbox / "command-000.json").exists()

    with pytest.raises(worker.CommandOutcomeUnknownError):
        worker.run_declared_command(
            context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert spawn_calls == 1


def test_failed_intent_publication_leaves_untouched_command_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_intent_retry")
    value, raw, digest = _packet(tmp_path, ["/usr/bin/printf", "retryable"])
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=17
    )
    original_write = worker.safe_output.write_new_artifact
    failed = False

    def fail_intent_before_publication(path: Path, data: bytes):
        nonlocal failed
        if path.name == "command-000.intent.json" and not failed:
            failed = True
            raise OSError("intent was not published")
        return original_write(path, data)

    monkeypatch.setattr(
        worker.safe_output, "write_new_artifact", fail_intent_before_publication
    )
    with pytest.raises(worker.WorkerResultError, match="COMMAND_INTENT_WRITE_FAILED"):
        worker.run_declared_command(
            context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
        )
    assert not list(outbox.glob("command-*"))

    evidence = worker.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path, [])
    )
    assert evidence["safe_result"]["status"] == "SUCCESS"


def test_run_command_executes_authorized_commit_mode_git_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_commit_mode")
    factory = _load(PACKET_TEST, f"commit_factory_{tmp_path.name}")
    repo, lane, head = factory._git_lane(tmp_path)
    (lane / "src.txt").write_text("change\n")
    commands = [
        [factory.GIT, "add", "-A"],
        [factory.GIT, "commit", "-q", "-m", "work"],
    ]
    value = factory._packet(tmp_path)
    value["repository"]["base_sha"] = head
    value["scope"]["integration_mode"] = "commit"
    value["scope"]["code_write"] = True
    value["scope"]["local_commit"] = True
    value["verification"]["required_commands"] = commands
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    outbox = Path(value["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    monkeypatch.chdir(value["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=31
    )

    for index in (0, 1):
        evidence = worker.run_declared_command(
            context,
            command_index=index,
            sensitive_values_file=_descriptor(tmp_path, []),
        )
        assert evidence["safe_result"]["status"] == "SUCCESS"

    new_head = subprocess.run(
        [factory.GIT, "rev-parse", "HEAD"],
        cwd=lane,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert new_head != head
