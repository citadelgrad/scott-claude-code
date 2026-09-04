from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import load  # noqa: E402


def _mod():
    return load("coordinator_front")


def _ready(
    issue_id: str,
    *,
    priority: int = 2,
    owned_paths: list[str] | None = None,
    dependency_ids: list[str] | None = None,
    issue_type: str = "task",
    status: str = "open",
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "title": issue_id,
        "status": status,
        "issue_type": issue_type,
        "priority": priority,
        "dependency_ids": dependency_ids or [],
        "owned_paths": owned_paths or [],
    }


def _snapshot(
    ready: list[dict[str, Any]],
    *,
    gates: list[dict[str, Any]] | None = None,
    root_issue_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "beads.issue-snapshot.v1",
        "captured_at": "2026-09-04T00:00:00.000000Z",
        "workspace_sha256": "0" * 64,
        "cli_version": "1.2.2",
        "root_issue_id": root_issue_id,
        "ready": ready,
        "blocked": [],
        "cycles": {"cycles": [], "count": 0},
        "gates": gates or [],
        "human": [],
    }


# --- group_gates_by_target -------------------------------------------------


def test_group_gates_by_target_groups_by_target_field() -> None:
    m = _mod()
    gates = [
        {"id": "g1", "target": "A", "status": "open", "type": "human"},
        {"id": "g2", "target": "A", "status": "resolved", "type": "human"},
        {"id": "g3", "target": "B", "status": "open", "type": "async"},
    ]
    grouped = m.group_gates_by_target(gates)
    assert set(grouped) == {"A", "B"}
    assert [g["id"] for g in grouped["A"]] == ["g1", "g2"]
    assert [g["id"] for g in grouped["B"]] == ["g3"]


def test_group_gates_by_target_drops_missing_or_non_string_target() -> None:
    m = _mod()
    gates = [
        {"id": "g1", "status": "open", "type": "human"},  # no target
        {"id": "g2", "target": None, "status": "open", "type": "human"},
        {"id": "g3", "target": 42, "status": "open", "type": "human"},
        {"id": "g4", "target": "", "status": "open", "type": "human"},
    ]
    grouped = m.group_gates_by_target(gates)
    assert grouped == {}


# --- build_candidates --------------------------------------------------


def test_build_candidates_projects_id_and_owned_paths() -> None:
    m = _mod()
    ready = [
        _ready("A", owned_paths=["src/a.py"]),
        _ready("B", owned_paths=[]),
    ]
    candidates = m.build_candidates(ready)
    assert candidates == [
        {"id": "A", "scopes": ["src/a.py"]},
        {"id": "B", "scopes": []},
    ]


def test_build_candidates_rejects_missing_id() -> None:
    m = _mod()
    import pytest

    with pytest.raises(m.CoordinatorFrontError, match="READY_RECORD_MISSING_ID"):
        m.build_candidates([{"title": "no id here"}])


# --- build_front_from_snapshot: AC-T11-001 ready membership ---------------


def test_ready_membership_correct_for_diamond_shared_prerequisite(
    tmp_path: Path,
) -> None:
    """Diamond S0 fixture from dependencies-and-ready-fronts.md: D->B, D->C,
    B->A, C->A, all open; only the shared leaf prerequisite A is ready."""
    m = _mod()
    ready = [_ready("A")]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.refusal is None
    assert front.selected == ("A",)


def test_ready_membership_respects_capacity_bound(tmp_path: Path) -> None:
    """Bounded fixture: same multi-root candidates, cap 2 selects only 2.

    Each candidate declares a disjoint write scope so capacity, not
    write-scope conflict, is the only thing under test here.
    """
    m = _mod()
    ready = [
        _ready("B", priority=0, owned_paths=["src/b.py"]),
        _ready("A", priority=1, owned_paths=["src/a.py"]),
        _ready("C", priority=1, owned_paths=["src/c.py"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=2
    )
    assert front.refusal is None
    assert front.selected == ("B", "A")
    assert {e["id"]: e["reason"] for e in front.excluded}["C"] == "CAPACITY_REACHED"


def test_root_issue_never_selected(tmp_path: Path) -> None:
    m = _mod()
    ready = [_ready("R"), _ready("A")]
    snapshot = _snapshot(ready, root_issue_id="R")
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.refusal is None
    assert front.selected == ("A",)
    assert {e["id"]: e["reason"] for e in front.excluded}["R"] == "ROOT_ISSUE_EXCLUDED"


# --- build_front_from_snapshot: AC-T11-002 whole-front refusals -----------


def test_cycle_refuses_whole_front_even_with_independent_ready_candidate(
    tmp_path: Path,
) -> None:
    """Cycle-refusal fixture: U is independent and ready, but the cycle
    poisons the WHOLE front, not just A/B."""
    m = _mod()
    ready = [_ready("U")]
    snapshot = _snapshot(ready)
    snapshot["cycles"] = {"cycles": [["A", "B", "A"]], "count": 1}
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=2
    )
    assert front.selected == ()
    assert front.refusal == "DEPENDENCY_CYCLE"


def test_unresolved_human_gate_refuses_whole_front(tmp_path: Path) -> None:
    """Human-gate refusal fixture: A requires open human gate H targeting A;
    U is independent and ready but the whole front still refuses."""
    m = _mod()
    ready = [_ready("A"), _ready("U")]
    gates = [{"id": "H", "target": "A", "status": "open", "type": "human"}]
    snapshot = _snapshot(ready, gates=gates)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=2
    )
    assert front.selected == ()
    assert front.refusal == "UNRESOLVED_HUMAN_GATE"


