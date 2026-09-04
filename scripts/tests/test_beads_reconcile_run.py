"""Filesystem-only reconciliation and coordinator status/recover tests."""

from __future__ import annotations

import importlib
import hashlib
import json
import stat
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
        workspace_identity_sha256="c" * 64,
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
        "workspace_identity_sha256": "c" * 64,
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
    state.create_owner_file(run / "run.lock", root=run)
    return state, root, run


def _path_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str | None]]:
    snapshot: dict[str, tuple[int, int, int, int, str | None]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.stat(follow_symlinks=False)
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.S_ISREG(metadata.st_mode)
            else None
        )
        snapshot[str(path.relative_to(root))] = (
            metadata.st_ino,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
            metadata.st_nlink,
            digest,
        )
    return snapshot


def _acquire_root(root: Path, run: Path, *, operation_id: str = "a" * 64):
    ownership = importlib.import_module("beads_ownership")
    return ownership.OwnershipStore(root).acquire(
        issue_id="root",
        actor="tester",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id=operation_id,
    )


def test_status_unheld_ownership_requires_explicit_safe_reacquisition(
    tmp_path: Path,
) -> None:
    _state, root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")

    plan = reconcile.status(run)

    assert plan.disposition == "consistent"
    assert plan.ownership_status == "unheld"
    assert plan.next_safe_action == "reacquire_ownership_and_resume"


def test_status_unknown_ownership_requires_manual_reconciliation_non_success(
    tmp_path: Path,
) -> None:
    import datetime as dt

    _state, root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    ownership = importlib.import_module("beads_ownership")
    _acquire_root(root, run)
    with pytest.raises(ownership.OwnershipError, match="OWNERSHIP_LEASE_EXPIRED"):
        ownership.OwnershipStore(root).renew(
            "root",
            run,
            epoch=1,
            operation_id="b" * 64,
            now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )

    plan = reconcile.status(run)

    assert plan.disposition == "manual_decision_required"
    assert plan.ownership_status == "unknown"
    assert plan.next_safe_action == "manual_reconciliation"


def test_conflict_ownership_maps_to_manual_reconciliation() -> None:
    reconcile = importlib.import_module("reconcile_run")

    # A persisted conflict record (defensive store mapping) never continues
    # automatically: it must report manual reconciliation non-success.
    assert reconcile._ownership_next_action("conflict") == (
        "manual_decision_required",
        "manual_reconciliation",
    )
    assert reconcile._ownership_next_action("unknown") == (
        "manual_decision_required",
        "manual_reconciliation",
    )


def test_status_release_prepared_ownership_requires_manual_reconciliation(
    tmp_path: Path,
) -> None:
    _state, root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    ownership = importlib.import_module("beads_ownership")
    _acquire_root(root, run)

    def crash_after_release_prepared(event: str) -> None:
        if event == "after_ownership_current_publication":
            raise RuntimeError("crash after release_prepared publication")

    store = ownership.OwnershipStore(root, crash_hook=crash_after_release_prepared)
    with pytest.raises(RuntimeError):
        store.release("root", run, epoch=1, operation_id="e" * 64)

    plan = reconcile.status(run)

    assert plan.disposition == "manual_decision_required"
    assert plan.ownership_status == "unknown"
    assert plan.next_safe_action == "manual_reconciliation"


def test_status_released_ownership_requires_explicit_safe_reacquisition(
    tmp_path: Path,
) -> None:
    _state, root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    ownership = importlib.import_module("beads_ownership")
    _acquire_root(root, run)
    released = ownership.OwnershipStore(root).release(
        "root", run, epoch=1, operation_id="d" * 64
    )
    assert released.disposition == "released"

    plan = reconcile.status(run)

    assert plan.disposition == "consistent"
    assert plan.ownership_status == "released"
    assert plan.next_safe_action == "reacquire_ownership_and_resume"


def test_status_validates_journal_checkpoint_manifest_and_ownership_without_mutation(
    tmp_path: Path,
) -> None:
    _state, root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    before = _path_snapshot(run.parent)
    plan = reconcile.status(run)
    after = _path_snapshot(run.parent)
    assert plan.disposition == "consistent"
    assert plan.accepted_generation == 1
    assert plan.next_safe_action == "reacquire_ownership_and_resume"
    assert before == after
    assert plan.local_fencing_limit == "cooperative_local_filesystem_only"
    assert root.exists()


def test_status_missing_lock_is_read_only_unknown(tmp_path: Path) -> None:
    _state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    (run / "run.lock").unlink()
    before = _path_snapshot(run.parent)

    with pytest.raises(reconcile.ReconciliationError, match="LOCK_MISSING") as raised:
        reconcile.status(run)

    assert raised.value.status == "unknown"
    assert _path_snapshot(run.parent) == before
    assert not (run / "run.lock").exists()


