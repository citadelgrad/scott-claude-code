from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from ._common import load

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parents[2]
_SCRIPTS = _ROOT / "skills/beads/scripts"

sys.path.insert(0, str(_SCRIPTS))

import safe_bd  # noqa: E402  (shared cached module; module-under-test imports the same)

FIXED_NOW = datetime(2026, 9, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)


def _mod():
    return load("capture_beads_snapshot")


def _result(
    profile: str,
    data: Any,
    *,
    status: str = "ok",
    workspace: str = "ws-sha-a" * 8,
    cli: str = safe_bd.PINNED_BD_VERSION,
    error_code: str | None = None,
) -> safe_bd.SafeBdResult:
    return safe_bd.SafeBdResult(
        "beads.safe-bd-result.v1",
        profile,
        status,
        cli,
        workspace,
        data if status == "ok" else None,
        (),
        error_code,
    )


def _ready_record(
    issue_id: str, *, deps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "title": f"Title for {issue_id}",
        "status": "open",
        "issue_type": "task",
        "priority": 2,
        "dependencies": deps or [],
    }


def _canned_calls(
    *,
    ready: list[dict[str, Any]] | None = None,
    blocked: list[dict[str, Any]] | None = None,
    cycles: dict[str, Any] | None = None,
    gates: list[dict[str, Any]] | None = None,
    human: list[dict[str, Any]] | None = None,
    workspace: str = "ws-sha-a" * 8,
) -> dict[str, safe_bd.SafeBdResult]:
    return {
        "ready_list": _result(
            "ready_list", ready if ready is not None else [], workspace=workspace
        ),
        "blocked_list": _result(
            "blocked_list", blocked if blocked is not None else [], workspace=workspace
        ),
        "dependency_cycles": _result(
            "dependency_cycles",
            cycles if cycles is not None else {"cycles": [], "count": 0},
            workspace=workspace,
        ),
        "gate_list": _result(
            "gate_list", gates if gates is not None else [], workspace=workspace
        ),
        "human_list": _result(
            "human_list", human if human is not None else [], workspace=workspace
        ),
    }


class _Recorder:
    """Fake ``safe_bd.run_profile`` dispatching on ``request.profile``."""

    def __init__(self, canned: dict[str, safe_bd.SafeBdResult]) -> None:
        self.canned = canned
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request, *, sensitive=None):
        self.calls.append((request.profile, dict(request.arguments)))
        return self.canned[request.profile]


def _install(
    monkeypatch: pytest.MonkeyPatch, canned: dict[str, safe_bd.SafeBdResult]
) -> _Recorder:
    recorder = _Recorder(canned)
    monkeypatch.setattr(safe_bd, "run_profile", recorder)
    return recorder


def test_happy_path_issues_five_read_only_calls_and_builds_evaluate_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    canned = _canned_calls(
        ready=[
            _ready_record(
                "scc-2",
                deps=[
                    {"issue_id": "scc-2", "depends_on_id": "scc-1", "type": "blocks"}
                ],
            ),
        ],
        blocked=[{"id": "scc-3", "blocked_by": ["scc-2"], "blocked_by_count": 1}],
        cycles={"cycles": [], "count": 0},
        gates=[{"id": "gate-1", "status": "open", "type": "human"}],
        human=[{"id": "human-1"}],
    )
    recorder = _install(monkeypatch, canned)

    snapshot = m.capture_snapshot(
        repository_root=tmp_path,
        root_issue_id="scc-root",
        clock=lambda: FIXED_NOW,
    )

    called_profiles = [profile for profile, _ in recorder.calls]
    assert called_profiles == [
        "ready_list",
        "blocked_list",
        "dependency_cycles",
        "gate_list",
        "human_list",
    ]

    assert snapshot["schema_version"] == m.SCHEMA_VERSION
    assert snapshot["captured_at"] == "2026-09-03T12:00:00.123456Z"
    assert snapshot["root_issue_id"] == "scc-root"
    assert snapshot["workspace_sha256"] == "ws-sha-a" * 8
    assert snapshot["cli_version"] == safe_bd.PINNED_BD_VERSION

    assert snapshot["ready"] == [
        {
            "id": "scc-2",
            "title": "Title for scc-2",
            "status": "open",
            "issue_type": "task",
            "priority": 2,
            "dependency_ids": ["scc-1"],
            "owned_paths": [],
        }
    ]
    assert snapshot["cycles"] == {"cycles": [], "count": 0}
    assert snapshot["blocked"] == [
        {"id": "scc-3", "blocked_by": ["scc-2"], "blocked_by_count": 1}
    ]
    assert snapshot["gates"] == [{"id": "gate-1", "status": "open", "type": "human"}]
    assert snapshot["human"] == [{"id": "human-1"}]
    assert isinstance(snapshot["content_sha256"], str)
    assert len(snapshot["content_sha256"]) == 64
    int(snapshot["content_sha256"], 16)  # valid hex


