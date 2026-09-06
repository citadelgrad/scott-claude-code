"""AC-T13-002 (action/receipt variant matrix, action prepare/resolve slice):
the ``action prepare`` and ``action resolve`` subcommands.

``_handle_action_prepare`` (see ``beads_coordinator.py`` lines 365-398)
always reports ``status: "human_action_required"`` -- that is the whole
point of ``protected_action.prepare_protected_action``: it journals a
PREPARED record and hands back a ``pending_action`` document, but never
performs the effect itself, so there is no other possible outcome. The CLI's
only jobs here are: forward every ``--input`` field verbatim, populate
``pending_actions`` from the frozen call's return value, and always attach
the fixed ``PROTECTED_ACTION_PENDING`` error code.

``_handle_action_resolve`` (lines 404-435) maps
``protected_action.resolve_protected_action``'s returned ``status`` through
the shared ``_map_direct_status`` table, and its ``error_code`` always falls
back to the raw status itself when the resolution dict omits an explicit one
(the frozen function's real return shape never actually includes an
``error_code`` key, so this fallback is the only path exercised in practice
-- still worth locking down explicitly, plus the override path in case a
future resolution variant does supply one).

This file drives both subcommands through a real ``bc.main()`` dispatch over
a real run directory (``common.make_run``), monkeypatching only
``protected_action.prepare_protected_action``/``resolve_protected_action``
themselves -- proving the CLI's own envelope-building, not re-proving
``protected_action``'s own journal/grant logic (already covered by
``scripts/tests/beads_tracker/test_protected_action.py``).
"""

from __future__ import annotations

import json

from . import _common as common

bc = common.bc

ISSUE_ID = "scc-lane-a"

_PREPARE_INPUT = {
    "effect_type": "TRACKER_NOTE",
    "target_identity": "issue:scc-lane-a",
    "target_sha256": "a" * 64,
    "precondition_sha256": "b" * 64,
    "summary": "Append a coordination note",
    "probe_type": "issue_get",
    "probe_argv": ["issue", "get", ISSUE_ID],
    "issue_id": ISSUE_ID,
    "ownership_epoch": 1,
}


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pending_action(**overrides):
    """A ``beads.pending-action.v1`` document conforming to the
    ``protected_harness_effect`` variant of the schema's ``oneOf`` -- every
    field the schema marks ``required`` for that variant, with
    schema-conforming placeholder values (64-hex-char shas, a real
    ``common.make_run_id()`` run id, an ISO-microsecond UTC timestamp for
    ``expires_at``).
    """
    payload = {
        "schema_version": "beads.pending-action.v1",
        "action": "protected_harness_effect",
        "action_id": "c" * 64,
        "operation_id": "d" * 64,
        "run_id": common.make_run_id(),
        "issue_id": ISSUE_ID,
        "ownership_epoch": 1,
        "target_sha256": "a" * 64,
        "precondition_sha256": "b" * 64,
        "payload_sha256": "e" * 64,
        "authority_class": "coordinator_parent",
        "payload": {
            "summary": "Append a coordination note",
            "artifact_path": None,
            "parameters": [],
        },
        "expires_at": "2026-09-05T00:00:00.000000Z",
        "required_receipt_variant": "protected_harness_effect",
    }
    payload.update(overrides)
    return payload


