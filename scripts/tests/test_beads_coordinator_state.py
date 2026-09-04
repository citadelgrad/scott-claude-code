"""Persistence and bootstrap contract tests for the Beads coordinator core."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "beads" / "scripts"
SCRIPT = SCRIPTS / "coordinator_state.py"
sys.path.insert(0, str(SCRIPTS))


def load_state():
    spec = importlib.util.spec_from_file_location("coordinator_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["coordinator_state"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def state():
    return load_state()


@pytest.fixture
def owner_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    return root


def test_canonical_identity_preserves_opaque_unicode_and_semantic_operation_id(
    state,
) -> None:
    composed = "café"
    decomposed = "cafe\u0301"
    assert state.issue_key(composed) != state.issue_key(decomposed)
    assert state.canonical_bytes({"b": 2, "a": "Ę"}) == b'{"a":"\xc4\x98","b":2}\n'

    common = {
        "schema": "beads.operation-input.v1",
        "effect_type": "OWNERSHIP_ACQUIRE",
        "target_identity": "issue:opaque",
        "immutable_input_sha256": "1" * 64,
        "ownership_epoch": 7,
    }
    assert state.semantic_operation_id(common) == state.semantic_operation_id(
        dict(reversed(common.items()))
    )
    with pytest.raises(state.StateError, match="OPERATION_ID_INPUT_INVALID"):
        state.semantic_operation_id({**common, "timestamp": "forbidden"})


def test_guarded_paths_reject_escape_alias_modes_and_hardlinks(
    state, owner_root: Path
) -> None:
    outside = owner_root.parent / "outside"
    outside.mkdir()
    alias = owner_root / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(state.StateError):
        state.guarded_path(owner_root, "alias/file")
    with pytest.raises(state.StateError):
        state.guarded_path(owner_root, "../outside/file")

    weak = owner_root / "weak.json"
    weak.write_text("{}\n", encoding="utf-8")
    weak.chmod(0o644)
    with pytest.raises(state.StateError, match="PATH_MODE_INVALID"):
        state.validate_owner_file(weak, root=owner_root)

    first = owner_root / "first"
    first.write_bytes(b"x")
    first.chmod(0o600)
    second = owner_root / "second"
    os.link(first, second)
    with pytest.raises(state.StateError, match="PATH_LINK_COUNT_INVALID"):
        state.validate_owner_file(first, root=owner_root)


def test_manifest_publication_is_owner_only_and_recovers_one_exact_temp(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    manifest = state.make_run_manifest(
        run_id=run.name,
        request_id="request-identity-0001",
        repository_root=str(owner_root.parent.resolve()),
        workspace=str((owner_root.parent / ".beads").resolve()),
        workspace_identity_sha256="c" * 64,
        root_issue_id="opaque/issue",
        run_root=str(owner_root.resolve()),
        created_at="2026-09-03T12:00:00.000000Z",
        coordinator_version="test",
        run_secret_hex="ab" * 32,
    )
    events: list[str] = []
    published = state.publish_run_manifest(run, manifest, crash_hook=events.append)
    assert published == run / "run.json"
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert json.loads(published.read_text())["run_secret_hex"] == "ab" * 32
    assert not list(run.glob("run.json.tmp-*"))
    assert {
        "after_temp_write",
        "after_temp_fsync",
        "after_manifest_rename",
        "after_directory_fsync",
    } <= set(events)

    published.unlink()
    temp = run / "run.json.tmp-ABCDEFGHJKLMNPQR"
    temp.write_bytes(state.canonical_bytes(manifest))
    temp.chmod(0o600)
    assert state.recover_run_manifest(run) == published
    assert published.exists()

    published.unlink()
    for suffix in ("QRSTUVWXYZ234567", "ABCDEFGH23456789"):
        path = run / f"run.json.tmp-{suffix}"
        path.write_bytes(state.canonical_bytes(manifest))
        path.chmod(0o600)
    with pytest.raises(state.StateError, match="RUN_MANIFEST_TEMP_CONFLICT"):
        state.recover_run_manifest(run)


def _trace_fsync_before_replace(monkeypatch, temp: Path) -> list[str]:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace
    temp_inode = temp.stat().st_ino

    def traced_fsync(fd: int) -> None:
        events.append(
            "temp_fsync" if os.fstat(fd).st_ino == temp_inode else "other_fsync"
        )
        real_fsync(fd)

    def traced_replace(source, target) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", traced_fsync)
    monkeypatch.setattr(os, "replace", traced_replace)
    return events


def test_atomic_write_fsyncs_matching_existing_temp_before_rename(
    state, owner_root: Path, monkeypatch
) -> None:
    raw = b'{"durable":true}\n'
    target = owner_root / "value.json"
    temp = owner_root / ".value.json.tmp"
    temp.write_bytes(raw)
    temp.chmod(0o600)
    events = _trace_fsync_before_replace(monkeypatch, temp)

    state.atomic_write(target, raw, root=owner_root, max_bytes=1024)

    assert events.index("temp_fsync") < events.index("replace") < len(events) - 1
    assert events[-1] == "other_fsync"


def test_manifest_recovery_fsyncs_existing_temp_before_rename(
    state, owner_root: Path, monkeypatch
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    manifest = state.make_run_manifest(
        run_id=run.name,
        request_id="request-identity-0001",
        repository_root=str(owner_root.parent.resolve()),
        workspace=str((owner_root.parent / ".beads").resolve()),
        workspace_identity_sha256="c" * 64,
        root_issue_id="opaque/issue",
        run_root=str(owner_root.resolve()),
        created_at="2026-09-03T12:00:00.000000Z",
        coordinator_version="test",
        run_secret_hex="ab" * 32,
    )
    temp = run / "run.json.tmp-ABCDEFGHJKLMNPQR"
    temp.write_bytes(state.canonical_bytes(manifest))
    temp.chmod(0o600)
    events = _trace_fsync_before_replace(monkeypatch, temp)

    state.recover_run_manifest(run)

    assert events.index("temp_fsync") < events.index("replace") < len(events) - 1
    assert events[-1] == "other_fsync"


def _probe() -> dict[str, object]:
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "filesystem_identity",
        "target_identity": "target",
        "expected_before_sha256": "0" * 64,
        "intended_after_identity": "after",
        "intended_after_sha256": "1" * 64,
        "descriptor": ["filesystem", "target"],
        "timeout_seconds": 1,
        "required_authority": "local_write",
    }


def _prepared(state, run: Path, operation_id: str | None = None) -> dict[str, object]:
    input_path = run / "input.json"
    if not input_path.exists():
        input_path.write_bytes(b"{}\n")
        input_path.chmod(0o600)
    digest = state.sha256_bytes(input_path.read_bytes())
    identity = {
        "schema": "beads.operation-input.v1",
        "effect_type": "FILESYSTEM_TEST",
        "target_identity": "target",
        "immutable_input_sha256": digest,
        "ownership_epoch": None,
    }
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": operation_id or state.semantic_operation_id(identity),
        "run_id": run.name,
        "attempt_id": None,
        "issue_id": None,
        "ownership_epoch": None,
        "effect_type": "FILESYSTEM_TEST",
        "immutable_input_path": str(input_path.resolve()),
        "immutable_input_sha256": digest,
        "expected_pre_state_sha256": "0" * 64,
        "recovery_probe": _probe(),
        "timestamp": "2026-09-03T12:00:00.000000Z",
        "authority_class": "local_write",
        "previous_event_sha256": "0" * 64,
        "phase": "PREPARED",
    }


def test_journal_is_hash_chained_idempotent_and_changed_reuse_conflicts(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    event = _prepared(state, run)
    first = journal.append(event)
    assert journal.append(event) == first
    changed = {**event, "effect_type": "CHANGED"}
    with pytest.raises(state.StateError, match="OPERATION_ID_REUSE_CONFLICT"):
        journal.append(changed)
    records = journal.read().records
    assert len(records) == 1
    assert records[0]["previous_event_sha256"] == "0" * 64


def test_journal_allows_one_prepared_to_resolution_transition_and_idempotent_replay(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    prepared = journal.append(_prepared(state, run))
    evidence = run / "input.json"
    evidence_sha = state.sha256_bytes(evidence.read_bytes())
    resolution = {
        **prepared,
        "phase": "RESOLUTION",
        "observed_post_state_sha256": evidence_sha,
        "readback_evidence_path": str(evidence.resolve()),
        "readback_evidence_sha256": evidence_sha,
        "status": "APPLIED",
        "error": None,
    }
    accepted = journal.append(resolution)
    assert journal.append(resolution) == accepted
    assert [event["phase"] for event in journal.read().records] == [
        "PREPARED",
        "RESOLUTION",
    ]
    with pytest.raises(state.StateError, match="OPERATION_ID_REUSE_CONFLICT"):
        journal.append({**resolution, "status": "CONFLICT"})


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("operation_id", "f" * 64),
        ("run_id", "run-bbbbbbbbbbbbbbbb-20260903T120000.000000Z-BCDEFGH2"),
        ("attempt_id", "attempt-001"),
        ("issue_id", "different-issue"),
        ("ownership_epoch", 3),
        ("effect_type", "DIFFERENT_EFFECT"),
        ("immutable_input_path", "/different/input.json"),
        ("immutable_input_sha256", "3" * 64),
        ("expected_pre_state_sha256", "4" * 64),
        ("recovery_probe", {**_probe(), "target_identity": "different"}),
        ("authority_class", "different_authority"),
    ],
)
def test_resolution_is_bound_to_prepared_immutable_projection(
    state, owner_root: Path, field: str, replacement: object
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    prepared = journal.append(_prepared(state, run))
    evidence = run / "input.json"
    evidence_sha = state.sha256_bytes(evidence.read_bytes())
    resolution = {
        **prepared,
        "phase": "RESOLUTION",
        "timestamp": "2026-09-03T12:01:00.000000Z",
        "observed_post_state_sha256": evidence_sha,
        "readback_evidence_path": str(evidence.resolve()),
        "readback_evidence_sha256": evidence_sha,
        "status": "APPLIED",
        "error": None,
        field: replacement,
    }
    with pytest.raises(state.StateError, match="OPERATION_RESOLUTION_MISMATCH"):
        journal.append(resolution)


def test_valid_resolution_may_change_only_resolution_fields(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    prepared = journal.append(_prepared(state, run))
    evidence = run / "input.json"
    evidence_sha = state.sha256_bytes(evidence.read_bytes())
    resolution = {
        **prepared,
        "phase": "RESOLUTION",
        "timestamp": "2026-09-03T12:01:00.000000Z",
        "observed_post_state_sha256": evidence_sha,
        "readback_evidence_path": str(evidence.resolve()),
        "readback_evidence_sha256": evidence_sha,
        "status": "APPLIED",
        "error": None,
    }
    accepted = journal.append(resolution)
    assert journal.append(resolution) == accepted


def _checkpoint(state, run: Path, generation: int, previous: str) -> dict[str, object]:
    return {
        "schema_version": "beads.run-checkpoint.v1",
        "run_id": run.name,
        "generation": generation,
        "root_issue_id": "root",
        "workspace": str((run.parent.parent / ".beads").resolve()),
        "workspace_identity_sha256": "c" * 64,
        "repository_root": str(run.parent.parent.resolve()),
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
        "previous_checkpoint_sha256": previous,
        "created_at": "2026-09-03T12:00:00.000000Z",
    }


def test_checkpoint_authority_comes_from_acceptance_and_rebuilds_stale_pointer(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    store = state.CheckpointStore(run, journal)
    first = store.accept(_checkpoint(state, run, 1, "0" * 64))
    stray = run / "checkpoints" / "000002.json"
    stray.write_bytes(
        state.canonical_bytes(_checkpoint(state, run, 2, first.generation_sha256))
    )
    stray.chmod(0o600)
    (run / "checkpoints" / "latest.json").unlink()
    rebuilt = store.current(rebuild_pointer=True)
    assert rebuilt.generation == 1
    assert (
        json.loads((run / "checkpoints" / "latest.json").read_text())["generation"] == 1
    )
    assert stray.exists()

    accepted = run / "checkpoints" / "000001.json"
    accepted.write_bytes(b"tampered\n")
    with pytest.raises(state.StateError, match="ACCEPTED_CHECKPOINT_HASH_MISMATCH"):
        store.current(rebuild_pointer=True)


def test_exact_checkpoint_reacceptance_is_noop_but_changed_generation_conflicts(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    store = state.CheckpointStore(run, journal)
    value = _checkpoint(state, run, 1, "0" * 64)
    accepted = store.accept(value)
    journal_before = journal.path.read_bytes()
    pointer_before = (run / "checkpoints" / "latest.json").read_bytes()
    events: list[str] = []
    repeated = store.accept(value, hook=events.append)
    assert repeated == accepted
    assert journal.path.read_bytes() == journal_before
    assert (run / "checkpoints" / "latest.json").read_bytes() == pointer_before
    assert events == []
    with pytest.raises(state.StateError, match="CHECKPOINT_GENERATION_COLLISION"):
        store.accept({**value, "phase": "changed"})


def test_checkpoint_retry_after_acceptance_rebuilds_pointer_without_duplicate_event(
    state, owner_root: Path
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    value = _checkpoint(state, run, 1, "0" * 64)

    def crash(event: str) -> None:
        if event == "after_checkpoint_acceptance":
            raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        state.CheckpointStore(run, journal).accept(value, hook=crash)
    assert not (run / "checkpoints" / "latest.json").exists()
    journal_before = journal.path.read_bytes()

    recovered = state.CheckpointStore(run, journal).accept(value)

    assert recovered.generation == 1
    assert journal.path.read_bytes() == journal_before
    assert len(state.OperationJournal(run).read().records) == 1
    assert (run / "checkpoints" / "latest.json").exists()


@pytest.mark.parametrize(
    ("crash_event", "occurrence"),
    [
        ("after_temp_write", 1),
        ("after_temp_fsync", 1),
        ("after_atomic_rename", 1),
        ("after_directory_fsync", 1),
        ("after_journal_write", 1),
        ("after_journal_fsync", 1),
        ("after_checkpoint_acceptance", 1),
        ("after_temp_write", 2),
        ("after_temp_fsync", 2),
        ("after_atomic_rename", 2),
        ("after_directory_fsync", 2),
        ("after_pointer_publication", 1),
    ],
)
def test_checkpoint_retry_converges_at_every_publication_seam(
    state, owner_root: Path, crash_event: str, occurrence: int
) -> None:
    run = owner_root / "run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-ABCDEFGH"
    run.mkdir(mode=0o700)
    journal = state.OperationJournal.create(run)
    value = _checkpoint(state, run, 1, "0" * 64)
    seen = 0

    def crash(event: str) -> None:
        nonlocal seen
        if event == crash_event:
            seen += 1
            if seen == occurrence:
                raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        state.CheckpointStore(run, journal).accept(value, hook=crash)

    recovered = state.CheckpointStore(run, journal).accept(value)
    records = state.OperationJournal(run).read().records
    assert recovered.generation == 1
    assert [record["phase"] for record in records] == ["CHECKPOINT_ACCEPTED"]
    assert sorted(path.name for path in (run / "checkpoints").glob("[0-9]*.json")) == [
        "000001.json"
    ]
    assert state.CheckpointStore.open_existing(run, journal).current() == recovered


def test_bootstrap_is_request_idempotent_and_publishes_pointer_before_active(
    state, owner_root: Path
) -> None:
    pointer: dict[str, object] = {}
    publications: list[dict[str, object]] = []

    def observe(expected: dict[str, object]):
        if not pointer:
            return state.PointerObservation("prestate_unchanged", "0" * 64)
        return state.PointerObservation(
            "intended_effect_present",
            state.sha256_bytes(state.canonical_payload_bytes(pointer)),
            observed_value=dict(pointer),
        )

    def publish(value: dict[str, object]):
        pointer.update(value)
        publications.append(dict(value))
        return observe(value)

    request = state.StartRunInput(
        request_id="request-bootstrap-0001",
        repository_root=str(owner_root.parent.resolve()),
        git_common_dir=str((owner_root.parent / ".git").resolve()),
        workspace=str((owner_root.parent / ".beads").resolve()),
        run_root=str(owner_root.resolve()),
        root_issue_id="root",
        scope_issue_ids=("issue-b", "issue-a", "issue-a"),
        actor="actor",
        base_git_commit="a" * 40,
        authority_snapshot_sha256="b" * 64,
        workspace_identity_sha256="c" * 64,
    )
    callbacks = state.PointerCallbacks(observe=observe, publish=publish)
    first = state.bootstrap_run(
        request,
        callbacks,
        now="2026-09-03T12:00:00.000000Z",
        run_id_factory=lambda: "run-4813494d137e1631-20260903T120000.000000Z-ABCDEFGH",
        secret_factory=lambda: bytes.fromhex("ab" * 32),
    )
    assert first.disposition == "active"
    assert first.checkpoint_generation == 2
    mapping = json.loads(
        (
            owner_root / "_requests" / f"{state.request_key(request.request_id)}.json"
        ).read_text()
    )
    assert mapping["status"] == "active"
    assert pointer["status"] == "active"
    run = owner_root / first.run_id
    records = state.OperationJournal(run).read().records
    owner_prepared = next(
        record
        for record in records
        if record["phase"] == "PREPARED"
        and record["effect_type"] == "OWNERSHIP_ACQUIRE"
    )
    owner_current = owner_root / "_ownership" / state.issue_key("root") / "current.json"
    assert owner_prepared["recovery_probe"]["intended_after_sha256"] == (
        state.sha256_bytes(owner_current.read_bytes())
    )

    repeated = state.bootstrap_run(
        request,
        callbacks,
        now="2026-09-03T12:00:00.000000Z",
        run_id_factory=lambda: pytest.fail("must not allocate another run"),
        secret_factory=lambda: pytest.fail("must not allocate another secret"),
    )
    assert repeated.run_id == first.run_id
    assert repeated.ownership_epoch == 1
    assert len(publications) == 1

    with pytest.raises(state.StateError, match="REQUEST_MAPPING_CONFLICT"):
        state.bootstrap_run(
            state.StartRunInput(**{**request.__dict__, "base_git_commit": "c" * 40}),
            callbacks,
            now="2026-09-03T12:00:00.000000Z",
        )


def test_bootstrap_persists_canonical_workspace_identity(
    state, owner_root: Path
) -> None:
    def observe(_expected: dict[str, object]):
        return state.PointerObservation("prestate_unchanged", "0" * 64)

    def publish(value: dict[str, object]):
        return state.PointerObservation(
            "intended_effect_present",
            state.sha256_bytes(state.canonical_payload_bytes(value)),
            observed_value=dict(value),
        )

    request = state.StartRunInput(
        request_id="request-workspace-0001",
        repository_root=str(owner_root.parent.resolve()),
        git_common_dir=str((owner_root.parent / ".git").resolve()),
        workspace=str((owner_root.parent / ".beads").resolve()),
        run_root=str(owner_root.resolve()),
        root_issue_id="root",
        scope_issue_ids=(),
        actor="actor",
        base_git_commit="a" * 40,
        authority_snapshot_sha256="b" * 64,
        workspace_identity_sha256="c" * 64,
    )
    result = state.bootstrap_run(
        request,
        state.PointerCallbacks(observe=observe, publish=publish),
        now="2026-09-03T12:00:00.000000Z",
        run_id_factory=lambda: "run-4813494d137e1631-20260903T120000.000000Z-ABCDEFGH",
        secret_factory=lambda: bytes.fromhex("ab" * 32),
    )

    run = owner_root / result.run_id
    manifest = state.load_run_manifest(run)

    assert manifest["workspace_identity_sha256"] == "c" * 64
    assert manifest["workspace_identity_sha256"] != state.sha256_bytes(
        manifest["workspace"].encode()
    )
    checkpoint = json.loads((run / "checkpoints" / "000001.json").read_text())
    assert checkpoint["workspace_identity_sha256"] == "c" * 64


@pytest.mark.parametrize(
    "malicious",
    [
        "wrong_digest",
        "missing_value",
        "wrong_fields",
        "stale_epoch",
    ],
)
def test_bootstrap_rejects_untruthful_pointer_callback(
    state, owner_root: Path, malicious: str
) -> None:
    request = state.StartRunInput(
        request_id=f"request-malicious-{malicious}-0001",
        repository_root=str(owner_root.parent.resolve()),
        git_common_dir=str((owner_root.parent / ".git").resolve()),
        workspace=str((owner_root.parent / ".beads").resolve()),
        run_root=str(owner_root.resolve()),
        root_issue_id="root",
        scope_issue_ids=(),
        actor="actor",
        base_git_commit="a" * 40,
        authority_snapshot_sha256="b" * 64,
        workspace_identity_sha256="c" * 64,
    )

    def observe(_expected: dict[str, object]):
        return state.PointerObservation("prestate_unchanged", "0" * 64)

    def publish(value: dict[str, object]):
        observed = dict(value)
        digest = state.sha256_bytes(state.canonical_payload_bytes(observed))
        if malicious == "wrong_digest":
            digest = "f" * 64
        elif malicious == "missing_value":
            observed = None
        elif malicious == "wrong_fields":
            observed["run_id"] = "run-bbbbbbbbbbbbbbbb-20260903T120000.000000Z-BCDEFGH2"
        elif malicious == "stale_epoch":
            observed["ownership_epoch"] = 0
        return state.PointerObservation(
            "intended_effect_present", digest, observed_value=observed
        )

    with pytest.raises(state.StateError, match="POINTER_OBSERVATION_INVALID"):
        state.bootstrap_run(
            request,
            state.PointerCallbacks(observe=observe, publish=publish),
            now="2026-09-03T12:00:00.000000Z",
            run_id_factory=lambda: "run-4813494d137e1631-20260903T120000.000000Z-ABCDEFGH",
            secret_factory=lambda: bytes.fromhex("ab" * 32),
        )
