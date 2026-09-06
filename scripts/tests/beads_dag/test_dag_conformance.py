"""DAG scheduling conformance fixtures (scc-0pu.16 / t15).

Loads every fixture under ``scripts/tests/fixtures/beads_dag/`` and asserts
its ``expected`` block against the real, frozen t05/t11 predecessor
contract (``build_ready_front.evaluate`` via
``coordinator_front.build_front_from_snapshot``). No production code here:
these fixtures and this loader are the executable tests themselves.

AC coverage:
  AC-T15-001 -- ready membership: static fixtures 01-03, 06, 12.
  AC-T15-002 -- cycles/inversions/inconsistent snapshots block with no
                mutation: static fixtures 04, 05, 07, 09, plus the
                mutation-denial tests below.
  AC-T15-003 -- same-batch blocker never unlocks its dependent, and live
                changes require fresh recomputation: static fixture 12 and
                the dynamic multi-step fixture.
  AC-T15-004 -- deterministic capacity/ordering, overlap/unknown never
                concurrent: static fixtures 08, 10, 11, plus the
                determinism-repeat tests below.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from ._common import (
    dynamic_fixture_paths,
    evaluate_step,
    excluded_reasons,
    load,
    load_json,
    static_fixture_paths,
)


def _mod():
    return load("coordinator_front")


def _assert_matches_expected(front, expected: dict[str, Any]) -> None:
    assert list(front.selected) == expected["selected"]
    assert front.refusal == expected["refusal"]
    assert excluded_reasons(front) == expected["excluded"]


# --- AC-T15-001/002/004: static fixture matrix ------------------------------


@pytest.mark.parametrize("path", static_fixture_paths(), ids=lambda p: p.stem)
def test_static_fixture_matches_expected(path: Path, tmp_path: Path) -> None:
    fixture = load_json(path)
    m = _mod()
    front = evaluate_step(m, fixture, repository_root=str(tmp_path))
    _assert_matches_expected(front, fixture["expected"])


@pytest.mark.parametrize("path", static_fixture_paths(), ids=lambda p: p.stem)
def test_static_fixture_input_snapshot_is_not_mutated(
    path: Path, tmp_path: Path
) -> None:
    """AC-T15-002's mutation-denial clause: evaluating a fixture must never
    change the snapshot data that produced it."""
    fixture = load_json(path)
    m = _mod()
    from ._common import build_snapshot

    snapshot = build_snapshot(fixture)
    before = copy.deepcopy(snapshot)
    m.build_front_from_snapshot(
        snapshot,
        repository_root=str(tmp_path),
        hard_cap=fixture["hard_cap"],
        declared_edges=fixture.get("declared_edges"),
        consistency=fixture.get("consistency"),
        live_ready_order=fixture.get("live_ready_order", True),
    )
    assert snapshot == before


@pytest.mark.parametrize("path", static_fixture_paths(), ids=lambda p: p.stem)
def test_static_fixture_is_deterministic_across_repeated_calls(
    path: Path, tmp_path: Path
) -> None:
    """AC-T15-004: repeated evaluation of the same snapshot is byte-identical."""
    fixture = load_json(path)
    m = _mod()
    results = [
        evaluate_step(m, fixture, repository_root=str(tmp_path)).to_dict()
        for _ in range(5)
    ]
    assert all(r == results[0] for r in results)


def test_every_ac_is_covered_by_at_least_one_static_fixture() -> None:
    declared: set[str] = set()
    for path in static_fixture_paths():
        declared.update(load_json(path)["ac"])
    for ac in ("AC-T15-001", "AC-T15-002", "AC-T15-003", "AC-T15-004"):
        assert ac in declared, f"no static fixture declares {ac}"


# --- AC-T15-003: same-batch blocker + live recomputation --------------------


def _dynamic_fixture() -> dict[str, Any]:
    paths = dynamic_fixture_paths()
    assert paths, "expected at least one dynamic fixture for AC-T15-003"
    return load_json(paths[0])


@pytest.mark.parametrize(
    "step",
    _dynamic_fixture()["steps"],
    ids=lambda step: step["step_id"],
)
def test_dynamic_step_matches_expected_in_isolation(
    step: dict[str, Any], tmp_path: Path
) -> None:
    """Each step's snapshot alone must produce that step's expected result --
    proves no step depends on carried-over state from another."""
    m = _mod()
    front = evaluate_step(m, step, repository_root=str(tmp_path))
    _assert_matches_expected(front, step["expected"])


def test_dynamic_recomputation_reflects_each_live_change_in_sequence(
    tmp_path: Path,
) -> None:
    """The defining AC-T15-003 property: replaying the fixture's steps in
    order, through one long-lived module instance, must yield each step's
    own correct answer -- never a memoized/cached answer from the step
    before it. Consecutive steps are also asserted to actually differ,
    so this test cannot pass by accident if recomputation were skipped.
    """
    fixture = _dynamic_fixture()
    m = _mod()
    results = [
        evaluate_step(m, step, repository_root=str(tmp_path)).to_dict()
        for step in fixture["steps"]
    ]
    for step, result in zip(fixture["steps"], results):
        assert result["selected"] == step["expected"]["selected"]
        assert result["refusal"] == step["expected"]["refusal"]
        assert {e["id"]: e["reason"] for e in result["excluded"]} == step["expected"][
            "excluded"
        ]
    for prev, curr in zip(results, results[1:]):
        assert prev != curr, "a live change must change the recomputed front"


def test_dynamic_fixture_steps_are_not_mutated_by_evaluation(tmp_path: Path) -> None:
    fixture = _dynamic_fixture()
    before = copy.deepcopy(fixture)
    m = _mod()
    for step in fixture["steps"]:
        evaluate_step(m, step, repository_root=str(tmp_path))
    assert fixture == before


def test_same_batch_unlock_forbidden_static_fixture_is_present() -> None:
    """Guards against silently deleting the dedicated single-call fixture
    for the first half of AC-T15-003 (as opposed to only the dynamic story)."""
    ids = {load_json(p)["id"] for p in static_fixture_paths()}
    assert "same-batch-blocker" in ids
