"""AC-T13-002 (action/receipt variant matrix, lane-gate slice): the six
"evaluate_gate" subcommands -- ``freeze``, ``verify``, ``review``,
``review-combined``, ``build-candidate``, ``apply-candidate``.

All six dispatch through the single shared ``_integration_candidate(...)``
builder (see ``beads_coordinator.py`` lines ~443-481), which:

* always reports the schema's shared ``operation: "evaluate_gate"`` value,
  regardless of which of the six ad hoc ``label`` strings produced the
  candidate (regression coverage for defect #14 -- these six subcommands
  used to leak their internal label into the schema-facing ``operation``
  field, which is not a legal member of ``beads.operation-result.v1``'s
  16-value ``operation`` enum);
* maps the frozen verb's returned ``payload["status"]`` through
  ``_INTEGRATION_STATUS_MAP`` (``"refused" -> "blocked"``,
  ``"conflict" -> "conflict"``, ``"success" -> "success"``, anything else
  defaulting to ``"blocked"``);
* defaults ``error_code`` to ``payload["status"].upper()`` when the frozen
  verb's payload omits an explicit ``error_code``, but honors an explicit
  one when present;
* populates ``blockers`` only for the ``"blocked"`` status, never for
  ``"conflict"`` (only ``_map_direct_status``'s ``"conflict"`` classification
  is exempt from ever getting a ``blockers`` entry here -- that is a
  deliberate, schema-conforming asymmetry in this handler family, not a gap).

This file drives each subcommand through a real ``bc.main()`` dispatch over a
real run directory (``common.make_run``), monkeypatching only the one frozen
``coordinator_integration`` verb function each subcommand calls -- proving the
CLI's own envelope-building and exit-code logic, not re-proving
``coordinator_integration``'s own internal gate logic (already covered by
that module's own test suite).
"""

from __future__ import annotations

import json

import pytest

from . import _common as common

bc = common.bc

ISSUE_ID = "scc-lane-a"


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _argv_freeze(run, tmp_path):
    return [
        "freeze",
        "--run-dir",
        str(run["run_directory"]),
        "--issue-id",
        ISSUE_ID,
        "--expected-result-sha256",
        "a" * 64,
    ]


def _argv_verify(run, tmp_path):
    return ["verify", "--run-dir", str(run["run_directory"]), "--issue-id", ISSUE_ID]


def _argv_review(run, tmp_path):
    return [
        "review",
        "--run-dir",
        str(run["run_directory"]),
        "--issue-id",
        ISSUE_ID,
        "--review-path",
        str(tmp_path / "review.json"),
    ]


def _argv_review_combined(run, tmp_path):
    return [
        "review-combined",
        "--run-dir",
        str(run["run_directory"]),
        "--review-path",
        str(tmp_path / "combined-review.json"),
    ]


def _argv_build_candidate(run, tmp_path):
    input_path = tmp_path / "build-candidate.json"
    _write_json(input_path, {"lane_freeze_sha256s": ["a" * 64, "b" * 64]})
    return [
        "build-candidate",
        "--run-dir",
        str(run["run_directory"]),
        "--input",
        str(input_path),
    ]


def _argv_apply_candidate(run, tmp_path):
    return [
        "apply-candidate",
        "--run-dir",
        str(run["run_directory"]),
        "--candidate-id",
        "candidate-0001",
    ]


# (subcommand, attribute on bc.coordinator_integration to monkeypatch,
#  argv builder, expected issue_ids in the envelope)
LANE_GATE_CASES = [
    ("freeze", "freeze_lane", _argv_freeze, [ISSUE_ID]),
    ("verify", "verify_lane", _argv_verify, [ISSUE_ID]),
    ("review", "record_review", _argv_review, [ISSUE_ID]),
    ("review-combined", "record_combined_review", _argv_review_combined, []),
    ("build-candidate", "build_candidate", _argv_build_candidate, []),
    ("apply-candidate", "apply_candidate", _argv_apply_candidate, []),
]


@pytest.mark.parametrize(
    ("subcommand", "attr", "argv_builder", "expected_issue_ids"),
    LANE_GATE_CASES,
    ids=[case[0] for case in LANE_GATE_CASES],
)
def test_success_payload_reports_success_and_shared_evaluate_gate_operation(
    tmp_path, monkeypatch, capsys, subcommand, attr, argv_builder, expected_issue_ids
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_integration, attr, lambda *a, **k: {"status": "success"}
    )

    exit_code = bc.main(argv_builder(run, tmp_path) + ["--json"])
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    # Defect #14 regression: every one of the six subcommands must report the
    # single shared enum value, never its own internal label.
    assert out["operation"] == "evaluate_gate"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["blockers"] == []
    assert out["coverage_gaps"] == []
    assert out["issue_ids"] == expected_issue_ids


@pytest.mark.parametrize(
    ("subcommand", "attr", "argv_builder", "expected_issue_ids"),
    LANE_GATE_CASES,
    ids=[case[0] for case in LANE_GATE_CASES],
)
def test_refused_payload_without_explicit_error_code_defaults_to_uppercased_status(
    tmp_path, monkeypatch, capsys, subcommand, attr, argv_builder, expected_issue_ids
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_integration, attr, lambda *a, **k: {"status": "refused"}
    )

    exit_code = bc.main(argv_builder(run, tmp_path) + ["--json"])
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["operation"] == "evaluate_gate"
    assert out["status"] == "blocked"
    assert out["error_code"] == "REFUSED"
    assert out["blockers"] == ["REFUSED"]
    assert out["coverage_gaps"] != []
    assert out["safe_next_action"] is not None


