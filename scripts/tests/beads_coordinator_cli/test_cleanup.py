"""AC-T13-005 (cleanup retention/refusal) plus the "Required verification"
item of the same name: the ``cleanup`` subcommand's own inline refusal
matrix.

Unlike every other handler in ``beads_coordinator.py``, ``_handle_cleanup``
(see lines 838-891) does not delegate its decision to a single frozen verb
function -- it aggregates refusals directly from four independent frozen
sources (``reconcile_run.status``, per-issue
``beads_ownership.OwnershipStore.inspect_readonly``,
``state.validate_owner_directory``, and its own ``_run_age_seconds`` helper)
and only calls ``shutil.rmtree`` when none of them object. This file drives
that aggregation for real through ``bc.main()``, monkeypatching only the
three frozen boundary calls (``reconcile_run.status``,
``OwnershipStore.inspect_readonly``, ``state.validate_owner_directory``) --
each already has its own exhaustive test suite for *why* it returns what it
returns -- while letting ``_run_age_seconds`` run for real against a
manifest's actual ``created_at``, since that arithmetic is
``beads_coordinator.py``'s own code, not a frozen predecessor's.
"""

from __future__ import annotations

import json

from . import _common as common

bc = common.bc
reconcile_run = common.bc.reconcile_run
beads_ownership = common.bc.beads_ownership
state = common.bc.state

ROOT_ISSUE = "scc-root"
LANE_A = "scc-lane-a"
LANE_B = "scc-lane-b"


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _plan(disposition: str = "consistent") -> reconcile_run.ReconciliationPlan:
    return reconcile_run.ReconciliationPlan(
        disposition=disposition,
        run_id="run-0000000000000000",
        accepted_generation=1,
        journal_status="clean",
        ownership_status="clean",
        next_safe_action="none",
    )


def _ownership_result(disposition: str) -> beads_ownership.OwnershipResult:
    return beads_ownership.OwnershipResult(
        disposition=disposition, record=None, history=()
    )


def _patch_consistent(
    monkeypatch, *, dispositions: dict[str, str] | None = None
) -> None:
    """Patch two frozen boundaries so cleanup would succeed by default:
    reconciliation and per-issue ownership disposition (default: "released"
    for every scoped issue id). ``state.validate_owner_directory`` is
    deliberately left real here -- ``common.make_run`` already builds a
    correctly-permissioned owner directory, and
    ``beads_ownership.OwnershipStore.__init__`` (constructed unconditionally
    by ``_handle_cleanup`` before any refusal is even checked) calls that
    same function internally with a ``root=`` kwarg; a blanket monkeypatch
    would break that internal call, not just ``_handle_cleanup``'s own.
    """
    dispositions = dispositions or {}
    monkeypatch.setattr(reconcile_run, "status", lambda run_dir: _plan("consistent"))
    monkeypatch.setattr(
        beads_ownership.OwnershipStore,
        "inspect_readonly",
        lambda self, issue_id: _ownership_result(
            dispositions.get(issue_id, "released")
        ),
    )


