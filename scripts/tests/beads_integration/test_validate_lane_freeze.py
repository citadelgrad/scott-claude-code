"""RED-first tests for validate_lane_freeze.py (scc-0pu.11 / t10)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"
PACKET_TEST = ROOT / "scripts/tests/beads_contract/test_worker_packet.py"
MODULE = SCRIPTS / "validate_lane_freeze.py"
EPOCH = 21

_counter = {"n": 0}


def _load(path: Path):
    _counter["n"] += 1
    spec = importlib.util.spec_from_file_location(
        f"beads_module_{_counter['n']}_{path.stem}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def _lane(tmp_path: Path, monkeypatch):
    """Complete one honest patch_package attempt and freeze it."""
    factory = _load(PACKET_TEST)
    snapshot = _load(SCRIPTS / "lane_snapshot.py")
    worker = _load(SCRIPTS / "worker_result.py")
    validator = _load(SCRIPTS / "validate_worker_execution_result.py")
    packet_module = _load(SCRIPTS / "validate_worker_packet.py")
    pkg = _load(SCRIPTS / "package_lane.py")
    vlf = _load(MODULE)

    repo, lane, head = factory._git_lane(tmp_path)
    packet = factory._packet(tmp_path)
    packet["repository"]["base_sha"] = head
    packet["verification"]["required_commands"] = [["/usr/bin/printf", "ok"]]
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    Path(packet["verification"]["worker_outbox"]).chmod(0o700)
    monkeypatch.chdir(packet["repository"]["worktree"])
    context = worker.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=EPOCH
    )
    evidence = worker.run_declared_command(
        context, command_index=0, sensitive_values_file=_descriptor(tmp_path)
    )
    src = Path(packet["repository"]["worktree"]) / "src"
    src.mkdir()
    (src / "code.py").write_text("print('lane')\n")

    outbox = Path(packet["verification"]["worker_outbox"])
    captured = snapshot.capture(
        Path(packet["repository"]["worktree"]), head, exclude=outbox
    )
    record = outbox / "command-000.json"
    stdout = Path(evidence["safe_result"]["stdout_log"]["path"])
    stderr = Path(evidence["safe_result"]["stderr_log"]["path"])

    def _identity(path, artifact_type):
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
        "status": "completed",
        "repository": {
            "worktree": packet["repository"]["worktree"],
            "branch": packet["repository"]["branch"],
            "base_sha": head,
            "head_sha": captured.head_sha,
        },
        "lane_state": captured.lane_state,
        "changes": {"paths": list(captured.changed_paths), "outside_allowed_scope": []},
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
        "integration_mode": "patch_package",
        "worker_frozen_artifact": None,
    }
    worker.finalize_attempt(
        context, candidate, sensitive_values_file=_descriptor(tmp_path)
    )
    validated_packet = packet_module.validate_packet(raw)
    validated_result = validator.validate_finalized_attempt(
        result_path=outbox / "result.json",
        receipt_path=outbox / "receipt.json",
        packet=validated_packet,
        ownership_epoch=EPOCH,
    )
    return {
        "vlf": vlf,
        "pkg": pkg,
        "packet": validated_packet,
        "result": validated_result,
        "lane": Path(packet["repository"]["worktree"]),
        "outbox": outbox,
        "head": head,
        "digest": digest,
        "packet_dict": packet,
    }


def _descriptor(tmp_path: Path) -> Path:
    path = tmp_path / "sensitive-values.json"
    if not path.exists():
        path.write_text("[]")
    path.chmod(0o600)
    return path


def _freeze_record(fixture, tmp_path: Path) -> tuple[dict, bytes, Path]:
    pkg = fixture["pkg"]
    lane_dir = tmp_path / "lanes" / "key"
    lane_dir.mkdir(parents=True)
    archive_path = lane_dir / "lane-package.tar"
    built = pkg.build_lane_package(
        fixture["lane"],
        fixture["head"],
        outbox=fixture["outbox"],
        scope={
            "allowed_paths": fixture["packet"].value["scope"]["allowed_paths"],
            "forbidden_paths": fixture["packet"].value["scope"]["forbidden_paths"],
        },
        packet_budgets={},
        identity={
            "run_id": fixture["packet"].value["run_id"],
            "issue_id": fixture["packet"].value["issue"]["id"],
            "attempt_id": fixture["packet"].value["attempt_id"],
            "ownership_epoch": EPOCH,
            "worker_result_sha256": fixture["result"].result_sha256,
            "artifact_path": str(archive_path),
        },
    )
    archive_path.write_bytes(built.archive_bytes)
    return built.lane_freeze, _canonical(built.lane_freeze), archive_path


def test_valid_freeze_passes(tmp_path: Path, monkeypatch) -> None:
    fixture = _lane(tmp_path, monkeypatch)
    vlf = fixture["vlf"]
    freeze, freeze_bytes, _ = _freeze_record(fixture, tmp_path)

    validated = vlf.validate_lane_freeze(
        freeze_bytes,
        result=fixture["result"],
        packet=fixture["packet"],
        ownership_epoch=EPOCH,
        worktree_live=True,
    )
    assert validated.value == freeze
    assert validated.freeze_sha256 == hashlib.sha256(freeze_bytes).hexdigest()


def test_forged_freeze_refusals_are_typed(tmp_path: Path, monkeypatch) -> None:
    fixture = _lane(tmp_path, monkeypatch)
    vlf = fixture["vlf"]
    freeze, freeze_bytes, archive_path = _freeze_record(fixture, tmp_path)

    def check(mutate, code):
        forged = json.loads(json.dumps(freeze))
        raw = mutate(forged, archive_path)
        with pytest.raises(vlf.LaneFreezeError, match=code):
            vlf.validate_lane_freeze(
                raw,
                result=fixture["result"],
                packet=fixture["packet"],
                ownership_epoch=EPOCH,
                worktree_live=True,
            )

    check(
        lambda f, _: _canonical({**f, "worker_result_sha256": "0" * 64}),
        "LANE_FREEZE_IDENTITY_MISMATCH",
    )
    check(
        lambda f, _: _canonical({**f, "issue_id": "scc-other"}),
        "LANE_FREEZE_IDENTITY_MISMATCH",
    )
    # AC-T10-002 names a stale attempt and a stale epoch separately from a
    # wrong issue. All three are enforced by the same identity comparison, but
    # a refactor that dropped any one key from that tuple would still pass
    # every other case here, so each gets its own forgery. Each substitute
    # value is schema-valid on purpose -- an out-of-pattern id would be
    # refused by the schema check first and prove nothing about identity.
    check(
        lambda f, _: _canonical({**f, "attempt_id": "attempt-999"}),
        "LANE_FREEZE_IDENTITY_MISMATCH",
    )
    check(
        lambda f, _: _canonical({**f, "ownership_epoch": EPOCH + 1}),
        "LANE_FREEZE_IDENTITY_MISMATCH",
    )
    check(
        lambda f, _: _canonical(
            {
                **f,
                "run_id": "run-0000000000000000-20260101T000000.000000Z-AAAAAAAA",
            }
        ),
        "LANE_FREEZE_IDENTITY_MISMATCH",
    )
    check(
        lambda f, _: _canonical({**f, "transfer_mode": "commit"}),
        "LANE_FREEZE_MODE_MISMATCH",
    )
    check(
        lambda f, _: _canonical({**f, "base_sha": "0" * 40}),
        "LANE_FREEZE_BASE_MISMATCH",
    )
    check(
        lambda f, _: _canonical({**f, "observed_head_sha": "0" * 40}),
        "LANE_FREEZE_HEAD_MISMATCH",
    )
    # Non-canonical bytes: pretty-printed JSON of the same value.
    with pytest.raises(vlf.LaneFreezeError, match="LANE_FREEZE_NOT_CANONICAL"):
        vlf.validate_lane_freeze(
            json.dumps(freeze, indent=2).encode() + b"\n",
            result=fixture["result"],
            packet=fixture["packet"],
            ownership_epoch=EPOCH,
            worktree_live=True,
        )
    # Schema-invalid: unknown extra field.
    with pytest.raises(vlf.LaneFreezeError, match="LANE_FREEZE_SCHEMA_INVALID"):
        vlf.validate_lane_freeze(
            _canonical({**freeze, "extra": 1}),
            result=fixture["result"],
            packet=fixture["packet"],
            ownership_epoch=EPOCH,
            worktree_live=True,
        )
    # Artifact hash drift: rewrite the archive by one byte.
    archive_path.write_bytes(archive_path.read_bytes() + b"junk")
    with pytest.raises(vlf.LaneFreezeError, match="LANE_FREEZE_ARTIFACT_INVALID"):
        vlf.validate_lane_freeze(
            freeze_bytes,
            result=fixture["result"],
            packet=fixture["packet"],
            ownership_epoch=EPOCH,
            worktree_live=True,
        )


def test_failed_reproduction_never_freezes(tmp_path: Path, monkeypatch) -> None:
    fixture = _lane(tmp_path, monkeypatch)
    vlf = fixture["vlf"]
    freeze, freeze_bytes, _ = _freeze_record(fixture, tmp_path)
    forged = {**freeze, "reproduction_status": "failed"}
    with pytest.raises(vlf.LaneFreezeError, match="LANE_FREEZE_REPRODUCTION"):
        vlf.validate_lane_freeze(
            _canonical(forged),
            result=fixture["result"],
            packet=fixture["packet"],
            ownership_epoch=EPOCH,
            worktree_live=True,
        )


def test_inventory_drift_refused(tmp_path: Path, monkeypatch) -> None:
    fixture = _lane(tmp_path, monkeypatch)
    vlf = fixture["vlf"]
    freeze, _, _ = _freeze_record(fixture, tmp_path)
    # One extra untracked file appears in the lane after packaging.
    (fixture["lane"] / "src" / "late.txt").write_text("late\n")
    with pytest.raises(vlf.LaneFreezeError, match="LANE_FREEZE_LANE_DRIFT"):
        vlf.validate_lane_freeze(
            _canonical(freeze),
            result=fixture["result"],
            packet=fixture["packet"],
            ownership_epoch=EPOCH,
            worktree_live=True,
        )
    # Without the live worktree the same freeze validates from bytes alone.
    validated = vlf.validate_lane_freeze(
        _canonical(freeze),
        result=fixture["result"],
        packet=fixture["packet"],
        ownership_epoch=EPOCH,
        worktree_live=False,
    )
    assert validated.value["reproduction_status"] == "reproduced"
