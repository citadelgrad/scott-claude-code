from __future__ import annotations

import ast

from ._common import SCRIPTS, load


def _mod():
    return load("build_ready_front")


def _issue(
    issue_id: str,
    *,
    status: str = "open",
    priority: int = 2,
    issue_type: str = "task",
    dependency_ids: list[str] | None = None,
) -> dict:
    return {
        "id": issue_id,
        "title": issue_id,
        "status": status,
        "priority": priority,
        "issue_type": issue_type,
        "dependency_ids": dependency_ids or [],
    }


def _snapshot(ready: list[dict], *, root_issue_id: str = "R", cycles=None) -> dict:
    return {
        "root_issue_id": root_issue_id,
        "captured_at": "2026-09-03T00:00:00.000000Z",
        "ready": ready,
        "cycles": cycles if cycles is not None else {"cycles": [], "count": 0},
    }


# ---------------------------------------------------------------------------
# Purity / no I/O (AC-T11-004)
# ---------------------------------------------------------------------------


def test_module_never_imports_io_or_subprocess() -> None:
    source = (SCRIPTS / "build_ready_front.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"subprocess", "safe_bd", "safe_output", "os", "socket", "shutil"}
    assert imported.isdisjoint(forbidden), imported


def test_evaluate_never_calls_subprocess_or_os_system(monkeypatch) -> None:
    m = _mod()
    import os
    import subprocess

    def _boom(*_a, **_k):
        raise AssertionError("build_ready_front must never spawn a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)

    snapshot = _snapshot([_issue("A")])
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A",)


def test_evaluate_is_deterministic_across_repeated_calls() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B"), _issue("C")])
    first = m.evaluate(snapshot, hard_cap=5).to_dict()
    second = m.evaluate(snapshot, hard_cap=5).to_dict()
    assert first == second


# ---------------------------------------------------------------------------
# AC-T11-001: ready membership correctness across graph shapes
# ---------------------------------------------------------------------------


def test_linear_chain_selects_all_in_ready_order() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B"), _issue("C")])
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A", "B", "C")
    assert result.refusal is None


def test_diamond_graph_selects_all_ready_candidates() -> None:
    m = _mod()
    # A and B are both ready in parallel; C depends on both but bd would
    # only surface C once truly ready -- here all three are already
    # reported ready by the authoritative bd ready order.
    snapshot = _snapshot(
        [_issue("A"), _issue("B"), _issue("C", dependency_ids=["A", "B"])]
    )
    result = m.evaluate(snapshot, hard_cap=5)
    assert set(result.selected) >= {"A", "B"}


def test_multi_root_independent_lanes_all_selected() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("X")], root_issue_id="R")
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A", "X")


def test_deferred_issue_absent_from_captured_ready_is_not_selected() -> None:
    m = _mod()
    # bd ready is the membership oracle: a deferred issue simply never
    # appears in the captured ready list, so it cannot be selected.
    snapshot = _snapshot([_issue("A")])
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A",)
    assert all(e["id"] != "DEFERRED" for e in result.excluded)


def test_human_gated_issue_refuses_whole_front_with_clean_sibling() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    gates = {"A": [{"id": "H", "title": "h", "status": "open", "type": "human"}]}
    result = m.evaluate(snapshot, hard_cap=5, gates=gates)
    assert result.selected == ()
    assert result.refusal == m.UNRESOLVED_HUMAN_GATE


def test_changed_live_state_reflected_via_status_field() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A", status="in_progress")])
    result = m.evaluate(snapshot, hard_cap=5)
    # live status is carried through untouched -- selection does not
    # infer eligibility contrary to captured bd ready membership.
    assert result.selected == ("A",)


# ---------------------------------------------------------------------------
# AC-T11-002: whole-front refusal set (six fixtures), each with clean `U`
# ---------------------------------------------------------------------------


def test_cycle_refuses_whole_front_not_just_offending_candidate() -> None:
    m = _mod()
    snapshot = _snapshot(
        [_issue("A"), _issue("U")],
        cycles={"cycles": [["A", "B", "A"]], "count": 1},
    )
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ()
    assert result.refusal == m.DEPENDENCY_CYCLE


def test_malformed_cycle_evidence_is_cycle_check_unknown() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")], cycles={"cycles": [["x"]]})
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ()
    assert result.refusal == m.CYCLE_CHECK_UNKNOWN


