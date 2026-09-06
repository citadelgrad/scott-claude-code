"""AC-T13-004 (full happy path) and AC-T13-002 (status-mapping matrix): the
``finish`` subcommand's terminal run-pointer publish.

``_handle_finish`` (see ``beads_coordinator.py`` lines 774-824) rebuilds a
``state.StartRunInput`` from the run's own manifest plus the caller's
``--input`` fields, asks ``coordinator_tracker.pointer_callbacks`` for an
``observe``/``publish`` pair, observes the terminal pointer first and only
publishes when the observation does not already show the intended value
present, then maps ``direct_operation.CLASSIFICATION_STATUS`` through the
shared ``_map_direct_status`` table exactly like ``tracker-update``/the lane
gates do.

Two tests drive this for real, end to end, through the same
``common.tracker_double`` fixture ``_common.py`` documents as built for
exactly this purpose (a tiny in-memory tracker double that round-trips
whatever ``set_run_pointer`` writes back out of the next ``issue_get``) --
this proves the CLI's wiring into ``coordinator_tracker.pointer_callbacks``
genuinely works, not merely that a mock was called correctly. The remaining
status-mapping tests monkeypatch ``coordinator_tracker.pointer_callbacks``
itself to return fixed ``state.PointerObservation`` values, following the
same non-duplication pattern as ``test_tracker_update.py``/``test_gate.py``
-- ``pointer_callbacks``'s own classification logic is already exhaustively
covered by ``scripts/tests/beads_tracker/test_coordinator_tracker.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import _common as common

bc = common.bc
safe_bd = common.safe_bd
state = common.state

ROOT_ISSUE = "scc-root"
LANE_A = "scc-lane-a"


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _finish_input(repo: Path, **overrides):
    payload = {
        "terminal_status": "completed",
        "git_common_dir": str(repo / ".git"),
        "scope_issue_ids": [LANE_A],
        "base_git_commit": "a" * 40,
        "authority_snapshot_sha256": "b" * 64,
        "ownership_epoch": 1,
    }
    payload.update(overrides)
    return payload


def _argv(run, input_path, *, actor="parent"):
    return [
        "finish",
        "--run-dir",
        str(run["run_directory"]),
        "--input",
        str(input_path),
        "--actor",
        actor,
        "--json",
    ]


def test_finish_happy_path_publishes_the_terminal_pointer_and_reports_success(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    fake, _box = common.tracker_double(ROOT_ISSUE)
    monkeypatch.setattr(safe_bd, "run_profile", fake)

    input_path = tmp_path / "finish.json"
    _write_json(input_path, _finish_input(repo))

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "finish"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["blockers"] == []
    assert out["coverage_gaps"] == []
    assert out["issue_ids"] == [ROOT_ISSUE]
    # The terminal pointer must have actually been published, not merely
    # reported as such.
    assert fake.count("set_run_pointer") == 1


def test_finish_second_call_with_the_same_pointer_short_circuits_without_republishing(
    tmp_path, monkeypatch, capsys
):
    """A repeated ``finish`` call with an identical pointer must observe the
    already-published value as the intended one and skip ``publish``
    entirely (the ``before.classification == INTENDED_EFFECT_PRESENT``
    branch in ``_handle_finish``) -- still reporting success, but without a
    second native dispatch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    fake, _box = common.tracker_double(ROOT_ISSUE)
    monkeypatch.setattr(safe_bd, "run_profile", fake)

    input_path = tmp_path / "finish.json"
    _write_json(input_path, _finish_input(repo))

    first_exit = bc.main(_argv(run, input_path))
    capsys.readouterr()
    assert first_exit == 0
    assert fake.count("set_run_pointer") == 1

    second_exit = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert second_exit == 0
    assert out["status"] == "success"
    # No second publish: the pointer already matched the intended value.
    assert fake.count("set_run_pointer") == 1


def _pointer_callbacks_returning(classification: str):
    observation = state.PointerObservation(classification, "0" * 64, None)
    return lambda *a, **k: state.PointerCallbacks(
        observe=lambda pointer: observation,
        publish=lambda pointer: observation,
    )


def test_finish_not_applied_classification_reports_blocked(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "pointer_callbacks",
        _pointer_callbacks_returning(bc.direct_operation.PRESTATE_UNCHANGED),
    )
    input_path = tmp_path / "finish.json"
    _write_json(input_path, _finish_input(repo))

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == bc.direct_operation.NOT_APPLIED
    assert out["blockers"] == [bc.direct_operation.NOT_APPLIED]
    assert out["coverage_gaps"] != []
    assert out["safe_next_action"] is not None


def test_finish_conflicting_effect_classification_reports_conflict_and_exit_4_with_no_blockers(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "pointer_callbacks",
        _pointer_callbacks_returning(bc.direct_operation.CONFLICTING_EFFECT),
    )
    input_path = tmp_path / "finish.json"
    _write_json(input_path, _finish_input(repo))

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert out["status"] == "conflict"
    assert out["error_code"] == bc.direct_operation.CONFLICT
    assert out["blockers"] == []
    assert out["coverage_gaps"] != []


def test_finish_insufficient_observation_classification_reports_inconclusive_and_exit_5(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    monkeypatch.setattr(
        bc.coordinator_tracker,
        "pointer_callbacks",
        _pointer_callbacks_returning(bc.direct_operation.INSUFFICIENT_OBSERVATION),
    )
    input_path = tmp_path / "finish.json"
    _write_json(input_path, _finish_input(repo))

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 5
    assert out["status"] == "inconclusive"
    assert out["error_code"] == bc.direct_operation.UNKNOWN


def test_finish_forwards_input_and_manifest_fields_into_start_run_input(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    manifest = run["manifest"]
    captured = {}

    def fake_pointer_callbacks(request, *, actor):
        captured["request"] = request
        captured["actor"] = actor
        observation = state.PointerObservation(
            bc.direct_operation.INTENDED_EFFECT_PRESENT, "0" * 64, None
        )
        return state.PointerCallbacks(
            observe=lambda pointer: observation, publish=lambda pointer: observation
        )

    monkeypatch.setattr(
        bc.coordinator_tracker, "pointer_callbacks", fake_pointer_callbacks
    )
    input_path = tmp_path / "finish.json"
    _write_json(
        input_path,
        _finish_input(
            repo,
            terminal_status="completed",
            scope_issue_ids=[LANE_A, "scc-lane-b"],
            base_git_commit="f" * 40,
            authority_snapshot_sha256="c" * 64,
            ownership_epoch=3,
        ),
    )

    exit_code = bc.main(_argv(run, input_path, actor="reviewer"))

    assert exit_code == 0
    request = captured["request"]
    assert captured["actor"] == "reviewer"
    assert request.request_id == manifest["request_id"]
    assert request.repository_root == manifest["repository_root"]
    assert request.git_common_dir == str(repo / ".git")
    assert request.workspace == manifest["workspace"]
    assert request.run_root == str(run["run_directory"].parent)
    assert request.root_issue_id == manifest["root_issue_id"]
    assert request.scope_issue_ids == (LANE_A, "scc-lane-b")
    assert request.actor == "reviewer"
    assert request.base_git_commit == "f" * 40
    assert request.authority_snapshot_sha256 == "c" * 64
    assert request.workspace_identity_sha256 == manifest["workspace_identity_sha256"]


def test_finish_missing_required_field_exits_2(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = common.make_run(repo, root_issue_id=ROOT_ISSUE)
    incomplete = _finish_input(repo)
    del incomplete["ownership_epoch"]
    input_path = tmp_path / "finish.json"
    _write_json(input_path, incomplete)

    exit_code = bc.main(_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"
