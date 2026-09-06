"""AC-T13-002 (action/receipt variant matrix, tracker-update slice): the
``tracker-update`` subcommand's aggregation of ``coordinator_tracker.claim_front``'s
per-lane ``LaneClaimResult`` list into one envelope-level status.

``_handle_tracker_update`` (see ``beads_coordinator.py`` lines 555-599) never
re-derives claim semantics itself -- it only maps each result's raw status
through the shared ``_map_direct_status`` table and aggregates the mapped set
with an explicit priority: conflict > inconclusive > partial > success, with
an empty issue list (or an all-``APPLIED`` list) landing on ``partial``/
``success`` respectively. This file drives that aggregation end to end
through a real ``bc.main()`` dispatch, monkeypatching only
``coordinator_tracker.claim_front`` itself -- proving the CLI's own
aggregation logic, not re-proving ``claim_front``'s own ownership/dispatch
behavior (already covered by ``scripts/tests/beads_tracker/test_coordinator_tracker.py``).
"""

from __future__ import annotations

import json

from . import _common as common

bc = common.bc

ROOT_ISSUE = "scc-root"
LANE_A = "scc-lane-a"
LANE_B = "scc-lane-b"


def _lane(issue_id: str, status: str, *, error_code: str | None = None):
    return bc.coordinator_tracker.LaneClaimResult(
        issue_id=issue_id,
        status=status,
        classification=None,
        ownership_epoch=None,
        error_code=error_code,
    )


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _argv(run, input_path, *, actor="parent"):
    return [
        "tracker-update",
        "--run-dir",
        str(run["run_directory"]),
        "--input",
        str(input_path),
        "--actor",
        actor,
        "--json",
    ]


def test_all_applied_reports_success(tmp_path, monkeypatch, capsys):
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A, LANE_B], root_issue_id=ROOT_ISSUE
    )
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "claim_front",
        lambda *a, **k: [
            _lane(LANE_A, bc.direct_operation.APPLIED),
            _lane(LANE_B, bc.direct_operation.APPLIED),
        ],
    )
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": [LANE_A, LANE_B]})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "execute_set"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["coverage_gaps"] == []
    assert out["issue_ids"] == [LANE_A, LANE_B]


def test_empty_issue_list_reports_partial_with_no_pending_actions_and_exits_nonzero(
    tmp_path, capsys
):
    """An empty ``issues`` list means ``claim_front`` is never even called
    with anything to do -- ``mapped`` is the empty set, which the handler's
    ``if not mapped or mapped == {"success"}:`` branch reports as
    ``"partial"`` (not ``"success"``) since ``mapped`` is falsy. Because
    ``_handle_tracker_update`` never populates ``pending_actions``,
    ``operation_result.result_exit_code``'s ``status == "partial" and
    pending_actions`` carve-out does not apply here, so this still exits
    non-zero despite reporting "partial" rather than a hard failure status.
    """
    run = common.make_run(tmp_path, issue_ids=[], root_issue_id=ROOT_ISSUE)
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": []})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "partial"
    assert out["error_code"] == "PARTIAL"
    assert out["coverage_gaps"] == []
    assert exit_code == 1


def test_refused_lane_mixed_with_applied_reports_partial_with_its_own_error_code(
    tmp_path, monkeypatch, capsys
):
    """A ``REFUSED`` lane (e.g. the root issue slipping into ``issues``) maps
    to ``"blocked"``, which -- mixed with a ``"success"``-mapped lane and no
    ``"conflict"``/``"inconclusive"`` entries -- falls through to the
    handler's final ``else: "partial"`` branch, not ``"success"`` and not
    the raw ``"blocked"`` value itself.
    """
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "claim_front",
        lambda *a, **k: [
            _lane(LANE_A, bc.direct_operation.APPLIED),
            _lane(
                ROOT_ISSUE,
                "REFUSED",
                error_code="COORDINATOR_TRACKER_ROOT_ISSUE_INELIGIBLE",
            ),
        ],
    )
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": [LANE_A, ROOT_ISSUE]})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "partial"
    assert out["error_code"] == "COORDINATOR_TRACKER_ROOT_ISSUE_INELIGIBLE"
    assert len(out["coverage_gaps"]) == 1
    assert out["safe_next_action"] is not None
    assert exit_code == 1


def test_any_conflict_reports_conflict_and_exit_4_even_alongside_applied(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A, LANE_B], root_issue_id=ROOT_ISSUE
    )
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "claim_front",
        lambda *a, **k: [
            _lane(LANE_A, bc.direct_operation.APPLIED),
            _lane(
                LANE_B, bc.direct_operation.CONFLICT, error_code="LANE_CLAIM_CONFLICT"
            ),
        ],
    )
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": [LANE_A, LANE_B]})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "conflict"
    assert out["error_code"] == "LANE_CLAIM_CONFLICT"
    assert exit_code == 4


def test_unknown_without_conflict_reports_inconclusive_and_exit_5(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A, LANE_B], root_issue_id=ROOT_ISSUE
    )
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "claim_front",
        lambda *a, **k: [
            _lane(LANE_A, bc.direct_operation.APPLIED),
            _lane(
                LANE_B,
                bc.direct_operation.UNKNOWN,
                error_code="COORDINATOR_TRACKER_OWNERSHIP_UNKNOWN",
            ),
        ],
    )
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": [LANE_A, LANE_B]})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "inconclusive"
    assert out["error_code"] == "COORDINATOR_TRACKER_OWNERSHIP_UNKNOWN"
    assert exit_code == 5


def test_unknown_and_not_applied_without_conflict_prefers_inconclusive(
    tmp_path, monkeypatch, capsys
):
    """Priority order confirmation: ``inconclusive`` outranks a plain
    ``"blocked"``-mapped (``NOT_APPLIED``) entry when no ``"conflict"``
    entry is present at all.
    """
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A, LANE_B], root_issue_id=ROOT_ISSUE
    )
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "claim_front",
        lambda *a, **k: [
            _lane(
                LANE_A,
                bc.direct_operation.NOT_APPLIED,
                error_code="LANE_CLAIM_NOT_APPLIED",
            ),
            _lane(LANE_B, bc.direct_operation.UNKNOWN, error_code="LANE_CLAIM_UNKNOWN"),
        ],
    )
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": [LANE_A, LANE_B]})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "inconclusive"
    assert exit_code == 5


def test_forwards_issues_and_actor_verbatim_to_claim_front(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A, LANE_B], root_issue_id=ROOT_ISSUE
    )
    captured = {}

    def fake_claim_front(context, *, ownership, issues, actor):
        captured["issues"] = list(issues)
        captured["actor"] = actor
        return [_lane(issue_id, bc.direct_operation.APPLIED) for issue_id in issues]

    monkeypatch.setattr(bc.coordinator_tracker, "claim_front", fake_claim_front)
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {"issues": [LANE_A, LANE_B]})

    exit_code = bc.main(_argv(run, input_path, actor="reviewer"))

    assert exit_code == 0
    assert captured == {"issues": [LANE_A, LANE_B], "actor": "reviewer"}


def test_missing_issues_field_exits_2_with_input_missing_fields(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[], root_issue_id=ROOT_ISSUE)
    input_path = tmp_path / "tracker-update.json"
    _write_json(input_path, {})

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"