def _patch_validate_owner_directory_call_only(monkeypatch, replacement):
    """Intercept only ``_handle_cleanup``'s own call to
    ``state.validate_owner_directory(args.run_dir)`` (no ``root`` kwarg),
    while delegating every other call to the real implementation.

    Three different call sites reach ``validate_owner_directory`` with no
    ``root=`` keyword during a single ``cleanup`` dispatch, and they are not
    distinguishable by signature alone -- only by fixed call order:

    1. ``state.load_run_manifest(args.run_dir)`` -- the very first line of
       ``_handle_cleanup`` -- calls ``validate_owner_file(..., root=run_directory)``,
       which itself calls the *module-level* ``validate_owner_directory(Path(root))``
       with no keyword. Because that call resolves through
       ``coordinator_state``'s own global namespace, monkeypatching
       ``state.validate_owner_directory`` intercepts this internal call too,
       not just ``_handle_cleanup``'s own. This call must stay real, or
       ``load_run_manifest`` itself fails before any refusal is even
       evaluated, and the resulting exception escapes ``_handle_cleanup``
       uncaught (its local ``try/except state.StateError`` only wraps the
       *third*, later, explicit call), landing in ``main()``'s outer
       exception tiers instead of the cleanup-specific refusal path under
       test.
    2. ``beads_ownership.OwnershipStore.__init__``'s own
       ``self.run_root = state.validate_owner_directory(Path(run_root))``
       (``beads_ownership.py`` line 72) -- also called with no keyword. This
       must stay real too, or constructing ``ownership =
       beads_ownership.OwnershipStore(args.run_dir.parent)`` itself raises
       before ``_handle_cleanup`` even builds its envelope or reaches the
       per-issue ownership loop. (``OwnershipStore.__init__``'s *second*
       internal call, ``state.ensure_owner_directory(self.root,
       root=self.run_root)``, passes ``root=`` explicitly and is already
       routed to the real implementation below regardless of ordering.)
    3. ``_handle_cleanup``'s own ``resolved_root = state.validate_owner_directory(args.run_dir)``
       call, further down, guarded by its local ``try/except`` -- this is the
       one each test here actually wants to replace.

    The three no-keyword calls always occur in the fixed order above within
    one ``_handle_cleanup`` invocation, so the first two are delegated to the
    real implementation and only the third (and any further ones, though
    none occur in practice) is replaced.
    """
    original = state.validate_owner_directory
    no_kwarg_calls = {"count": 0}

    def patched(path, *, root=None):
        if root is not None:
            return original(path, root=root)
        no_kwarg_calls["count"] += 1
        if no_kwarg_calls["count"] <= 2:
            # 1: validate_owner_file's internal call, via load_run_manifest.
            # 2: OwnershipStore.__init__'s own self.run_root call.
            # Both must stay real.
            return original(path)
        return replacement(path)

    monkeypatch.setattr(state, "validate_owner_directory", patched)


def _argv(run, input_path, *, retention_seconds=0, dry_run=False):
    argv = [
        "cleanup",
        "--run-dir",
        str(run["run_directory"]),
        "--input",
        str(input_path),
        "--retention-seconds",
        str(retention_seconds),
        "--json",
    ]
    if dry_run:
        argv.insert(-1, "--dry-run")
    return argv


def test_cleanup_happy_path_dry_run_reports_success_without_deleting(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["operation"] == "cleanup_run"
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["blockers"] == []
    assert out["issue_ids"] == [ROOT_ISSUE]
    assert run["run_directory"].is_dir()


def test_cleanup_without_dry_run_actually_deletes_the_run_directory(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=False))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["status"] == "success"
    assert not run["run_directory"].exists()


def test_cleanup_refuses_when_run_is_not_reconciled(tmp_path, monkeypatch, capsys):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch)
    monkeypatch.setattr(reconcile_run, "status", lambda run_dir: _plan("diverged"))
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "RUN_NOT_RECONCILED"
    assert out["blockers"] == ["RUN_NOT_RECONCILED"]
    assert out["safe_next_action"] is not None
    assert run["run_directory"].is_dir()


def test_cleanup_refuses_on_held_ownership_for_a_scoped_issue(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch, dispositions={LANE_A: "held"})
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "OWNERSHIP_HELD"
    assert out["blockers"] == ["OWNERSHIP_HELD"]


def test_cleanup_refuses_on_unknown_ownership_for_a_scoped_issue(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch, dispositions={LANE_A: "unknown"})
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "OWNERSHIP_UNKNOWN"


def test_cleanup_refuses_on_conflicting_ownership_for_a_scoped_issue(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch, dispositions={LANE_A: "conflict"})
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "OWNERSHIP_CONFLICT"


def test_cleanup_released_ownership_is_not_a_refusal(tmp_path, monkeypatch, capsys):
    """Only held/unknown/conflict are refusals -- "released" (and any other
    disposition) must not block cleanup.
    """
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch, dispositions={LANE_A: "released"})
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["status"] == "success"


