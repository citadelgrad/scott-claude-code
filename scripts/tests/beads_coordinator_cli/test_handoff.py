"""AC-T13-002/T13-003 (action/receipt variant matrix, handoff slice): the
``handoff-launch`` and ``handoff-accept`` subcommands.

``_handle_handoff_launch`` (see ``beads_coordinator.py`` lines 662-688) is a
thin wrapper: it forwards ``data["handoff"]`` and ``--actor`` straight to
``coordinator_handoff.launch``, then maps the returned
``HandoffLaunchResult.status`` through the shared ``_map_direct_status``
table -- exactly the same envelope shape as the tracker-update/lane-gate
families, so this file follows their established monkeypatch-the-frozen-verb
pattern rather than re-proving ``coordinator_handoff.launch``'s own guard
logic (covered by its own test suite).

``_handle_handoff_accept`` (lines 715-750) has one wrinkle none of the other
handlers share: before it ever calls ``coordinator_handoff.accept``, it runs
``data["operation"]`` through ``_reconstruct_direct_operation`` (lines
691-712), which builds a *real*, validated ``direct_operation.DirectOperation``
(via a real ``direct_operation.Readback``) -- this happens unconditionally,
so even with ``accept`` itself monkeypatched, the ``--input`` JSON's
``operation`` object must be genuine enough to survive
``DirectOperation.__post_init__`` (a real ``effect_type`` drawn from
``direct_operation.EFFECT_PROFILES``, a non-empty ``caller_key`` with no NUL
byte, and no ``"actor"`` key inside ``arguments``). This file's
``_operation_spec`` helper builds exactly that, and one dedicated test
(``test_handoff_accept_rejects_an_unknown_effect_type_before_calling_accept``)
proves the reconstruction step's own validation fires -- and that ``accept``
is never even reached -- when the caller supplies a bogus ``effect_type``.
"""

from __future__ import annotations

import json

from . import _common as common

bc = common.bc

ISSUE_ID = "scc-lane-a"


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _launch_result(status, **overrides):
    fields = {
        "status": status,
        "handoff_id": "handoff-0001",
        "operation_id": "c" * 64,
        "already_launched": False,
        "prepared": None,
        "resolution": None,
    }
    fields.update(overrides)
    return bc.coordinator_handoff.HandoffLaunchResult(**fields)


def _acceptance_result(status, **overrides):
    fields = {
        "status": status,
        "handoff_id": "handoff-0001",
        "accept_operation_id": "d" * 64,
        "guard_passed": True,
        "error_code": None,
        "accept_prepared": None,
        "accept_resolution": None,
        "operation_result": None,
    }
    fields.update(overrides)
    return bc.coordinator_handoff.HandoffAcceptanceResult(**fields)


def _operation_spec(**overrides):
    """A ``data["operation"]`` object that survives
    ``_reconstruct_direct_operation`` -- a real ``effect_type`` from
    ``EFFECT_PROFILES``, a valid ``caller_key``, and a ``readback`` sub-dict
    with all four fields ``Readback`` requires (their actual values are
    unconstrained -- ``Readback`` and ``DirectOperation`` only validate
    ``effect_type``/``caller_key``/``arguments["actor"]``, not readback
    content).
    """
    spec = {
        "caller_key": "coordinator-handoff-accept-0001",  # gitleaks:allow -- fixture id, not a secret
        "effect_type": "TRACKER_NOTE",
        "target_identity": f"issue:{ISSUE_ID}",
        "issue_id": ISSUE_ID,
        "ownership_epoch": 1,
        "arguments": {},
        "readback": {
            "profile": "append_marker_note",
            "arguments": {},
            "intended": {"note": "present"},
            "prestate": bc.direct_operation.ABSENT,
        },
    }
    spec.update(overrides)
    return spec