def test_orientation_mismatch_refuses_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A", dependency_ids=["Z"]), _issue("U")])
    result = m.evaluate(snapshot, hard_cap=5, declared_edges={"A": ["OTHER_UNRELATED"]})
    assert result.selected == ()
    assert result.refusal == m.DEPENDENCY_ORIENTATION_MISMATCH


def test_orientation_unknown_when_declared_issue_missing_from_ready() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    result = m.evaluate(snapshot, hard_cap=5, declared_edges={"NOT_READY": ["A"]})
    assert result.selected == ()
    assert result.refusal == m.DEPENDENCY_ORIENTATION_UNKNOWN


def test_unresolved_async_gate_refuses_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    gates = {"A": [{"id": "G", "title": "ci", "status": "pending", "type": "async"}]}
    result = m.evaluate(snapshot, hard_cap=5, gates=gates)
    assert result.selected == ()
    assert result.refusal == m.UNRESOLVED_ASYNC_GATE


def test_unsupported_gate_type_refuses_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    gates = {
        "A": [{"id": "G", "title": "local bead", "status": "open", "type": "bead"}]
    }
    result = m.evaluate(snapshot, hard_cap=5, gates=gates)
    assert result.selected == ()
    assert result.refusal == m.GATE_UNSUPPORTED


def test_gate_readback_failed_when_gate_record_malformed() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    gates = {"A": [{"id": "G", "status": "", "type": "human"}]}
    result = m.evaluate(snapshot, hard_cap=5, gates=gates)
    assert result.selected == ()
    assert result.refusal == m.GATE_READBACK_FAILED


def test_snapshot_inconsistent_refuses_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    consistency = {"A": {"consistent": False, "detail": "dep list disagrees"}}
    result = m.evaluate(snapshot, hard_cap=5, consistency=consistency)
    assert result.selected == ()
    assert result.refusal == m.SNAPSHOT_INCONSISTENT


def test_stale_snapshot_refuses_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    result = m.evaluate(
        snapshot,
        hard_cap=5,
        now="2026-09-03T02:00:00.000000Z",
        max_snapshot_age_seconds=60,
    )
    assert result.selected == ()
    assert result.refusal == m.STALE_READY_FRONT


def test_explicit_stale_reasons_refuse_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("U")])
    result = m.evaluate(snapshot, hard_cap=5, stale_reasons=["run superseded"])
    assert result.selected == ()
    assert result.refusal == m.STALE_READY_FRONT


def test_resolved_gate_does_not_refuse() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A")])
    gates = {
        "A": [
            {
                "id": "G",
                "title": "ci",
                "status": "resolved",
                "type": "async",
                "resolved_at": "2026-09-02T00:00:00.000000Z",
            }
        ]
    }
    result = m.evaluate(snapshot, hard_cap=5, gates=gates)
    assert result.selected == ("A",)
    assert result.refusal is None


# ---------------------------------------------------------------------------
# AC-T11-002: per-candidate exclusions (partial fronts stay possible)
# ---------------------------------------------------------------------------


def test_root_issue_excluded_but_batch_proceeds() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("R"), _issue("A")], root_issue_id="R")
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A",)
    assert {"id": "R", "reason": m.ROOT_ISSUE_EXCLUDED} in result.excluded
    assert result.refusal is None


def test_non_writing_issue_type_excluded_but_batch_proceeds() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("E", issue_type="epic"), _issue("A")])
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A",)
    assert {"id": "E", "reason": m.NON_WRITING_ISSUE_TYPE} in result.excluded


def test_same_batch_unlock_forbidden_excludes_dependent_not_whole_front() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B", dependency_ids=["A"])])
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("A",)
    assert {"id": "B", "reason": m.SAME_BATCH_UNLOCK_FORBIDDEN} in result.excluded
    assert result.refusal is None


def test_same_batch_cap2_matches_conformance_example() -> None:
    m = _mod()
    # Conformance table: "Same batch(cap2) -> [B]". bd ready reports B
    # before A; A depends on B. B is selected first (no unmet deps in
    # this batch); when A is evaluated, its prerequisite B was *just*
    # selected in this same batch and has not actually closed, so A is
    # excluded for SAME_BATCH_UNLOCK_FORBIDDEN. Result is the single
    # element [B] -- a genuine partial front, not a whole-front refusal.
    snapshot = _snapshot([_issue("B"), _issue("A", dependency_ids=["B"])])
    result = m.evaluate(snapshot, hard_cap=2)
    assert result.selected == ("B",)
    assert {"id": "A", "reason": m.SAME_BATCH_UNLOCK_FORBIDDEN} in result.excluded
    assert result.refusal is None


