from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "skills/beads/scripts/validate_worker_packet.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "beads_validate_worker_packet", MODULE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _packet(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    worktree = repo / "lane"
    outbox = worktree / "outbox"
    run_root = repo / ".hermes/beads-runs/run"
    imported = run_root / "lanes/key/outbox"
    for path in (worktree, outbox, imported):
        path.mkdir(parents=True, exist_ok=True)
    issue_id = "scc-é"
    return {
        "schema_version": "beads.worker-packet.v1",
        "run_id": "run-0123456789abcdef-20260903T120000.000000Z-ABCDEFGH",
        "attempt_id": "attempt-001",
        "goal": "test",
        "issue": {
            "id": issue_id,
            "key": hashlib.sha256(issue_id.encode()).hexdigest(),
            "title": "x",
            "snapshot_path": str(repo / "snapshot.json"),
            "snapshot_sha256": "a" * 64,
            "acceptance_ids": ["AC-1"],
        },
        "prerequisites": {
            "issue_ids": ["scc-pre"],
            "required_base_state": "all_closed",
        },
        "repository": {
            "root": str(repo),
            "base_sha": "b" * 40,
            "worktree": str(worktree),
            "branch": "hermes-beads/key/a001",
        },
        "scope": {
            "allowed_paths": ["src/**"],
            "forbidden_paths": [".beads/**"],
            "tracker_access": "readonly",
            "tracker_transport": "safe_bd_only",
            "code_write": True,
            "local_commit": False,
            "merge": False,
            "git_remote": False,
            "dolt_remote": False,
            "external_side_effects": False,
            "external_io": False,
            "destructive": False,
            "spend": False,
            "secret_access": False,
            "integration_mode": "patch_package",
        },
        "delegation": {"allowed": False, "max_child_depth": 0},
        "verification": {
            "required_commands": [["uv", "run", "pytest", "tests/x"]],
            "worker_outbox": str(outbox),
            "parent_import_root": str(imported),
            "advisory_wall_clock_seconds": 900,
            "required_child_max_iterations": 250,
            "required_child_timeout_seconds": 0,
            "required_max_spawn_depth": 1,
            "required_orchestrator_enabled": False,
            "advisory_max_tool_calls": 100,
            "enforcement": {
                "child_max_iterations": "hermes_runtime_hard_per_child",
                "child_timeout": "disabled_when_zero_else_hermes_runtime_hard_per_child",
                "spawn_depth": "coordinator_preflight",
                "packet_delegation_allowed": "worker_policy_only",
                "wall_clock": "parent_monitored_stop_then_reconcile",
                "tool_calls": "parent_monitored_stop_then_reconcile",
            },
            "max_artifact_bytes": 10485760,
        },
        "return_contract": {
            "schema": str(
                ROOT / "skills/beads/schemas/worker-execution-result-v1.schema.json"
            ),
            "max_manifest_bytes": 65536,
            "max_receipt_bytes": 2048,
        },
    }


def test_packet_identity_and_exact_command_authorization(tmp_path: Path) -> None:
    validator = _load()
    value = _packet(tmp_path)
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    packet = validator.validate_packet(
        raw, expected_packet_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert validator.authorize_command(packet, ["uv", "run", "pytest", "tests/x"]) == 0
    with pytest.raises(validator.PacketValidationError):
        validator.authorize_command(packet, ["uv", "run", "pytest"])


def test_packet_rejects_hash_path_and_command_attacks(tmp_path: Path) -> None:
    validator = _load()
    value = _packet(tmp_path)
    value["issue"]["key"] = "0" * 64
    with pytest.raises(validator.PacketValidationError, match="ISSUE_KEY_MISMATCH"):
        validator.validate_packet(json.dumps(value).encode())
    value = _packet(tmp_path)
    value["scope"]["allowed_paths"] = ["../escape"]
    with pytest.raises(validator.PacketValidationError):
        validator.validate_packet(json.dumps(value).encode())
    value = _packet(tmp_path)
    value["verification"]["required_commands"] = [["sh", "-c", "echo ok"]]
    with pytest.raises(validator.PacketValidationError, match="FORBIDDEN_COMMAND"):
        validator.validate_packet(json.dumps(value).encode())
    forged_schema = tmp_path / "worker-execution-result-v1.schema.json"
    forged_schema.write_text("{}", encoding="utf-8")
    value = _packet(tmp_path)
    value["return_contract"]["schema"] = str(forged_schema)
    with pytest.raises(validator.PacketValidationError, match="RESULT_SCHEMA_MISMATCH"):
        validator.validate_packet(json.dumps(value).encode())


def test_packet_requires_nested_orchestration_to_remain_disabled(
    tmp_path: Path,
) -> None:
    validator = _load()
    value = _packet(tmp_path)
    value["verification"]["required_orchestrator_enabled"] = True

    with pytest.raises(validator.PacketValidationError, match="PACKET_SCHEMA_INVALID"):
        validator.validate_packet(json.dumps(value).encode())


@pytest.mark.parametrize("wrapper", ["env", "command", "xargs", "sudo"])
def test_packet_rejects_command_wrappers(tmp_path: Path, wrapper: str) -> None:
    validator = _load()
    value = _packet(tmp_path)
    value["verification"]["required_commands"] = [
        [f"/usr/bin/{wrapper}", "bd", "show", "scc-main"]
    ]

    with pytest.raises(validator.PacketValidationError, match="FORBIDDEN_COMMAND"):
        validator.validate_packet(json.dumps(value).encode())


@pytest.mark.parametrize(
    "command",
    [
        ["git", "push", "origin", "main"],
        ["uv", "run", "python", "-c", "print(1)"],
        ["uv", "run", "python", "-m", "http.server"],
    ],
)
def test_packet_rejects_protected_nested_commands(
    tmp_path: Path, command: list[str]
) -> None:
    validator = _load()
    value = _packet(tmp_path)
    value["verification"]["required_commands"] = [command]

    with pytest.raises(validator.PacketValidationError, match="FORBIDDEN_COMMAND"):
        validator.validate_packet(json.dumps(value).encode())


@pytest.mark.parametrize(
    "allowed_path", [".beads/**", ".git/**", ".env", ".env.local", "**"]
)
def test_packet_rejects_protected_or_unbounded_write_scope(
    tmp_path: Path, allowed_path: str
) -> None:
    validator = _load()
    value = _packet(tmp_path)
    value["scope"]["allowed_paths"] = [allowed_path]

    with pytest.raises(validator.PacketValidationError, match="SCOPE_PROTECTED"):
        validator.validate_packet(json.dumps(value).encode())


def test_packet_loader_rejects_float_duplicate_and_oversize(tmp_path: Path) -> None:
    validator = _load()
    with pytest.raises(validator.PacketValidationError):
        validator.validate_packet(b'{"x":1,"x":2}')
    with pytest.raises(validator.PacketValidationError):
        validator.validate_packet(b'{"x":1.0}')
    with pytest.raises(validator.PacketValidationError, match="PACKET_TOO_LARGE"):
        validator.validate_packet(b" " * 65537)
