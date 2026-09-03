"""Filesystem-only reconciliation and coordinator status/recover tests."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "beads" / "scripts"
sys.path.insert(0, str(SCRIPTS))
CLI = SCRIPTS / "beads_coordinator.py"


def _run_fixture(tmp_path: Path):
    state = importlib.import_module("coordinator_state")
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    run = root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    manifest = state.make_run_manifest(
        run_id=run.name,
        request_id="request-reconcile-0001",
        repository_root=str(tmp_path.resolve()),
        workspace=str((tmp_path / ".beads").resolve()),
        root_issue_id="root",
        run_root=str(root.resolve()),
        created_at="2026-09-03T12:00:00.000000Z",
        coordinator_version="test",
        run_secret_hex="ab" * 32,
    )
    state.publish_run_manifest(run, manifest)
    journal = state.OperationJournal.create(run)
    checkpoint = {
        "schema_version": "beads.run-checkpoint.v1",
        "run_id": run.name,
        "generation": 1,
        "root_issue_id": "root",
        "workspace": str((tmp_path / ".beads").resolve()),
        "repository_root": str(tmp_path.resolve()),
        "coordinator_session_id": None,
        "authority_snapshot_sha256": "2" * 64,
        "phase": "bootstrap",
        "budget": {
            "max_parallel": 3,
            "max_ready_fronts": 10,
            "max_worker_attempts_per_issue": 2,
            "max_nonprogress_rounds": 2,
        },
        "issues": {},
        "operation_journal_path": str((run / "operations.jsonl").resolve()),
        "issue_snapshot_sha256": "0" * 64,
        "ready_front_sha256": "0" * 64,
        "previous_checkpoint_sha256": "0" * 64,
        "created_at": "2026-09-03T12:00:00.000000Z",
    }
    state.CheckpointStore(run, journal).accept(checkpoint)
    return state, root, run


def test_status_validates_journal_checkpoint_manifest_and_ownership_without_mutation(
    tmp_path: Path,
) -> None:
    _state, root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
    plan = reconcile.status(run)
    after = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
    assert plan.disposition == "consistent"
    assert plan.accepted_generation == 1
    assert plan.next_safe_action == "continue_from_accepted_checkpoint"
    assert before == after
    assert plan.local_fencing_limit == "cooperative_local_filesystem_only"
    assert root.exists()


def test_recover_rebuilds_only_stale_pointer_and_ignores_unaccepted_generation(
    tmp_path: Path,
) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    checkpoints = run / "checkpoints"
    first = state.CheckpointStore(run, state.OperationJournal(run)).current()
    stray_value = {
        **first.value,
        "generation": 2,
        "previous_checkpoint_sha256": first.generation_sha256,
    }
    stray = checkpoints / "000002.json"
    stray.write_bytes(state.canonical_bytes(stray_value))
    stray.chmod(0o600)
    (checkpoints / "latest.json").write_text("{}\n")
    (checkpoints / "latest.json").chmod(0o600)

    plan = reconcile.recover(run)
    assert plan.disposition == "safe_to_retry"
    assert plan.repairs == ("checkpoint_pointer_rebuilt",)
    assert json.loads((checkpoints / "latest.json").read_text())["generation"] == 1
    assert stray.exists()


def test_unparseable_tail_is_preserved_and_becomes_terminal_unknown(
    tmp_path: Path,
) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    journal = run / "operations.jsonl"
    with journal.open("ab") as stream:
        stream.write(b'{"not":"complete"')
    original = journal.read_bytes()
    plan = reconcile.recover(run)
    assert plan.disposition == "manual_decision_required"
    assert plan.journal_status == "unknown"
    evidence = list((run / "recovery-evidence").glob("journal-*.bin"))
    assert len(evidence) == 1 and evidence[0].read_bytes() == original
    records = state.OperationJournal(run).read().records
    assert records[-1]["phase"] == "CORRUPT_TAIL"
    assert records[-1]["status"] == "UNKNOWN"
    assert reconcile.recover(run).repairs == ()


def test_interior_corruption_and_accepted_checkpoint_tamper_fail_closed_without_repair(
    tmp_path: Path,
) -> None:
    _state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    journal = run / "operations.jsonl"
    raw = journal.read_bytes()
    journal.write_bytes(
        raw.replace(b'"phase":"CHECKPOINT_ACCEPTED"', b'"phase":"CHECKPOINT_ACCEPTEd"')
    )
    with pytest.raises(reconcile.ReconciliationError, match="JOURNAL_INTERIOR_CORRUPT"):
        reconcile.recover(run)


def test_cli_exposes_only_readonly_status_and_filesystem_recover(
    tmp_path: Path,
) -> None:
    _state, _root, run = _run_fixture(tmp_path)
    status = subprocess.run(
        [sys.executable, str(CLI), "status", "--run-dir", str(run), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0
    value = json.loads(status.stdout)
    assert value["operation"] == "recover"
    assert value["status"] == "success"
    assert value["authority"]["exercised"] == ["read"]

    unknown = subprocess.run(
        [sys.executable, str(CLI), "start-run", "--run-dir", str(run)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unknown.returncode == 2