def test_resolved_gate_does_not_block_dispatch(tmp_path: Path) -> None:
    m = _mod()
    ready = [_ready("A")]
    gates = [{"id": "H", "target": "A", "status": "resolved", "type": "human"}]
    snapshot = _snapshot(ready, gates=gates)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=2
    )
    assert front.refusal is None
    assert front.selected == ("A",)


def test_gate_with_unattributable_target_is_dropped_not_applied_to_wrong_issue(
    tmp_path: Path,
) -> None:
    """A gate naming an issue that is not in this ready front must not
    accidentally attach to (and block) an unrelated candidate."""
    m = _mod()
    ready = [_ready("A")]
    gates = [
        {"id": "H", "target": "SOME-OTHER-ISSUE", "status": "open", "type": "human"}
    ]
    snapshot = _snapshot(ready, gates=gates)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=2
    )
    assert front.refusal is None
    assert front.selected == ("A",)


def test_same_batch_unlock_forbidden(tmp_path: Path) -> None:
    """B must be processed (and selected) before A so A's dependency is
    already in ``selected`` (and still open) when A is evaluated -- that
    is what actually triggers SAME_BATCH_UNLOCK_FORBIDDEN. Each candidate
    declares a distinct write scope so write-scope logic cannot mask the
    same-batch check under test here.
    """
    m = _mod()
    ready = [
        _ready("B", owned_paths=["src/b.py"]),
        _ready("A", dependency_ids=["B"], owned_paths=["src/a.py"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.refusal is None
    assert front.selected == ("B",)
    assert {e["id"]: e["reason"] for e in front.excluded}["A"] == (
        "SAME_BATCH_UNLOCK_FORBIDDEN"
    )


def test_stale_snapshot_refuses_whole_front(tmp_path: Path) -> None:
    m = _mod()
    ready = [_ready("A")]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot,
        repository_root=str(tmp_path),
        hard_cap=2,
        stale_reasons=["dependency add observed after capture"],
    )
    assert front.selected == ()
    assert front.refusal == "STALE_READY_FRONT"


# --- build_front_from_snapshot: AC-T11-003 write-scope conflicts ----------


def test_disjoint_write_scopes_both_proceed(tmp_path: Path) -> None:
    m = _mod()
    ready = [
        _ready("A", owned_paths=["src/a.py"]),
        _ready("B", owned_paths=["src/b.py"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.refusal is None
    assert front.selected == ("A", "B")


def test_overlapping_write_scope_blocks_second_candidate(tmp_path: Path) -> None:
    m = _mod()
    ready = [
        _ready("A", owned_paths=["src/shared.py"]),
        _ready("B", owned_paths=["src/shared.py"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.refusal is None
    assert front.selected == ("A",)
    assert {e["id"]: e["reason"] for e in front.excluded}["B"] == (
        "WRITE_SCOPE_CONFLICT"
    )


def test_hardlink_alias_write_scope_serializes(tmp_path: Path) -> None:
    """AC-T11-003's 'aliased' clause: two candidates that declare different
    file names hardlinked to the same inode must serialize, never both
    proceed as if their scopes were disjoint."""
    m = _mod()
    (tmp_path / "real.py").write_text("x = 1\n")
    import os

    os.link(tmp_path / "real.py", tmp_path / "alias.py")
    ready = [
        _ready("A", owned_paths=["real.py"]),
        _ready("B", owned_paths=["alias.py"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.selected == ("A",)
    assert {e["id"]: e["reason"] for e in front.excluded}["B"] == (
        "WRITE_SCOPE_CONFLICT"
    )


def test_unknown_write_scope_blocks_not_optimistic(tmp_path: Path) -> None:
    """A top-level wildcard pattern is SCOPE_PATTERN_AMBIGUOUS => unknown,
    which must exclude the candidate, never be treated as safe.

    Conflict classification is pairwise against already-selected
    candidates, so B (clean scope) is listed first and is selected with
    nothing yet to compare against; A's ambiguous ``*.lock`` scope is
    then classified ``unknown`` against B and excluded -- proving the
    unknown scope blocks rather than being waved through optimistically.
    """
    m = _mod()
    ready = [
        _ready("B", owned_paths=["src/b.py"]),
        _ready("A", owned_paths=["*.lock"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.refusal is None
    assert front.selected == ("B",)
    assert {e["id"]: e["reason"] for e in front.excluded}["A"] == (
        "WRITE_SCOPE_UNKNOWN"
    )


def test_shared_manifest_hotspot_serializes(tmp_path: Path) -> None:
    m = _mod()
    ready = [
        _ready("A", owned_paths=["uv.lock"]),
        _ready("B", owned_paths=["uv.lock"]),
    ]
    snapshot = _snapshot(ready)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=3
    )
    assert front.selected == ("A",)
    assert {e["id"]: e["reason"] for e in front.excluded}["B"] == (
        "WRITE_SCOPE_CONFLICT"
    )


# --- build_front_from_snapshot: AC-T11-004 determinism / no mutation ------


def test_repeated_evaluation_is_byte_identical(tmp_path: Path) -> None:
    m = _mod()
    ready = [
        _ready("B", priority=0, owned_paths=["src/b.py"]),
        _ready("A", priority=1, owned_paths=["src/a.py"]),
        _ready("C", priority=1, owned_paths=["src/c.py"]),
    ]
    snapshot = _snapshot(ready)
    results = [
        m.build_front_from_snapshot(
            snapshot, repository_root=str(tmp_path), hard_cap=2
        ).to_dict()
        for _ in range(5)
    ]
    assert all(r == results[0] for r in results)
    assert results[0]["selected"] == ["B", "A"]


def test_build_front_from_snapshot_does_not_mutate_input_snapshot(
    tmp_path: Path,
) -> None:
    import copy

    m = _mod()
    ready = [_ready("A", owned_paths=["src/a.py"])]
    snapshot = _snapshot(ready)
    before = copy.deepcopy(snapshot)
    m.build_front_from_snapshot(snapshot, repository_root=str(tmp_path), hard_cap=2)
    assert snapshot == before


def test_build_front_from_snapshot_issues_no_bd_call(
    tmp_path: Path, monkeypatch
) -> None:
    """Structural guarantee: this pure-evaluation path must never import or
    invoke safe_bd, capture_beads_snapshot, or subprocess -- it only
    consumes an already-captured snapshot."""
    m = _mod()
    assert not hasattr(m, "safe_bd")
    ready = [_ready("A")]
    snapshot = _snapshot(ready)

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess must never be invoked")

    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.Popen", _boom)
    front = m.build_front_from_snapshot(
        snapshot, repository_root=str(tmp_path), hard_cap=2
    )
    assert front.selected == ("A",)


# --- capture_and_build_front: composition wiring ---------------------------


def test_capture_and_build_front_wires_capture_into_evaluation(
    tmp_path: Path, monkeypatch
) -> None:
    """Without a real bd workspace, stub capture_beads_snapshot.capture_snapshot
    and prove capture_and_build_front feeds its exact output into
    build_front_from_snapshot (same repository_root, snapshot returned
    verbatim, front computed from it)."""
    m = _mod()
    ready = [
        _ready("A", owned_paths=["src/a.py"]),
        _ready("B", owned_paths=["src/b.py"]),
    ]
    fake_snapshot = _snapshot(ready)
    calls: list[dict[str, Any]] = []

    def fake_capture_snapshot(*, repository_root, root_issue_id=None, limit=100):
        calls.append(
            {
                "repository_root": repository_root,
                "root_issue_id": root_issue_id,
                "limit": limit,
            }
        )
        return fake_snapshot

    monkeypatch.setattr(
        m.capture_beads_snapshot, "capture_snapshot", fake_capture_snapshot
    )
    snapshot, front = m.capture_and_build_front(
        repository_root=tmp_path, root_issue_id="R", hard_cap=3
    )
    assert snapshot is fake_snapshot
    assert calls == [{"repository_root": tmp_path, "root_issue_id": "R", "limit": 100}]
    assert front.refusal is None
    assert front.selected == ("A", "B")


def test_capture_and_build_front_propagates_capture_error_without_fabricating_front(
    tmp_path: Path, monkeypatch
) -> None:
    m = _mod()
    import pytest

    def fake_capture_snapshot(**kwargs):
        raise m.capture_beads_snapshot.SnapshotCaptureError("SNAPSHOT_CALL_FAILED")

    monkeypatch.setattr(
        m.capture_beads_snapshot, "capture_snapshot", fake_capture_snapshot
    )
    with pytest.raises(
        m.capture_beads_snapshot.SnapshotCaptureError, match="SNAPSHOT_CALL_FAILED"
    ):
        m.capture_and_build_front(repository_root=tmp_path, hard_cap=3)
