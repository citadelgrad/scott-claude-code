"""AC-T13-003: malformed/missing/invalid ``--input`` and exception-to-exit
mapping across the CLI's ``main()`` dispatch tiers.

Scope note: this file proves the *dispatcher's* three exception tiers in
``main()`` -- ``_CliInputError`` (exit 2, ``cli_error_envelope``),
``_INTEGRATION_ERRORS + _DIRECT_ERRORS + CoordinatorFrontError`` (status- and
code-derived exit), and the bare ``(KeyError, TypeError, ValueError)``
fallback (exit 2, ``INPUT_INVALID``) -- not the frozen modules' own
validation logic. Every subcommand shares the identical ``_load_json_input``/
``_require_fields`` helpers, so a representative subcommand (``start-run``,
which needs no pre-existing run directory) stands in for the whole family;
one additional test per remaining input-taking subcommand only confirms the
same helpers are actually being called there too.
"""

from __future__ import annotations

import json

import pytest

from . import _common as common

bc = common.bc


def test_load_json_input_missing_file_reports_input_file_unreadable(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(bc._CliInputError) as excinfo:
        bc._load_json_input(missing)
    assert excinfo.value.code == "INPUT_FILE_UNREADABLE"


def test_load_json_input_malformed_json_reports_input_file_not_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(bc._CliInputError) as excinfo:
        bc._load_json_input(bad)
    assert excinfo.value.code == "INPUT_FILE_NOT_JSON"


@pytest.mark.parametrize(
    "payload", ["[1, 2, 3]", '"just a string"', "42", "null", "true"]
)
def test_load_json_input_non_object_reports_input_file_not_object(tmp_path, payload):
    not_object = tmp_path / "not-object.json"
    not_object.write_text(payload, encoding="utf-8")
    with pytest.raises(bc._CliInputError) as excinfo:
        bc._load_json_input(not_object)
    assert excinfo.value.code == "INPUT_FILE_NOT_OBJECT"


def test_require_fields_reports_every_missing_field_sorted():
    with pytest.raises(bc._CliInputError) as excinfo:
        bc._require_fields({"b": 1}, ["b", "a", "c"])
    assert excinfo.value.code == "INPUT_MISSING_FIELDS:a,c"


def test_require_fields_passes_when_all_fields_present():
    bc._require_fields({"a": 1, "b": 2}, ["a", "b"])  # must not raise


# ---------------------------------------------------------------------------
# End-to-end through bc.main(): the same three failure shapes, but proven at
# the CLI boundary (exit code + stdout envelope), using start-run as the
# representative subcommand.
# ---------------------------------------------------------------------------


def test_cli_missing_input_file_exits_2_with_cli_error_envelope(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    exit_code = bc.main(
        ["start-run", "--input", str(missing), "--actor", "parent", "--json"]
    )
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out == {
        "schema_version": "beads.cli-error.v1",
        "status": "invalid",
        "error_code": "INPUT_FILE_UNREADABLE",
        "exit_code": 2,
    }


def test_cli_malformed_json_exits_2_with_cli_error_envelope(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    exit_code = bc.main(["start-run", "--input", str(bad), "--actor", "parent"])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_FILE_NOT_JSON"


def test_cli_non_object_json_exits_2_with_cli_error_envelope(tmp_path, capsys):
    array_input = tmp_path / "array.json"
    array_input.write_text("[]", encoding="utf-8")
    exit_code = bc.main(["start-run", "--input", str(array_input), "--actor", "parent"])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_FILE_NOT_OBJECT"


def test_cli_missing_required_fields_exits_2_with_input_missing_fields_code(
    tmp_path, capsys
):
    """``main()``'s ``_CliInputError`` tier renders only
    ``exc.code.split(":", 1)[0]`` through ``cli_error_envelope`` -- the
    ``:field,field`` detail ``_require_fields`` attaches never reaches the
    CLI's stdout envelope, because ``cli_error_envelope``'s ``_RENDER_CODE``
    pattern (``^[A-Z][A-Z0-9_]{0,127}$``) rejects colons and commas. The
    detailed code is still asserted directly against ``_require_fields``
    above; this test locks down what a real CLI caller actually sees.
    """
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(
        json.dumps({"request_id": "request-0001-x"}), encoding="utf-8"
    )
    exit_code = bc.main(["start-run", "--input", str(incomplete), "--actor", "parent"])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"


def test_gate_missing_hard_cap_key_falls_through_bare_keyerror_to_input_invalid(
    tmp_path, capsys
):
    """``gate``'s ``data["hard_cap"]`` is a bare dict subscript, never routed
    through ``_require_fields`` -- so a caller who omits ``hard_cap`` (or
    omits ``--input`` entirely, since ``_handle_gate`` falls back to ``{}``)
    trips the bare ``except (KeyError, TypeError, ValueError)`` tier in
    ``main()``, not the ``_CliInputError`` tier. This is a real, distinct
    edge case (see file summary) worth locking down explicitly.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    exit_code = bc.main(["gate", "--repository-root", str(repo)])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_INVALID"
    assert out["schema_version"] == "beads.cli-error.v1"


def test_gate_input_present_but_missing_hard_cap_also_input_invalid(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    input_path = tmp_path / "gate.json"
    input_path.write_text(json.dumps({"limit": 10}), encoding="utf-8")
    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path)]
    )
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_INVALID"


def test_status_for_exception_maps_direct_operation_error_variants():
    direct_operation = bc.direct_operation
    protected_action = bc.protected_action

    human = direct_operation.DirectOperationError("X")
    human.status = protected_action.HUMAN_ACTION_REQUIRED
    assert bc._status_for_exception(human) == "human_action_required"

    applied_but_error = direct_operation.DirectOperationError("X")
    applied_but_error.status = direct_operation.APPLIED
    assert bc._status_for_exception(applied_but_error) == "failed"

    # DirectOperationError.__init__ always sets .status (defaulting to
    # CONFLICT), so the genuinely-missing-attribute branch in
    # _status_for_exception's getattr(exc, "status", None) fallback can only
    # be exercised by bypassing __init__ entirely -- a defensive branch for
    # any future _DIRECT_ERRORS member that doesn't guarantee the attribute.
    # ``object.__new__`` is refused by CPython for exception subclasses
    # ("not safe, use DirectOperationError.__new__()") because
    # BaseException defines its own ``__new__``; calling the class's own
    # ``__new__`` directly (skipping ``__init__``) is the correct way to
    # bypass the constructor and get a genuinely status-less instance.
    no_status = direct_operation.DirectOperationError.__new__(
        direct_operation.DirectOperationError, "X"
    )
    assert getattr(no_status, "status", None) is None
    assert bc._status_for_exception(no_status) == "blocked"

    # The default status (no explicit kwarg) is CONFLICT.
    default_status = direct_operation.DirectOperationError("X")
    assert default_status.status == direct_operation.CONFLICT
    assert bc._status_for_exception(default_status) == "conflict"

    unknown = direct_operation.DirectOperationError("X")
    unknown.status = direct_operation.UNKNOWN
    assert bc._status_for_exception(unknown) == "inconclusive"


def test_status_for_exception_maps_integration_error_variants():
    coordinator_integration = bc.coordinator_integration
    refused = coordinator_integration.IntegrationError("X")
    refused.status = "refused"
    assert bc._status_for_exception(refused) == "blocked"

    conflict = coordinator_integration.IntegrationError("X")
    conflict.status = "conflict"
    assert bc._status_for_exception(conflict) == "conflict"


def test_status_for_exception_maps_coordinator_front_error_to_blocked():
    coordinator_front = bc.coordinator_front
    exc = coordinator_front.CoordinatorFrontError("X")
    assert bc._status_for_exception(exc) == "blocked"


def test_status_for_exception_falls_back_to_failed_for_anything_else():
    assert bc._status_for_exception(RuntimeError("surprise")) == "failed"


def test_main_maps_a_raised_integration_error_to_its_exit_code(
    tmp_path, monkeypatch, capsys
):
    """A real ``main()`` dispatch where the handler's frozen call raises a
    ``coordinator_integration.IntegrationError`` with ``status="conflict"``
    must exit 4 and print the ``{"status", "error_code"}`` shape -- proving
    the *middle* exception tier in ``main()``, using ``freeze`` (whose input
    is pure CLI flags, no ``--input`` file) as the vehicle.
    """
    run = common.make_run(tmp_path, issue_ids=["scc-lane-a"])

    def raise_conflict(*_args, **_kwargs):
        exc = bc.coordinator_integration.IntegrationError("LANE_FROZEN_CONFLICT")
        exc.status = "conflict"
        exc.code = "LANE_FROZEN_CONFLICT"
        raise exc

    monkeypatch.setattr(bc.coordinator_integration, "freeze_lane", raise_conflict)
    exit_code = bc.main(
        [
            "freeze",
            "--run-dir",
            str(run["run_directory"]),
            "--issue-id",
            "scc-lane-a",
            "--expected-result-sha256",
            "a" * 64,
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert out == {"status": "conflict", "error_code": "LANE_FROZEN_CONFLICT"}


def test_main_maps_a_bare_value_error_to_input_invalid(tmp_path, monkeypatch, capsys):
    """A handler-internal ``ValueError`` unrelated to ``_CliInputError`` (for
    instance a malformed ``scope_issue_ids`` entry deep in a frozen call)
    must still land on exit 2 / ``INPUT_INVALID`` via the bare
    ``except (KeyError, TypeError, ValueError)`` tier, proving that tier is
    reachable independent of the two more specific tiers above it.
    """
    run = common.make_run(tmp_path, issue_ids=["scc-lane-a"])

    def raise_value_error(*_args, **_kwargs):
        raise ValueError("not a _CliInputError, not a frozen error type")

    monkeypatch.setattr(bc.coordinator_integration, "verify_lane", raise_value_error)
    exit_code = bc.main(
        ["verify", "--run-dir", str(run["run_directory"]), "--issue-id", "scc-lane-a"]
    )
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_INVALID"