def test_capacity_reached_excludes_overflow_candidates() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B"), _issue("C")])
    result = m.evaluate(snapshot, hard_cap=2)
    assert result.selected == ("A", "B")
    assert {"id": "C", "reason": m.CAPACITY_REACHED} in result.excluded
    assert result.refusal is None


def test_capacity_unavailable_when_hard_cap_missing() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A")])
    result = m.evaluate(snapshot, hard_cap=None)
    assert result.selected == ()
    assert result.refusal == m.CAPACITY_UNAVAILABLE


def test_capacity_unavailable_when_hard_cap_non_positive() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A")])
    result = m.evaluate(snapshot, hard_cap=0)
    assert result.selected == ()
    assert result.refusal == m.CAPACITY_UNAVAILABLE


def test_capacity_exceeded_when_requested_cap_over_hard_cap() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A")])
    result = m.evaluate(snapshot, hard_cap=2, requested_cap=5)
    assert result.selected == ()
    assert result.refusal == m.CAPACITY_EXCEEDED


def test_write_scope_conflict_excludes_only_conflicting_candidate() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B")])
    matrix = {"A|B": {"verdict": "conflict", "reason": "PATH_OVERLAP"}}
    result = m.evaluate(snapshot, hard_cap=5, conflict_matrix=matrix)
    assert result.selected == ("A",)
    assert {"id": "B", "reason": m.WRITE_SCOPE_CONFLICT} in result.excluded


def test_write_scope_unknown_excludes_only_that_candidate() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B")])
    matrix = {"A|B": {"verdict": "unknown", "reason": "SCOPE_ESCAPES_ROOT"}}
    result = m.evaluate(snapshot, hard_cap=5, conflict_matrix=matrix)
    assert result.selected == ("A",)
    assert {"id": "B", "reason": m.WRITE_SCOPE_UNKNOWN} in result.excluded


def test_disjoint_scopes_proceed_up_to_capacity() -> None:
    m = _mod()
    snapshot = _snapshot([_issue("A"), _issue("B")])
    matrix = {"A|B": {"verdict": "safe", "reason": "DISJOINT_SCOPE"}}
    result = m.evaluate(snapshot, hard_cap=5, conflict_matrix=matrix)
    assert result.selected == ("A", "B")


# ---------------------------------------------------------------------------
# Ordering / determinism (AC-T11-004)
# ---------------------------------------------------------------------------


def test_live_ready_order_is_used_verbatim_by_default() -> None:
    m = _mod()
    # bd ready returned C before A before B (tie-break not alphabetic);
    # selection must copy this order, not re-sort it.
    snapshot = _snapshot([_issue("C"), _issue("A"), _issue("B")])
    result = m.evaluate(snapshot, hard_cap=5)
    assert result.selected == ("C", "A", "B")


def test_fallback_sort_orders_by_priority_then_id_bytes() -> None:
    m = _mod()
    snapshot = _snapshot(
        [
            _issue("B", priority=2),
            _issue("A", priority=1),
            _issue("C", priority=1),
        ]
    )
    result = m.evaluate(snapshot, hard_cap=5, live_ready_order=False)
    assert result.selected == ("A", "C", "B")


def test_precedence_cycle_wins_over_gate_when_both_present() -> None:
    m = _mod()
    snapshot = _snapshot(
        [_issue("A"), _issue("U")],
        cycles={"cycles": [["A", "A"]], "count": 1},
    )
    gates = {"A": [{"id": "G", "title": "h", "status": "open", "type": "human"}]}
    result = m.evaluate(snapshot, hard_cap=5, gates=gates)
    assert result.refusal == m.DEPENDENCY_CYCLE


def test_to_dict_is_json_serializable() -> None:
    import json

    m = _mod()
    snapshot = _snapshot([_issue("A")])
    result = m.evaluate(snapshot, hard_cap=5)
    encoded = json.dumps(result.to_dict(), sort_keys=True)
    assert json.loads(encoded)["selected"] == ["A"]