@pytest.mark.parametrize(
    ("subcommand", "attr", "argv_builder", "expected_issue_ids"),
    LANE_GATE_CASES,
    ids=[case[0] for case in LANE_GATE_CASES],
)
def test_refused_payload_with_explicit_error_code_is_honored_verbatim(
    tmp_path, monkeypatch, capsys, subcommand, attr, argv_builder, expected_issue_ids
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_integration,
        attr,
        lambda *a, **k: {"status": "refused", "error_code": "LANE_ARTIFACT_MISSING"},
    )

    exit_code = bc.main(argv_builder(run, tmp_path) + ["--json"])
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "LANE_ARTIFACT_MISSING"
    assert out["blockers"] == ["LANE_ARTIFACT_MISSING"]


@pytest.mark.parametrize(
    ("subcommand", "attr", "argv_builder", "expected_issue_ids"),
    LANE_GATE_CASES,
    ids=[case[0] for case in LANE_GATE_CASES],
)
def test_conflict_payload_reports_conflict_exit_4_with_no_blockers_entry(
    tmp_path, monkeypatch, capsys, subcommand, attr, argv_builder, expected_issue_ids
):
    """Unlike "blocked", "conflict" never populates ``blockers`` here -- that
    field is only ever filled by ``_integration_candidate`` for its
    ``"blocked"`` branch. This is a deliberate asymmetry (a lane conflict is
    reported via ``status``/``error_code``/``coverage_gaps`` alone), not an
    omission -- locked down explicitly so a future edit cannot "fix" it into
    an inconsistency with the freeze/verify sibling suites' own expectations.
    """
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    monkeypatch.setattr(
        bc.coordinator_integration,
        attr,
        lambda *a, **k: {"status": "conflict", "error_code": "LANE_FROZEN_CONFLICT"},
    )

    exit_code = bc.main(argv_builder(run, tmp_path) + ["--json"])
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert out["operation"] == "evaluate_gate"
    assert out["status"] == "conflict"
    assert out["error_code"] == "LANE_FROZEN_CONFLICT"
    assert out["blockers"] == []
    assert out["coverage_gaps"] != []


def test_freeze_passes_through_expected_result_sha256_to_the_frozen_verb(
    tmp_path, monkeypatch, capsys
):
    """The CLI's only job for ``freeze`` is to forward ``--issue-id`` and
    ``--expected-result-sha256`` verbatim -- prove the actual values reach
    ``coordinator_integration.freeze_lane`` unmodified.
    """
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    captured = {}

    def fake_freeze_lane(context, issue_id, *, expected_result_sha256):
        captured["issue_id"] = issue_id
        captured["expected_result_sha256"] = expected_result_sha256
        return {"status": "success"}

    monkeypatch.setattr(bc.coordinator_integration, "freeze_lane", fake_freeze_lane)
    exit_code = bc.main(
        [
            "freeze",
            "--run-dir",
            str(run["run_directory"]),
            "--issue-id",
            ISSUE_ID,
            "--expected-result-sha256",
            "f" * 64,
        ]
    )
    assert exit_code == 0
    assert captured == {"issue_id": ISSUE_ID, "expected_result_sha256": "f" * 64}


def test_build_candidate_passes_through_lane_freeze_sha256s_and_expected_predecessor(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    captured = {}

    def fake_build_candidate(context, *, lane_freeze_sha256s, expected_predecessor):
        captured["lane_freeze_sha256s"] = lane_freeze_sha256s
        captured["expected_predecessor"] = expected_predecessor
        return {"status": "success"}

    monkeypatch.setattr(
        bc.coordinator_integration, "build_candidate", fake_build_candidate
    )
    input_path = tmp_path / "build-candidate.json"
    _write_json(
        input_path,
        {"lane_freeze_sha256s": ["a" * 64, "b" * 64], "expected_predecessor": "c" * 64},
    )
    exit_code = bc.main(
        [
            "build-candidate",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
        ]
    )
    assert exit_code == 0
    assert captured == {
        "lane_freeze_sha256s": ["a" * 64, "b" * 64],
        "expected_predecessor": "c" * 64,
    }


def test_build_candidate_requires_lane_freeze_sha256s_field(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    input_path = tmp_path / "build-candidate.json"
    _write_json(input_path, {"expected_predecessor": "c" * 64})
    exit_code = bc.main(
        [
            "build-candidate",
            "--run-dir",
            str(run["run_directory"]),
            "--input",
            str(input_path),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"


def test_apply_candidate_passes_through_candidate_id_and_optional_expected_predecessor(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[ISSUE_ID])
    captured = {}

    def fake_apply_candidate(context, candidate_id, *, expected_predecessor):
        captured["candidate_id"] = candidate_id
        captured["expected_predecessor"] = expected_predecessor
        return {"status": "success"}

    monkeypatch.setattr(
        bc.coordinator_integration, "apply_candidate", fake_apply_candidate
    )
    exit_code = bc.main(
        [
            "apply-candidate",
            "--run-dir",
            str(run["run_directory"]),
            "--candidate-id",
            "candidate-0002",
            "--expected-predecessor",
            "d" * 64,
        ]
    )
    assert exit_code == 0
    assert captured == {
        "candidate_id": "candidate-0002",
        "expected_predecessor": "d" * 64,
    }