def test_ready_list_argument_construction_scoped_by_root_and_sort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    recorder = _install(monkeypatch, _canned_calls())

    m.capture_snapshot(
        repository_root=tmp_path, root_issue_id="scc-root", clock=lambda: FIXED_NOW
    )

    ready_args = dict(recorder.calls[0][1])
    assert ready_args == {"parent": "scc-root", "sort": "priority", "limit": 100}


def test_ready_list_argument_construction_unscoped_when_no_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    recorder = _install(monkeypatch, _canned_calls())

    m.capture_snapshot(
        repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
    )

    ready_args = dict(recorder.calls[0][1])
    assert ready_args == {"sort": "priority", "limit": 100}


def test_blocked_list_argument_construction_scoped_by_root_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    recorder = _install(monkeypatch, _canned_calls())

    m.capture_snapshot(
        repository_root=tmp_path, root_issue_id="scc-root", clock=lambda: FIXED_NOW
    )

    assert recorder.calls[1] == ("blocked_list", {"parent": "scc-root"})


def test_zero_argument_profiles_receive_no_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    recorder = _install(monkeypatch, _canned_calls())

    m.capture_snapshot(
        repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
    )

    profiles_with_args = {profile: args for profile, args in recorder.calls[2:]}
    assert profiles_with_args == {
        "dependency_cycles": {},
        "gate_list": {},
        "human_list": {},
    }


@pytest.mark.parametrize(
    "failing_profile",
    ["ready_list", "blocked_list", "dependency_cycles", "gate_list", "human_list"],
)
def test_raises_typed_error_when_any_call_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failing_profile: str
) -> None:
    m = _mod()
    canned = _canned_calls()
    canned[failing_profile] = _result(
        failing_profile, None, status="native_error", error_code="BD_NATIVE_ERROR"
    )
    _install(monkeypatch, canned)

    with pytest.raises(m.SnapshotCaptureError, match="SNAPSHOT_CALL_FAILED"):
        m.capture_snapshot(
            repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
        )


def test_raises_on_workspace_hash_mismatch_across_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    canned = _canned_calls()
    canned["gate_list"] = _result("gate_list", [], workspace="different-hash" * 4)
    _install(monkeypatch, canned)

    with pytest.raises(m.SnapshotCaptureError, match="SNAPSHOT_WORKSPACE_MISMATCH"):
        m.capture_snapshot(
            repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
        )


def test_raises_on_cli_version_mismatch_across_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    canned = _canned_calls()
    canned["human_list"] = _result("human_list", [], cli="9.9.9")
    _install(monkeypatch, canned)

    with pytest.raises(m.SnapshotCaptureError, match="SNAPSHOT_CLI_VERSION_MISMATCH"):
        m.capture_snapshot(
            repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
        )


