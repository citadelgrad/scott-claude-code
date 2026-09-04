"""Phase 0 P2 regression tests for the scc-0pu.11 repair wave.

Covers the two usability regressions recorded in the scc-0pu.9 adversarial
re-review of 4512e59..e0f209b:

1. untracked symlinks / oversized untracked files raise
   LANE_SNAPSHOT_UNTRACKED_INVALID without naming the path, for every outcome
   including honest failed reports;
2. ``git commit -C <commit>`` message reuse is over-blocked by the global
   Git path-override rejection.

Also freezes the worker-contract prose to the narrowed policy so prose and
code cannot drift apart again.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"
LANE_SNAPSHOT = SCRIPTS / "lane_snapshot.py"
WORKER_RESULT = SCRIPTS / "worker_result.py"
PACKET_TEST = ROOT / "scripts/tests/beads_contract/test_worker_packet.py"
WORKER_CONTRACT = ROOT / "skills/beads/references/worker-contract.md"
EVALS_README = ROOT / "skills/beads/evals/README.md"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _lane(tmp_path: Path):
    """Build a real repository lane and return (module, repo, lane, head)."""
    factory = _load(PACKET_TEST, f"phase0_factory_{tmp_path.name}")
    snapshot = _load(LANE_SNAPSHOT, f"phase0_lane_snapshot_{tmp_path.name}")
    repo, lane, head = factory._git_lane(tmp_path)
    return snapshot, repo, lane, head


def test_lane_snapshot_symlink_inventoried(tmp_path: Path) -> None:
    snapshot, repo, lane, head = _lane(tmp_path)
    outbox = lane / "outbox"
    outbox.mkdir(parents=True)
    target = lane / "src" / "target.txt"
    target.parent.mkdir()
    target.write_text("x\n")
    (lane / "src" / "link").symlink_to("target.txt")
    (lane / "src" / "dangling").symlink_to("does-not-exist")

    captured = snapshot.capture(lane, head, exclude=outbox)

    by_path = {item["path"]: item for item in captured.inventory}
    link = by_path["src/link"]
    assert link["kind"] == "symlink"
    assert link["size_bytes"] == 0
    assert link["sha256"] == hashlib.sha256(b"target.txt").hexdigest()
    dangling = by_path["src/dangling"]
    assert dangling["kind"] == "symlink"
    assert dangling["sha256"] == hashlib.sha256(b"does-not-exist").hexdigest()
    file_entry = by_path["src/target.txt"]
    assert file_entry["kind"] == "file"
    assert captured.dirty


def test_lane_snapshot_error_names_path(tmp_path: Path) -> None:
    snapshot, repo, lane, head = _lane(tmp_path)
    outbox = lane / "outbox"
    outbox.mkdir(parents=True)
    big = lane / "build" / "big.bin"
    big.parent.mkdir()
    big.write_bytes(b"0" * (snapshot.MAX_UNTRACKED_FILE_BYTES + 1))

    with pytest.raises(snapshot.LaneSnapshotError) as excinfo:
        snapshot.capture(lane, head, exclude=outbox)
    assert excinfo.value.code == "LANE_SNAPSHOT_UNTRACKED_INVALID"
    assert excinfo.value.path == "build/big.bin"
    assert "build/big.bin" in str(excinfo.value)


def test_lane_snapshot_packet_budget(tmp_path: Path) -> None:
    snapshot, repo, lane, head = _lane(tmp_path)
    outbox = lane / "outbox"
    outbox.mkdir(parents=True)
    small = lane / "src" / "small.txt"
    small.parent.mkdir()
    small.write_bytes(b"0" * 200)

    with pytest.raises(snapshot.LaneSnapshotError) as excinfo:
        snapshot.capture(lane, head, exclude=outbox, max_untracked_file_bytes=100)
    assert excinfo.value.path == "src/small.txt"

    # The module default still applies when the packet declares nothing.
    captured_default = snapshot.capture(
        lane, head, exclude=outbox, max_untracked_file_bytes=None
    )
    assert any(item["path"] == "src/small.txt" for item in captured_default.inventory)

    # Packet-declared budget is derived through the shared helper used at
    # worker finalization and parent validation seams.
    assert (
        snapshot.budget_from_packet({"verification": {"max_untracked_file_bytes": 5}})
        == 5
    )
    assert snapshot.budget_from_packet({"verification": {}}) == (
        snapshot.MAX_UNTRACKED_FILE_BYTES
    )
    with pytest.raises(snapshot.LaneSnapshotError):
        snapshot.budget_from_packet({"verification": {"max_untracked_file_bytes": 0}})


def test_lane_snapshot_honest_failed_can_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalize = _load(WORKER_RESULT, f"phase0_worker_result_{tmp_path.name}")
    factory = _load(PACKET_TEST, f"phase0_factory2_{tmp_path.name}")
    snapshot = _load(LANE_SNAPSHOT, f"phase0_lane_snapshot2_{tmp_path.name}")
    repo, lane, head = factory._git_lane(tmp_path)
    packet = factory._packet(tmp_path)
    packet["repository"]["base_sha"] = head
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "ok"]]
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    Path(packet["verification"]["worker_outbox"]).chmod(0o700)
    monkeypatch.chdir(packet["repository"]["worktree"])
    context = finalize.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=21
    )
    evidence = finalize.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path)
    )
    # Untracked symlink outside the outbox: an honest failed report must not
    # be blocked by snapshot capture.
    node_modules = Path(packet["repository"]["worktree"]) / "node_modules"
    node_modules.symlink_to("/nonexistent-store")

    outbox = Path(packet["verification"]["worker_outbox"])
    captured = snapshot.capture(
        Path(packet["repository"]["worktree"]), head, exclude=outbox
    )
    record = outbox / "command-000.json"
    stdout = Path(evidence["safe_result"]["stdout_log"]["path"])
    stderr = Path(evidence["safe_result"]["stderr_log"]["path"])

    def _identity(path: Path, artifact_type: str) -> dict:
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
        "status": "failed",
        "repository": {
            "worktree": packet["repository"]["worktree"],
            "branch": packet["repository"]["branch"],
            "base_sha": head,
            "head_sha": captured.head_sha,
        },
        "lane_state": captured.lane_state,
        "changes": {
            "paths": list(captured.changed_paths),
            # The untracked node_modules symlink sits outside packet scope
            # (src/**); an honest failed report must still declare it.
            "outside_allowed_scope": [
                item for item in captured.changed_paths if item == "node_modules"
            ],
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
                "status": "failed",
                "evidence_paths": [str(record)],
            }
        ],
        "artifacts": [
            _identity(record, "command_evidence"),
            _identity(stdout, "stdout_log"),
            _identity(stderr, "stderr_log"),
        ],
        "blockers": [],
        "errors": [
            {
                "code": "WORK_FAILED",
                "template_id": "worker_error",
                "field_path": "/",
                "parameters": [],
            }
        ],
        "cancellation_reason": None,
        "skipped_checks": [],
        "residual_risks": [],
        "summary": "honest failure",
        "integration_mode": packet["scope"]["integration_mode"],
        "worker_frozen_artifact": None,
    }

    receipt = finalize.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path)
    )
    assert receipt["status"] == "failed"


def _descriptor(tmp_path: Path) -> Path:
    path = tmp_path / "sensitive-values.json"
    if not path.exists():
        path.write_text("[]")
    path.chmod(0o600)
    return path


def _commit_packet_with_commands(tmp_path: Path, commands: list[list[str]]):
    factory = _load(PACKET_TEST, f"phase0_factory3_{tmp_path.name}")
    _, _, head = factory._git_lane(tmp_path)
    value = factory._packet(tmp_path)
    value["repository"]["base_sha"] = head
    value["scope"]["integration_mode"] = "commit"
    value["scope"]["code_write"] = True
    value["scope"]["local_commit"] = True
    value["verification"]["required_commands"] = commands
    return value


def test_git_commit_message_reuse_allowed(tmp_path: Path) -> None:
    validator = _load(
        SCRIPTS / "validate_worker_packet.py",
        f"phase0_vwp_{tmp_path.name}",
    )
    commands = [["git", "add", "A"], ["git", "commit", "-C", "0" * 40]]
    value = _commit_packet_with_commands(tmp_path, commands)
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    packet = validator.validate_packet(raw)
    assert validator.authorize_command(packet, tuple(commands[1])) == 1


def test_git_dash_c_before_subcommand_still_rejected(tmp_path: Path) -> None:
    validator = _load(
        SCRIPTS / "validate_worker_packet.py",
        f"phase0_vwp2_{tmp_path.name}",
    )
    base = _commit_packet_with_commands(tmp_path, [["git", "status"]])
    for argv in (
        ["git", "-C", "/etc", "status"],
        ["git", "status", "--git-dir=x"],
        ["git", "-Cevil", "log"],
        ["git", "status", "--work-tree", "/elsewhere"],
    ):
        value = json.loads(json.dumps(base))
        value["verification"]["required_commands"] = [list(argv)]
        with pytest.raises(validator.PacketValidationError, match="FORBIDDEN_COMMAND"):
            validator.validate_packet(
                json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
            )


def test_worker_contract_prose_matches_policy() -> None:
    text = WORKER_CONTRACT.read_text(encoding="utf-8")
    assert "are always rejected" not in text
    assert "git commit -C <commit>" in text
    assert "before the Git subcommand" in text


def test_evals_readme_names_required_records_flag() -> None:
    text = EVALS_README.read_text(encoding="utf-8")
    assert "--records" in text
