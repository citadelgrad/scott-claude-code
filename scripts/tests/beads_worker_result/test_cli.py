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
VALIDATOR = ROOT / "skills/beads/scripts/validate_worker_execution_result.py"
PACKET_TEST = ROOT / "scripts/tests/beads_contract/test_worker_packet.py"
FINALIZE_TEST = ROOT / "scripts/tests/beads_worker_result/test_finalize.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_result_and_validator_cli_complete_packet_bound_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_cli")
    validator = _load(VALIDATOR, "beads_worker_result_validator_cli")
    factory = _load(PACKET_TEST, "worker_result_cli_packet_factory")
    result_factory = _load(FINALIZE_TEST, "worker_result_cli_result_factory")
    repo, lane, head = factory._git_lane(tmp_path)
    packet = factory._packet(tmp_path)
    packet["repository"]["base_sha"] = head
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "ok"]]
    outbox = Path(packet["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    packet_path = tmp_path / "packet.json"
    packet_bytes = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    packet_path.write_bytes(packet_bytes)
    packet_digest = hashlib.sha256(packet_bytes).hexdigest()
    descriptor = result_factory._descriptor(tmp_path, [])
    monkeypatch.chdir(packet["repository"]["worktree"])
    common = [
        "--worker-packet",
        str(packet_path),
        "--expected-packet-sha256",
        packet_digest,
        "--ownership-epoch",
        "31",
    ]

    assert worker.main(["initialize", *common]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "attempt_id": packet["attempt_id"],
        "status": "initialized",
    }
    assert (
        worker.main(
            [
                "run-command",
                *common,
                "--command-index",
                "0",
                "--sensitive-values-file",
                str(descriptor),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "SUCCESS"
    evidence = json.loads((outbox / "command-000.json").read_text())
    candidate = result_factory._candidate(packet, packet_digest, evidence)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate))

    assert (
        worker.main(
            [
                "finalize",
                *common,
                "--input",
                str(candidate_path),
                "--sensitive-values-file",
                str(descriptor),
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"

    assert (
        validator.main(
            [
                "--worker-packet",
                str(packet_path),
                "--expected-packet-sha256",
                packet_digest,
                "--ownership-epoch",
                "31",
                "--result",
                str(outbox / "result.json"),
                "--receipt",
                str(outbox / "receipt.json"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "result_sha256": receipt["result_sha256"],
        "status": "valid",
    }


def test_cli_refusals_are_generic_and_do_not_echo_sensitive_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_cli_refusal")
    sentinel = "synthetic-cli-secret-0123456789"
    bad_packet = tmp_path / "bad-packet.json"
    bad_packet.write_text(sentinel)

    assert (
        worker.main(
            [
                "initialize",
                "--worker-packet",
                str(bad_packet),
                "--expected-packet-sha256",
                "0" * 64,
                "--ownership-epoch",
                "1",
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert sentinel not in output
    assert json.loads(output) == {
        "error_code": "WORKER_RESULT_REFUSED",
        "status": "REFUSED",
    }


def test_cli_reports_typed_unknown_command_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker = _load(WORKER_RESULT, "beads_worker_result_cli_unknown")
    factory = _load(PACKET_TEST, "worker_result_cli_unknown_packet_factory")
    result_factory = _load(FINALIZE_TEST, "worker_result_cli_unknown_result_factory")
    packet = factory._packet(tmp_path)
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "maybe"]]
    outbox = Path(packet["verification"]["worker_outbox"])
    outbox.chmod(0o700)
    packet_path = tmp_path / "packet.json"
    packet_bytes = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    packet_path.write_bytes(packet_bytes)
    digest = hashlib.sha256(packet_bytes).hexdigest()
    descriptor = result_factory._descriptor(tmp_path, [])
    monkeypatch.chdir(packet["repository"]["worktree"])

    def publication_failure(*args, **kwargs):
        raise OSError("outcome unknown")

    monkeypatch.setattr(worker.safe_output, "run_command", publication_failure)
    assert (
        worker.main(
            [
                "run-command",
                "--worker-packet",
                str(packet_path),
                "--expected-packet-sha256",
                digest,
                "--ownership-epoch",
                "32",
                "--command-index",
                "0",
                "--sensitive-values-file",
                str(descriptor),
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["error_code"] == "COMMAND_OUTCOME_UNKNOWN"
    assert output["safe_next_action"] == "start_new_attempt"
    assert output["status"] == "REFUSED"
    assert [Path(item["path"]).name for item in output["artifacts"]] == [
        "command-000.intent.json"
    ]