def test_dependency_ids_derivation_handles_both_entry_shapes_and_dedupes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    ready = [
        _ready_record(
            "scc-4",
            deps=[
                {"issue_id": "scc-4", "depends_on_id": "scc-1", "type": "blocks"},
                {"id": "scc-1", "title": "nested full record", "status": "open"},
                {"issue_id": "scc-4", "depends_on_id": "scc-2", "type": "blocks"},
            ],
        )
    ]
    _install(monkeypatch, _canned_calls(ready=ready))

    snapshot = m.capture_snapshot(
        repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
    )

    assert snapshot["ready"][0]["dependency_ids"] == ["scc-1", "scc-2"]


def test_raises_on_malformed_ready_data_not_a_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    canned = _canned_calls()
    canned["ready_list"] = _result("ready_list", {"not": "a list"})
    _install(monkeypatch, canned)

    with pytest.raises(m.SnapshotCaptureError, match="SNAPSHOT_MALFORMED"):
        m.capture_snapshot(
            repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
        )


def test_raises_on_malformed_cycles_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    canned = _canned_calls()
    canned["dependency_cycles"] = _result("dependency_cycles", [["scc-1", "scc-2"]])
    _install(monkeypatch, canned)

    with pytest.raises(m.SnapshotCaptureError, match="SNAPSHOT_MALFORMED"):
        m.capture_snapshot(
            repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
        )


def test_raises_when_serialized_snapshot_exceeds_max_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    _install(monkeypatch, _canned_calls(ready=[_ready_record("scc-5")]))

    with pytest.raises(m.SnapshotCaptureError, match="SNAPSHOT_TOO_LARGE"):
        m.capture_snapshot(
            repository_root=tmp_path,
            root_issue_id=None,
            clock=lambda: FIXED_NOW,
            max_snapshot_bytes=16,
        )


def test_never_calls_any_mutating_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    recorder = _install(monkeypatch, _canned_calls())

    m.capture_snapshot(
        repository_root=tmp_path, root_issue_id=None, clock=lambda: FIXED_NOW
    )

    called_profiles = {profile for profile, _ in recorder.calls}
    assert called_profiles == {
        "ready_list",
        "blocked_list",
        "dependency_cycles",
        "gate_list",
        "human_list",
    }
    assert all(not safe_bd.PROFILES[p].mutation for p in called_profiles)


def test_is_deterministic_same_inputs_produce_byte_identical_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    m = _mod()
    ready = [
        _ready_record(
            "scc-6",
            deps=[{"issue_id": "scc-6", "depends_on_id": "scc-1", "type": "blocks"}],
        )
    ]
    _install(monkeypatch, _canned_calls(ready=ready))
    first = m.capture_snapshot(
        repository_root=tmp_path, root_issue_id="r", clock=lambda: FIXED_NOW
    )

    _install(monkeypatch, _canned_calls(ready=ready))
    second = m.capture_snapshot(
        repository_root=tmp_path, root_issue_id="r", clock=lambda: FIXED_NOW
    )

    assert first == second


def test_persist_snapshot_writes_atomically_under_owner_directory(
    tmp_path: Path,
) -> None:
    m = _mod()
    run_dir = tmp_path / "run-dir"
    snapshot = {
        "schema_version": m.SCHEMA_VERSION,
        "ready": [],
        "content_sha256": "a" * 64,
    }

    written = m.persist_snapshot(snapshot, run_dir=run_dir)

    assert written == run_dir / "snapshot.json"
    assert written.read_text(encoding="utf-8") == m._canonical_json(snapshot)
    import stat

    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_persist_snapshot_second_call_with_same_content_does_not_raise(
    tmp_path: Path,
) -> None:
    m = _mod()
    run_dir = tmp_path / "run-dir"
    snapshot = {
        "schema_version": m.SCHEMA_VERSION,
        "ready": [],
        "content_sha256": "b" * 64,
    }

    m.persist_snapshot(snapshot, run_dir=run_dir)
    written_again = m.persist_snapshot(snapshot, run_dir=run_dir)

    assert written_again.read_text(encoding="utf-8") == m._canonical_json(snapshot)
