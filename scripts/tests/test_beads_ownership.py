"""Cooperative ownership and fencing tests."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import multiprocessing
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "beads" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _acquire_process(
    root: str, run: str, actor: str, tracker_hash: str, start, queue
) -> None:
    ownership = importlib.import_module("beads_ownership")
    start.wait()
    try:
        result = ownership.OwnershipStore(Path(root)).acquire(
            issue_id="opaque/issue",
            actor=actor,
            run_directory=Path(run),
            tracker_state_sha256=tracker_hash,
            operation_id=("1" if run.endswith("ABCDEFGH") else "2") * 64,
            now=dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc),
        )
        queue.put(
            (result.disposition, result.record["run_id"] if result.record else None)
        )
    except Exception as exc:  # pragma: no cover - diagnostic transfer
        queue.put(("error", type(exc).__name__))


def _make_run(state, root: Path, suffix: str, secret: str) -> Path:
    run = root / f"run-aaaaaaaaaaaaaaaa-20260903T120000.000000Z-{suffix}"
    run.mkdir(mode=0o700)
    manifest = state.make_run_manifest(
        run_id=run.name,
        request_id=f"request-{suffix}-000000",
        repository_root=str(root.parent.resolve()),
        workspace=str((root.parent / ".beads").resolve()),
        workspace_identity_sha256="c" * 64,
        root_issue_id="root",
        run_root=str(root.resolve()),
        created_at="2026-09-03T12:00:00.000000Z",
        coordinator_version="test",
        run_secret_hex=secret,
    )
    state.publish_run_manifest(run, manifest)
    return run


@pytest.fixture
def setup(tmp_path: Path):
    state = importlib.import_module("coordinator_state")
    ownership = importlib.import_module("beads_ownership")
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    return state, ownership, root


def test_two_processes_get_one_held_owner_and_one_typed_conflict(setup) -> None:
    state, ownership, root = setup
    first = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    second = _make_run(state, root, "BCDEFGH2", "cd" * 32)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_acquire_process,
            args=(str(root), str(run), "same actor", "0" * 64, start, queue),
        )
        for run in (first, second)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=5) for _ in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert sorted(item[0] for item in results) == ["conflict", "held"]

    audit = ownership.OwnershipStore(root).inspect("opaque/issue")
    assert audit.disposition == "held"
    assert len(audit.history) == 1
    assert audit.history[0]["status"] == "active"


def test_epochs_are_monotonic_and_stale_or_expired_capabilities_fail_closed(
    setup,
) -> None:
    state, ownership, root = setup
    first = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    second = _make_run(state, root, "BCDEFGH2", "cd" * 32)
    store = ownership.OwnershipStore(root)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
    held = store.acquire(
        issue_id="issue",
        actor="actor",
        run_directory=first,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )
    assert held.disposition == "held" and held.record["epoch"] == 1
    renewed = store.renew(
        "issue",
        first,
        epoch=1,
        operation_id="2" * 64,
        now=now + dt.timedelta(minutes=5),
    )
    assert renewed.disposition == "held" and renewed.record["epoch"] == 1

    with pytest.raises(ownership.OwnershipError, match="OWNERSHIP_EPOCH_STALE"):
        store.release("issue", first, epoch=0, operation_id="3" * 64, now=now)

    released = store.release(
        "issue",
        first,
        epoch=1,
        operation_id="6" * 64,
        now=now + dt.timedelta(minutes=11),
    )
    assert released.disposition == "released"
    again = store.acquire(
        issue_id="issue",
        actor="actor",
        run_directory=second,
        tracker_state_sha256="0" * 64,
        operation_id="7" * 64,
        now=now + dt.timedelta(minutes=12),
    )
    assert again.record["epoch"] == 2

    expiring = store.acquire(
        issue_id="expiring",
        actor="actor",
        run_directory=first,
        tracker_state_sha256="0" * 64,
        operation_id="8" * 64,
        now=now,
    )
    assert expiring.disposition == "held"
    expired = store.verify(
        "expiring", first, epoch=1, now=now + dt.timedelta(minutes=16)
    )
    assert expired.disposition == "unknown"
    with pytest.raises(ownership.OwnershipError, match="OWNERSHIP_NOT_ACTIVE"):
        store.acquire(
            issue_id="expiring",
            actor="actor",
            run_directory=second,
            tracker_state_sha256="0" * 64,
            operation_id="9" * 64,
            now=now + dt.timedelta(minutes=17),
        )


def test_missing_history_mismatch_overflow_and_secret_persistence_fail_closed(
    setup,
) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    store = ownership.OwnershipStore(root)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
    store.acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )
    owner_dir = root / "_ownership" / state.issue_key("issue")
    events = owner_dir / "events.jsonl"
    original = events.read_bytes()
    events.write_bytes(b"")
    with pytest.raises(ownership.OwnershipError, match="OWNERSHIP_HISTORY_MISSING"):
        store.inspect("issue")
    events.write_bytes(original)

    current = owner_dir / "current.json"
    raw = current.read_text().replace('"epoch":1', f'"epoch":{state.MAX_U64}')
    current.write_text(raw)
    current.chmod(0o600)
    with pytest.raises(
        ownership.OwnershipError, match="OWNERSHIP_CURRENT_HISTORY_MISMATCH"
    ):
        store.acquire(
            issue_id="issue",
            actor="actor",
            run_directory=run,
            tracker_state_sha256="0" * 64,
            operation_id="2" * 64,
            now=now,
        )

    persisted = b"".join(
        path.read_bytes() for path in root.rglob("*") if path.is_file()
    )
    # The secret may exist only in run.json; never in shared ownership state.
    ownership_bytes = b"".join(
        path.read_bytes() for path in (root / "_ownership").rglob("*") if path.is_file()
    )
    assert bytes.fromhex("ab" * 32) not in persisted
    assert ("ab" * 32).encode() not in ownership_bytes


@pytest.mark.parametrize(
    "crash_event",
    [
        "after_ownership_history_write",
        "after_ownership_history_fsync",
        "after_temp_write",
        "after_temp_fsync",
        "after_atomic_rename",
        "after_directory_fsync",
        "after_ownership_current_publication",
    ],
)
def test_acquire_recovers_every_two_file_publication_crash(
    setup, crash_event: str
) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)

    def crash(event: str) -> None:
        if event == crash_event:
            raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        ownership.OwnershipStore(root, crash_hook=crash).acquire(
            issue_id="issue",
            actor="actor",
            run_directory=run,
            tracker_state_sha256="0" * 64,
            operation_id="1" * 64,
            now=now,
        )

    recovered = ownership.OwnershipStore(root).acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )
    assert recovered.disposition == "held"
    assert recovered.record is not None and recovered.record["epoch"] == 1
    assert len(recovered.history) == 1


def test_release_prepared_and_released_retries_are_idempotent(setup) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
    ownership.OwnershipStore(root).acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )

    def crash(event: str) -> None:
        if event == "after_ownership_current_publication":
            raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        ownership.OwnershipStore(root, crash_hook=crash).release(
            "issue", run, epoch=1, operation_id="2" * 64, now=now
        )
    prepared = ownership.OwnershipStore(root).inspect("issue")
    assert (
        prepared.record is not None and prepared.record["status"] == "release_prepared"
    )

    released = ownership.OwnershipStore(root).release(
        "issue", run, epoch=1, operation_id="2" * 64, now=now
    )
    assert released.disposition == "released"
    history_bytes = (
        root / "_ownership" / state.issue_key("issue") / "events.jsonl"
    ).read_bytes()
    repeated = ownership.OwnershipStore(root).release(
        "issue", run, epoch=1, operation_id="2" * 64, now=now
    )
    assert repeated.disposition == "released"
    assert (
        root / "_ownership" / state.issue_key("issue") / "events.jsonl"
    ).read_bytes() == history_bytes


@pytest.mark.parametrize(
    "crash_event",
    [
        "after_ownership_history_fsync",
        "after_temp_fsync",
        "after_atomic_rename",
        "after_directory_fsync",
        "after_ownership_current_publication",
    ],
)
def test_renew_retry_converges_without_duplicate_history(
    setup, crash_event: str
) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
    ownership.OwnershipStore(root).acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )

    def crash(event: str) -> None:
        if event == crash_event:
            raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        ownership.OwnershipStore(root, crash_hook=crash).renew(
            "issue",
            run,
            epoch=1,
            operation_id="2" * 64,
            now=now + dt.timedelta(minutes=1),
        )
    recovered = ownership.OwnershipStore(root).renew(
        "issue",
        run,
        epoch=1,
        operation_id="2" * 64,
        now=now + dt.timedelta(minutes=1),
    )
    assert recovered.disposition == "held"
    assert len(recovered.history) == 2


def test_expiry_retry_converges_to_stable_unknown(setup) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
    ownership.OwnershipStore(root).acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )

    def crash(event: str) -> None:
        if event == "after_ownership_history_fsync":
            raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        ownership.OwnershipStore(root, crash_hook=crash).verify(
            "issue", run, epoch=1, now=now + dt.timedelta(minutes=16)
        )
    recovered = ownership.OwnershipStore(root).verify(
        "issue", run, epoch=1, now=now + dt.timedelta(minutes=16)
    )
    assert recovered.disposition == "unknown"
    assert len(recovered.history) == 2


def test_release_retry_after_released_history_and_current_publication(setup) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
    ownership.OwnershipStore(root).acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=now,
    )
    publications = 0

    def crash(event: str) -> None:
        nonlocal publications
        if event == "after_ownership_current_publication":
            publications += 1
            if publications == 2:
                raise RuntimeError("crash witness")

    with pytest.raises(RuntimeError, match="crash witness"):
        ownership.OwnershipStore(root, crash_hook=crash).release(
            "issue", run, epoch=1, operation_id="2" * 64, now=now
        )
    recovered = ownership.OwnershipStore(root).release(
        "issue", run, epoch=1, operation_id="2" * 64, now=now
    )
    assert recovered.disposition == "released"
    assert len(recovered.history) == 3


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("actor", "attacker"),
        ("acquisition_operation_id", "9" * 64),
        ("tracker_state_sha256", "8" * 64),
        ("acquired_at", "2026-09-03T11:00:00.000000Z"),
        ("renewed_at", "2026-09-03T11:00:00.000000Z"),
        ("lease_expires_at", "2099-09-03T12:00:00.000000Z"),
    ],
)
def test_history_authenticates_every_authoritative_current_field(
    setup, field: str, replacement: str
) -> None:
    state, ownership, root = setup
    run = _make_run(state, root, "ABCDEFGH", "ab" * 32)
    ownership.OwnershipStore(root).acquire(
        issue_id="issue",
        actor="actor",
        run_directory=run,
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc),
    )
    current_path = root / "_ownership" / state.issue_key("issue") / "current.json"
    current = json.loads(current_path.read_bytes())
    current[field] = replacement
    current_path.write_bytes(state.canonical_bytes(current))
    current_path.chmod(0o600)

    with pytest.raises(
        ownership.OwnershipError, match="OWNERSHIP_CURRENT_HISTORY_MISMATCH"
    ):
        ownership.OwnershipStore(root).inspect("issue")