def test_cleanup_translates_a_state_error_from_validate_owner_directory(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch)

    def raise_state_error(run_dir):
        raise state.StateError("OWNER_DIRECTORY_MODE_INVALID")

    _patch_validate_owner_directory_call_only(monkeypatch, raise_state_error)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "OWNER_DIRECTORY_MODE_INVALID"
    assert out["blockers"] == ["OWNER_DIRECTORY_MODE_INVALID"]
    # The directory must be left untouched -- no rmtree was ever attempted.
    assert run["run_directory"].is_dir()


def test_cleanup_refuses_a_target_that_resolves_to_a_filesystem_root(
    tmp_path, monkeypatch, capsys
):
    import pathlib

    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch)
    root_like = pathlib.Path(pathlib.Path(tmp_path).anchor)
    _patch_validate_owner_directory_call_only(monkeypatch, lambda run_dir: root_like)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "CLEANUP_TARGET_TOO_BROAD"
    assert run["run_directory"].is_dir()


def test_cleanup_refuses_when_retention_has_not_elapsed(tmp_path, monkeypatch, capsys):
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE, created_at=common.now()
    )
    _patch_consistent(monkeypatch)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=10_000, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["status"] == "blocked"
    assert out["error_code"] == "CLEANUP_RETENTION_NOT_ELAPSED"
    assert run["run_directory"].is_dir()


def test_cleanup_allows_deletion_once_retention_has_elapsed(
    tmp_path, monkeypatch, capsys
):
    """A manifest ``created_at`` far in the past clears the retention
    refusal for real -- ``_run_age_seconds`` is exercised unmocked.
    """
    run = common.make_run(
        tmp_path,
        issue_ids=[LANE_A],
        root_issue_id=ROOT_ISSUE,
        created_at="2000-01-01T00:00:00.000000Z",
    )
    _patch_consistent(monkeypatch)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=10_000, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["status"] == "success"


def test_cleanup_reports_the_first_refusal_in_source_order_and_accumulates_all_blockers(
    tmp_path, monkeypatch, capsys
):
    """Ordering per ``_handle_cleanup``'s own source: reconciliation, then
    per-issue ownership, then owner-directory validation, then retention.
    With both a reconciliation refusal and an ownership refusal present,
    ``error_code``/``safe_next_action`` must be the *first* one
    (``RUN_NOT_RECONCILED``), while ``blockers`` accumulates both codes.
    """
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    _patch_consistent(monkeypatch, dispositions={LANE_A: "held"})
    monkeypatch.setattr(reconcile_run, "status", lambda run_dir: _plan("diverged"))
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["error_code"] == "RUN_NOT_RECONCILED"
    assert out["blockers"] == ["RUN_NOT_RECONCILED", "OWNERSHIP_HELD"]
    assert len(out["coverage_gaps"]) == 2


def test_cleanup_checks_every_scoped_issue_id_not_just_the_first(
    tmp_path, monkeypatch, capsys
):
    run = common.make_run(
        tmp_path, issue_ids=[LANE_A, LANE_B], root_issue_id=ROOT_ISSUE
    )
    _patch_consistent(monkeypatch, dispositions={LANE_A: "released", LANE_B: "held"})
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {"scope_issue_ids": [LANE_A, LANE_B]})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["error_code"] == "OWNERSHIP_HELD"
    assert out["blockers"] == ["OWNERSHIP_HELD"]


def test_cleanup_missing_required_field_exits_2(tmp_path, capsys):
    run = common.make_run(tmp_path, issue_ids=[LANE_A], root_issue_id=ROOT_ISSUE)
    input_path = tmp_path / "cleanup.json"
    _write_json(input_path, {})

    exit_code = bc.main(_argv(run, input_path, retention_seconds=0, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert out["error_code"] == "INPUT_MISSING_FIELDS"