def test_action_prepare_always_reports_human_action_required_with_pending_action(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    pending_action = _pending_action()
    monkeypatch.setattr(
        bc.protected_action,
        "prepare_protected_action",
        lambda *a, **k: {
            "operation_id": "c" * 64,
            "status": bc.protected_action.HUMAN_ACTION_REQUIRED,
            "prepared": {"schema_version": "beads.operation-journal-event.v1"},
            "pending_action": pending_action,
        },
    )
    input_path = tmp_path / "action-prepare.json"
    _write_json(input_path, _PREPARE_INPUT)

    exit_code = bc.main(
        [
            "action",
            "prepare",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 6
    assert out["operation"] == "prepare_action"
    assert out["status"] == "human_action_required"
    assert out["error_code"] == "PROTECTED_ACTION_PENDING"
    assert out["pending_actions"] == [pending_action]
    assert out["issue_ids"] == [ISSUE_ID]


def test_action_prepare_forwards_every_input_field_verbatim(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def fake_prepare(context, **kwargs):
        captured.update(kwargs)
        return {
            "operation_id": "c" * 64,
            "status": bc.protected_action.HUMAN_ACTION_REQUIRED,
            "prepared": {},
            "pending_action": _pending_action(),
        }

    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(bc.protected_action, "prepare_protected_action", fake_prepare)
    input_path = tmp_path / "action-prepare.json"
    _write_json(input_path, _PREPARE_INPUT)

    exit_code = bc.main(
        [
            "action",
            "prepare",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )

    assert exit_code == 6
    assert captured["effect_type"] == "TRACKER_NOTE"
    assert captured["target_identity"] == "issue:scc-lane-a"
    assert captured["target_sha256"] == "a" * 64
    assert captured["precondition_sha256"] == "b" * 64
    assert captured["summary"] == "Append a coordination note"
    assert captured["probe_type"] == "issue_get"
    assert captured["probe_argv"] == ["issue", "get", ISSUE_ID]
    assert captured["issue_id"] == ISSUE_ID
    assert captured["ownership_epoch"] == 1


def test_action_prepare_missing_required_field_exits_2(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    incomplete = dict(_PREPARE_INPUT)
    del incomplete["summary"]
    input_path = tmp_path / "action-prepare.json"
    _write_json(input_path, incomplete)

    exit_code = bc.main(
        [
            "action",
            "prepare",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"


def _resolve_input(**overrides):
    payload = {
        "prepared": {"schema_version": "beads.operation-journal-event.v1"},
        "pending_action": {"action": "protected_harness_effect", "issue_id": ISSUE_ID},
        "receipt": {"schema_version": "beads.action-receipt.v1", "outcome": "applied"},
    }
    payload.update(overrides)
    return payload


def test_action_resolve_applied_reports_success(tmp_path, monkeypatch, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.protected_action,
        "resolve_protected_action",
        lambda *a, **k: {"status": bc.direct_operation.APPLIED},
    )
    input_path = tmp_path / "action-resolve.json"
    _write_json(input_path, _resolve_input())

    exit_code = bc.main(
        [
            "action",
            "resolve",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "resolve_action"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["issue_ids"] == [ISSUE_ID]


def test_action_resolve_not_applied_without_explicit_error_code_falls_back_to_raw_status(
    tmp_path, monkeypatch, capsys
):
    """The frozen function's real return shape never actually includes an
    ``error_code`` key -- ``resolution.get("error_code", raw_status)`` falls
    back to the raw status string itself, not a fixed constant.
    """
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.protected_action,
        "resolve_protected_action",
        lambda *a, **k: {"status": bc.direct_operation.NOT_APPLIED},
    )
    input_path = tmp_path / "action-resolve.json"
    _write_json(input_path, _resolve_input())

    exit_code = bc.main(
        [
            "action",
            "resolve",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == bc.direct_operation.NOT_APPLIED
    assert out["blockers"] == [bc.direct_operation.NOT_APPLIED]


def test_action_resolve_honors_an_explicit_error_code_when_present(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.protected_action,
        "resolve_protected_action",
        lambda *a, **k: {
            "status": bc.direct_operation.CONFLICT,
            "error_code": "RECEIPT_PRESTATE_MISMATCH",
        },
    )
    input_path = tmp_path / "action-resolve.json"
    _write_json(input_path, _resolve_input())

    exit_code = bc.main(
        [
            "action",
            "resolve",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert out["status"] == "conflict"
    assert out["error_code"] == "RECEIPT_PRESTATE_MISMATCH"
    # "conflict" never populates blockers, matching the lane-gate family's
    # documented asymmetry (see test_lane_gates.py).
    assert out["blockers"] == []


def test_action_resolve_missing_status_defaults_to_unknown_and_reports_blocked(
    tmp_path, monkeypatch, capsys
):
    """``resolution.get("status", "UNKNOWN")`` -- an ``"UNKNOWN"`` raw status
    is not a key in ``_DIRECT_CLASSIFICATION_STATUS`` (only the constant
    ``direct_operation.UNKNOWN`` -- the same string value -- is), so this
    also confirms the two are the same literal value, not merely similar.
    """
    assert bc.direct_operation.UNKNOWN == "UNKNOWN"
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.protected_action, "resolve_protected_action", lambda *a, **k: {}
    )
    input_path = tmp_path / "action-resolve.json"
    _write_json(input_path, _resolve_input())

    exit_code = bc.main(
        [
            "action",
            "resolve",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 5
    assert out["status"] == "inconclusive"
    assert out["error_code"] == "UNKNOWN"


def test_action_resolve_missing_required_field_exits_2(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    incomplete = _resolve_input()
    del incomplete["receipt"]
    input_path = tmp_path / "action-resolve.json"
    _write_json(input_path, incomplete)

    exit_code = bc.main(
        [
            "action",
            "resolve",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"