def test_status_rejects_wrong_mode_extra_file_without_mutation(tmp_path: Path) -> None:
    _state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    extra = run / "unrelated.txt"
    extra.write_text("unsafe mode", encoding="utf-8")
    extra.chmod(0o644)
    before = _path_snapshot(run.parent)

    with pytest.raises(reconcile.ReconciliationError, match="PATH_MODE_INVALID"):
        reconcile.status(run)

    assert _path_snapshot(run.parent) == before


def test_cli_status_missing_lock_does_not_create_any_path(tmp_path: Path) -> None:
    _state, _root, run = _run_fixture(tmp_path)
    (run / "run.lock").unlink()
    before = _path_snapshot(run.parent)

    result = subprocess.run(
        [sys.executable, str(CLI), "status", "--run-dir", str(run), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 5
    assert json.loads(result.stdout)["error_code"] == "LOCK_MISSING"
    assert _path_snapshot(run.parent) == before


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


def test_recover_reconstructs_current_from_durable_ownership_history(
    tmp_path: Path,
) -> None:
    state, root, run = _run_fixture(tmp_path)
    ownership = importlib.import_module("beads_ownership")
    reconcile = importlib.import_module("reconcile_run")
    ownership.OwnershipStore(root).acquire(
        issue_id="root",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
    )
    owner_dir = root / "_ownership" / state.issue_key("root")
    (owner_dir / "current.json").unlink()

    plan = reconcile.recover(run)

    assert plan.disposition == "safe_to_retry"
    assert "ownership_current_rebuilt" in plan.repairs
    assert (owner_dir / "current.json").exists()
    assert reconcile.status(run).ownership_status == "held"


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
    repaired_journal = journal.read_bytes()
    repaired_evidence = evidence[0].read_bytes()

    repeated = reconcile.recover(run)
    assert repeated.disposition == "manual_decision_required"
    assert repeated.journal_status == "unknown"
    observed = reconcile.status(run)
    assert observed.disposition == "manual_decision_required"
    assert observed.journal_status == "unknown"
    assert journal.read_bytes() == repaired_journal
    assert evidence[0].read_bytes() == repaired_evidence


def _torn_prepared(state, run: Path, *, probe_type: str = "filesystem_identity"):
    input_path = run / "run.json"
    input_sha = state.sha256_bytes(input_path.read_bytes())
    target = run / "effect.json"
    target.write_bytes(b'{"applied":true}\n')
    target.chmod(0o600)
    intended_sha = state.sha256_bytes(target.read_bytes())
    identity = {
        "schema": "beads.test-effect.v1",
        "effect_type": "FILESYSTEM_TEST",
        "target_identity": str(target.resolve()),
        "immutable_input_sha256": input_sha,
        "ownership_epoch": None,
    }
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": state.semantic_operation_id(identity),
        "run_id": run.name,
        "attempt_id": None,
        "issue_id": None,
        "ownership_epoch": None,
        "effect_type": "FILESYSTEM_TEST",
        "immutable_input_path": str(input_path.resolve()),
        "immutable_input_sha256": input_sha,
        "expected_pre_state_sha256": "0" * 64,
        "recovery_probe": {
            "schema_version": "beads.recovery-probe.v1",
            "kind": "request",
            "probe_type": probe_type,
            "target_identity": str(target.resolve()),
            "expected_before_sha256": "0" * 64,
            "intended_after_identity": str(target.resolve()),
            "intended_after_sha256": intended_sha,
            "descriptor": [probe_type, "test"],
            "timeout_seconds": 1,
            "required_authority": "local_write",
        },
        "timestamp": "2026-09-03T12:01:00.000000Z",
        "authority_class": "local_write",
        "previous_event_sha256": state.OperationJournal(run).read().last_sha256,
        "phase": "PREPARED",
    }


def test_torn_prepared_is_normalized_probed_and_resolved_once(tmp_path: Path) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    prepared = _torn_prepared(state, run)
    with (run / "operations.jsonl").open("ab") as stream:
        stream.write(state.canonical_payload_bytes(prepared))

    first = reconcile.recover(run)

    records = state.OperationJournal(run).read().records
    assert [record["phase"] for record in records[-2:]] == ["PREPARED", "RESOLUTION"]
    assert records[-1]["status"] == "APPLIED"
    assert (
        records[-1]["observed_post_state_sha256"]
        == prepared["recovery_probe"]["intended_after_sha256"]
    )
    assert first.disposition == "safe_to_retry"
    journal_bytes = (run / "operations.jsonl").read_bytes()
    second = reconcile.recover(run)
    assert second.disposition == "consistent"
    assert (run / "operations.jsonl").read_bytes() == journal_bytes


def test_torn_prepared_with_unsupported_probe_resolves_unknown(tmp_path: Path) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    prepared = _torn_prepared(state, run, probe_type="tracker_state")
    with (run / "operations.jsonl").open("ab") as stream:
        stream.write(state.canonical_payload_bytes(prepared))

    plan = reconcile.recover(run)

    records = state.OperationJournal(run).read().records
    assert records[-1]["phase"] == "RESOLUTION"
    assert records[-1]["status"] == "UNKNOWN"
    assert plan.disposition == "manual_decision_required"


@pytest.mark.parametrize("terminal_status", ["UNKNOWN", "CONFLICT"])
def test_complete_terminal_resolution_remains_manual_across_status_and_recovery(
    tmp_path: Path, terminal_status: str
) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    journal = state.OperationJournal(run)
    prepared = journal.append(_torn_prepared(state, run, probe_type="tracker_state"))
    resolution = {
        **prepared,
        "phase": "RESOLUTION",
        "timestamp": "2026-09-03T12:02:00.000000Z",
        "observed_post_state_sha256": None,
        "readback_evidence_path": None,
        "readback_evidence_sha256": None,
        "status": terminal_status,
        "error": {
            "code": f"RECOVERY_{terminal_status}",
            "template_id": "recovery_probe_result",
            "field_path": "/recovery_probe",
            "parameters": [],
        },
    }
    journal.append(resolution)
    before = journal.path.read_bytes()

    observed = reconcile.status(run)
    first = reconcile.recover(run)
    second = reconcile.recover(run)

    expected = terminal_status.lower()
    assert observed.disposition == "manual_decision_required"
    assert observed.journal_status == expected
    assert first.disposition == second.disposition == "manual_decision_required"
    assert first.journal_status == second.journal_status == expected
    assert journal.path.read_bytes() == before


def test_torn_resolution_rejects_missing_or_false_readback_evidence(
    tmp_path: Path,
) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    journal = state.OperationJournal(run)
    prepared = journal.append(_torn_prepared(state, run))
    bogus = {
        **prepared,
        "phase": "RESOLUTION",
        "timestamp": "2026-09-03T12:02:00.000000Z",
        "previous_event_sha256": journal.read().last_sha256,
        "observed_post_state_sha256": prepared["recovery_probe"][
            "intended_after_sha256"
        ],
        "readback_evidence_path": str((run / "missing-evidence.json").resolve()),
        "readback_evidence_sha256": "f" * 64,
        "status": "APPLIED",
        "error": None,
    }
    with journal.path.open("ab") as stream:
        stream.write(state.canonical_payload_bytes(bogus))

    plan = reconcile.recover(run)

    assert plan.disposition == "manual_decision_required"
    assert plan.journal_status == "conflict"
    records = journal.read().records
    assert records[-1]["phase"] == "CORRUPT_TAIL"
    assert records[-1]["status"] == "CONFLICT"


def test_torn_checkpoint_acceptance_validates_generation_predecessor(
    tmp_path: Path,
) -> None:
    state, _root, run = _run_fixture(tmp_path)
    reconcile = importlib.import_module("reconcile_run")
    journal = state.OperationJournal(run)
    first = state.CheckpointStore.open_existing(run, journal).current()
    path = run / "checkpoints" / "000003.json"
    value = {**first.value, "generation": 3, "previous_checkpoint_sha256": "f" * 64}
    path.write_bytes(state.canonical_bytes(value))
    path.chmod(0o600)
    digest = state.sha256_bytes(path.read_bytes())
    event = {
        **journal.read().records[-1],
        "operation_id": "e" * 64,
        "previous_event_sha256": journal.read().last_sha256,
        "generation": 3,
        "checkpoint_path": str(path.resolve()),
        "checkpoint_sha256": digest,
        "immutable_input_path": str(path.resolve()),
        "immutable_input_sha256": digest,
        "expected_pre_state_sha256": "f" * 64,
        "recovery_probe": state._filesystem_probe(str(path.resolve()), digest),
    }
    with journal.path.open("ab") as stream:
        stream.write(state.canonical_payload_bytes(event))

    with pytest.raises(
        reconcile.ReconciliationError, match="CHECKPOINT_GENERATION_INVALID"
    ):
        reconcile.recover(run)


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
    assert value["workspace"]["workspace_sha256"] == "c" * 64

    unknown = subprocess.run(
        [sys.executable, str(CLI), "start-run", "--run-dir", str(run)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unknown.returncode == 2
