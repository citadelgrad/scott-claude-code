"""AC-T13-002 (action/receipt variant matrix, gate slice): the ``gate``
subcommand's wrapping of ``coordinator_front.capture_and_build_front``.

``_handle_gate`` (see ``beads_coordinator.py`` lines 607-654) is the one
subcommand with no run directory and no persisted manifest -- it forwards
``--repository-root`` plus a handful of optional ``--input`` fields straight
through to ``capture_and_build_front`` and builds a synthetic envelope
"manifest" from caller-supplied (or generated/defaulted) fields, since there
is no real run manifest to read ``request_id``/``root_issue_id`` back from.
This file drives that forwarding and the success/refused envelope mapping
end to end through a real ``bc.main()`` dispatch, monkeypatching only
``coordinator_front.capture_and_build_front`` -- proving the CLI's own
plumbing, not re-proving ``build_ready_front``'s own selection/refusal logic
(already covered by its own test suite).

The bare-``KeyError``-on-missing-``hard_cap`` edge case (no ``--input`` at
all, or an ``--input`` file lacking ``hard_cap``) is already covered by
``test_input_validation.py``'s two ``test_gate_*`` tests -- not duplicated
here.
"""

from __future__ import annotations

import json

from . import _common as common

bc = common.bc


def _front(*, selected=(), refusal=None, excluded=(), diagnostics=None):
    return bc.coordinator_front.build_ready_front.FrontResult(
        schema_version=bc.coordinator_front.SCHEMA_VERSION,
        selected=tuple(selected),
        excluded=tuple(excluded),
        refusal=refusal,
        diagnostics=diagnostics or {},
    )


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_happy_path_reports_success_with_selected_issue_ids(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        bc.coordinator_front,
        "capture_and_build_front",
        lambda **k: ({}, _front(selected=("scc-a", "scc-b"))),
    )
    input_path = tmp_path / "gate.json"
    _write_json(input_path, {"hard_cap": 5})

    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path), "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "front"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["blockers"] == []
    assert out["issue_ids"] == ["scc-a", "scc-b"]


def test_refusal_reports_blocked_with_blockers_and_coverage_gaps(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        bc.coordinator_front,
        "capture_and_build_front",
        lambda **k: ({}, _front(selected=(), refusal="SNAPSHOT_TOO_STALE")),
    )
    input_path = tmp_path / "gate.json"
    _write_json(input_path, {"hard_cap": 5})

    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path), "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "SNAPSHOT_TOO_STALE"
    assert out["blockers"] == ["SNAPSHOT_TOO_STALE"]
    assert out["coverage_gaps"] != []
    assert out["safe_next_action"] is not None


def test_forwards_repository_root_and_all_optional_input_fields_verbatim(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = {}

    def fake_capture_and_build_front(**kwargs):
        captured.update(kwargs)
        return {}, _front(selected=())

    monkeypatch.setattr(
        bc.coordinator_front, "capture_and_build_front", fake_capture_and_build_front
    )
    input_path = tmp_path / "gate.json"
    _write_json(
        input_path,
        {
            "root_issue_id": "scc-root",
            "limit": 42,
            "hard_cap": 7,
            "requested_cap": 3,
        },
    )

    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path), "--json"]
    )

    assert exit_code == 0
    assert captured["repository_root"] == repo
    assert captured["root_issue_id"] == "scc-root"
    assert captured["limit"] == 42
    assert captured["hard_cap"] == 7
    assert captured["requested_cap"] == 3


def test_limit_defaults_to_100_when_omitted(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = {}

    def fake_capture_and_build_front(**kwargs):
        captured.update(kwargs)
        return {}, _front(selected=())

    monkeypatch.setattr(
        bc.coordinator_front, "capture_and_build_front", fake_capture_and_build_front
    )
    input_path = tmp_path / "gate.json"
    _write_json(input_path, {"hard_cap": 5})

    bc.main(["gate", "--repository-root", str(repo), "--input", str(input_path)])

    assert captured["limit"] == 100
    assert captured["root_issue_id"] is None
    assert captured["requested_cap"] is None


def test_explicit_request_id_is_honored_verbatim(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        bc.coordinator_front,
        "capture_and_build_front",
        lambda **k: ({}, _front(selected=())),
    )
    input_path = tmp_path / "gate.json"
    _write_json(input_path, {"hard_cap": 5, "request_id": "gate-request-0001-explicit"})

    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path), "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["request_id"] == "gate-request-0001-explicit"


def test_omitted_request_id_falls_back_to_a_generated_gate_prefixed_id(
    tmp_path, monkeypatch, capsys
):
    """The schema requires ``request_id`` to be a 16-128 char printable-ASCII
    string with no null variant, so a caller who omits it entirely still
    gets a schema-conforming fallback, not a validation failure.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        bc.coordinator_front,
        "capture_and_build_front",
        lambda **k: ({}, _front(selected=())),
    )
    input_path = tmp_path / "gate.json"
    _write_json(input_path, {"hard_cap": 5})

    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path), "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["request_id"].startswith("gate-")
    assert 16 <= len(out["request_id"]) <= 128


def test_omitted_root_issue_id_falls_back_to_the_literal_unknown(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        bc.coordinator_front,
        "capture_and_build_front",
        lambda **k: ({}, _front(selected=())),
    )
    input_path = tmp_path / "gate.json"
    _write_json(input_path, {"hard_cap": 5})

    exit_code = bc.main(
        ["gate", "--repository-root", str(repo), "--input", str(input_path), "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["root_issue_id"] == "unknown"


def test_run_id_is_null_when_omitted_and_forwarded_when_supplied(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        bc.coordinator_front,
        "capture_and_build_front",
        lambda **k: ({}, _front(selected=())),
    )

    no_run_id_input = tmp_path / "gate-no-run-id.json"
    _write_json(no_run_id_input, {"hard_cap": 5})
    exit_code = bc.main(
        [
            "gate",
            "--repository-root",
            str(repo),
            "--input",
            str(no_run_id_input),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["run_id"] is None

    # The schema's run_id grammar is strict (see common.make_run_id), so an
    # arbitrary string like "run-example" is itself schema-invalid and would
    # trip the bare-ValueError tier instead of proving this fallback -- use a
    # grammar-conforming id.
    valid_run_id = common.make_run_id()
    with_run_id_input = tmp_path / "gate-with-run-id.json"
    _write_json(with_run_id_input, {"hard_cap": 5, "run_id": valid_run_id})
    bc.main(
        [
            "gate",
            "--repository-root",
            str(repo),
            "--input",
            str(with_run_id_input),
            "--json",
        ]
    )
    out2 = json.loads(capsys.readouterr().out)
    assert out2["run_id"] == valid_run_id