def test_handoff_launch_success_reports_success(tmp_path, monkeypatch, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "launch",
        lambda *a, **k: _launch_result(bc.direct_operation.APPLIED),
    )
    input_path = tmp_path / "handoff-launch.json"
    _write_json(input_path, {"handoff": {"issue_id": ISSUE_ID}})

    exit_code = bc.main(
        [
            "handoff-launch",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--actor",
            "parent",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "execute_one"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["issue_ids"] == [ISSUE_ID]


def test_handoff_launch_not_applied_reports_blocked_with_raw_status_as_error_code(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "launch",
        lambda *a, **k: _launch_result(bc.direct_operation.NOT_APPLIED),
    )
    input_path = tmp_path / "handoff-launch.json"
    _write_json(input_path, {"handoff": {"issue_id": ISSUE_ID}})

    exit_code = bc.main(
        [
            "handoff-launch",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--actor",
            "parent",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == bc.direct_operation.NOT_APPLIED
    assert out["blockers"] == [bc.direct_operation.NOT_APPLIED]
    assert out["safe_next_action"] is not None


def test_handoff_launch_conflict_reports_conflict_and_exit_4_with_no_blockers(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "launch",
        lambda *a, **k: _launch_result(bc.direct_operation.CONFLICT),
    )
    input_path = tmp_path / "handoff-launch.json"
    _write_json(input_path, {"handoff": {"issue_id": ISSUE_ID}})

    exit_code = bc.main(
        [
            "handoff-launch",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--actor",
            "parent",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert out["status"] == "conflict"
    assert out["blockers"] == []


def test_handoff_launch_without_issue_id_reports_empty_issue_ids(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "launch",
        lambda *a, **k: _launch_result(bc.direct_operation.APPLIED),
    )
    input_path = tmp_path / "handoff-launch.json"
    _write_json(input_path, {"handoff": {}})

    exit_code = bc.main(
        [
            "handoff-launch",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--actor",
            "parent",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["issue_ids"] == []


def test_handoff_launch_forwards_handoff_and_actor_verbatim(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    captured = {}

    def fake_launch(context, *, handoff, actor):
        captured["handoff"] = handoff
        captured["actor"] = actor
        return _launch_result(bc.direct_operation.APPLIED)

    monkeypatch.setattr(bc.coordinator_handoff, "launch", fake_launch)
    handoff_payload = {"issue_id": ISSUE_ID, "handoff_id": "handoff-0007"}
    input_path = tmp_path / "handoff-launch.json"
    _write_json(input_path, {"handoff": handoff_payload})

    exit_code = bc.main(
        [
            "handoff-launch",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--actor",
            "reviewer",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured == {"handoff": handoff_payload, "actor": "reviewer"}


def test_handoff_launch_missing_handoff_field_exits_2(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    input_path = tmp_path / "handoff-launch.json"
    _write_json(input_path, {})

    exit_code = bc.main(
        [
            "handoff-launch",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
            "--actor",
            "parent",
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"


def _accept_input(**overrides):
    payload = {
        "handoff": {"issue_id": ISSUE_ID, "handoff_id": "handoff-0001"},
        "result": {"outcome": "applied"},
        "operation": _operation_spec(),
    }
    payload.update(overrides)
    return payload


def _accept_argv(run, input_path, *, actor="parent"):
    return [
        "handoff-accept",
        "--run-dir",
        str(run["run_directory"]),
        "--input",
        str(input_path),
        "--actor",
        actor,
        "--json",
    ]


def test_handoff_accept_applied_reports_success(tmp_path, monkeypatch, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "accept",
        lambda *a, **k: _acceptance_result(bc.direct_operation.APPLIED),
    )
    input_path = tmp_path / "handoff-accept.json"
    _write_json(input_path, _accept_input())

    exit_code = bc.main(_accept_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "execute_one"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["issue_ids"] == [ISSUE_ID]


def test_handoff_accept_guard_failure_reports_blocked_with_its_error_code(
    tmp_path, monkeypatch, capsys
):
    """A failed AC-T12-006 guard reports ``NOT_APPLIED``-classified status
    (mapped to "blocked") with the specific guard's error code surfaced via
    ``getattr(acceptance, "error_code", acceptance.status)`` -- here the
    dataclass really does carry an explicit ``error_code``, so that value
    (not the raw status) must appear.
    """
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "accept",
        lambda *a, **k: _acceptance_result(
            bc.direct_operation.NOT_APPLIED,
            guard_passed=False,
            error_code="HANDOFF_EPOCH_MISMATCH",
        ),
    )
    input_path = tmp_path / "handoff-accept.json"
    _write_json(input_path, _accept_input())

    exit_code = bc.main(_accept_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "HANDOFF_EPOCH_MISMATCH"
    assert out["blockers"] == ["HANDOFF_EPOCH_MISMATCH"]


def test_handoff_accept_conflict_reports_conflict_and_exit_4(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "accept",
        lambda *a, **k: _acceptance_result(
            bc.direct_operation.CONFLICT, error_code="HANDOFF_RESULT_CONFLICT"
        ),
    )
    input_path = tmp_path / "handoff-accept.json"
    _write_json(input_path, _accept_input())

    exit_code = bc.main(_accept_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert out["status"] == "conflict"
    assert out["error_code"] == "HANDOFF_RESULT_CONFLICT"
    assert out["blockers"] == []


def test_handoff_accept_forwards_handoff_result_operation_and_actor(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    captured = {}

    def fake_accept(context, *, handoff, result, operation, actor, ownership):
        captured["handoff"] = handoff
        captured["result"] = result
        captured["operation"] = operation
        captured["actor"] = actor
        return _acceptance_result(bc.direct_operation.APPLIED)

    monkeypatch.setattr(bc.coordinator_handoff, "accept", fake_accept)
    input_path = tmp_path / "handoff-accept.json"
    _write_json(input_path, _accept_input())

    exit_code = bc.main(_accept_argv(run, input_path, actor="reviewer"))

    assert exit_code == 0
    assert captured["handoff"] == {"issue_id": ISSUE_ID, "handoff_id": "handoff-0001"}
    assert captured["result"] == {"outcome": "applied"}
    assert captured["actor"] == "reviewer"
    # The reconstructed operation is a real DirectOperation, not the raw dict.
    assert isinstance(captured["operation"], bc.direct_operation.DirectOperation)
    assert captured["operation"].effect_type == "TRACKER_NOTE"
    assert (
        captured["operation"].caller_key
        == "coordinator-handoff-accept-0001"  # gitleaks:allow -- fixture id, not a secret
    )
    assert captured["operation"].issue_id == ISSUE_ID


def test_handoff_accept_rejects_an_unknown_effect_type_before_calling_accept(
    tmp_path, monkeypatch, capsys
):
    """``_reconstruct_direct_operation`` builds the ``DirectOperation`` before
    ``coordinator_handoff.accept`` is ever invoked, so a bogus
    ``effect_type`` trips ``DirectOperation.__post_init__``'s
    ``DIRECT_OPERATION_EFFECT_UNKNOWN`` refusal (a ``DirectOperationError``,
    one of ``_DIRECT_ERRORS``) -- caught by ``main()``'s second exception
    tier -- and ``accept`` must never be reached at all.
    """
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    called = []
    monkeypatch.setattr(
        bc.coordinator_handoff,
        "accept",
        lambda *a, **k: called.append(1)
        or _acceptance_result(bc.direct_operation.APPLIED),
    )
    input_path = tmp_path / "handoff-accept.json"
    _write_json(
        input_path,
        _accept_input(operation=_operation_spec(effect_type="NOT_A_REAL_EFFECT")),
    )

    exit_code = bc.main(_accept_argv(run, input_path) + ["--json"])
    out = json.loads(capsys.readouterr().out)

    assert called == []
    assert exit_code == 4  # DirectOperationError defaults to status=CONFLICT
    assert out["status"] == "conflict"
    assert out["error_code"] == "DIRECT_OPERATION_EFFECT_UNKNOWN"


def test_handoff_accept_missing_required_field_exits_2(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    incomplete = _accept_input()
    del incomplete["result"]
    input_path = tmp_path / "handoff-accept.json"
    _write_json(input_path, incomplete)

    exit_code = bc.main(_accept_argv(run, input_path))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"
