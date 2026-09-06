"""AC-T13-001: ``start-run`` translates ``bootstrap.disposition`` correctly.

Regression coverage for defect #7: ``_handle_start_run`` used to test
``bootstrap.disposition in {"created", "resumed", "recovered"}`` -- three
literal strings ``state.bootstrap_run`` never actually produces (the sole
real success value is ``"active"``). That meant every genuinely successful
``start-run`` was misreported as ``status: "blocked"``. These tests exercise
the full CLI dispatch (``bc.main``) over a real bootstrap so the fix is
proven end to end, not just re-asserted against the source.

Scope note: the crash/resume ordering guarantee itself (root-pointer publish
before checkpoint 2 before "active", and non-redispatch on resume) is already
exhaustively proven at the ``coordinator_tracker``/``coordinator_state``
level by ``scripts/tests/beads_tracker/test_coordinator_tracker.py``.
``_handle_start_run`` exposes no ``crash_hook=`` injection point at all, so a
true crash/resume scenario cannot be driven through the CLI handler the way
that sibling suite drives it directly -- this suite's scope is narrower:
prove the CLI correctly wires into ``coordinator_tracker.start_run`` and
correctly translates the returned disposition into the CLI envelope.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import _common as common

bc = common.bc
safe_bd = common.safe_bd

ROOT_ISSUE = "scc-root"


def _prepare_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    hermes = repo / ".hermes"
    hermes.mkdir(mode=0o700)
    run_root = hermes / "beads-runs"
    run_root.mkdir(mode=0o700)
    for path in (hermes, run_root):
        path.chmod(0o700)
    return repo, run_root


def _write_input(
    path: Path, *, repo: Path, run_root: Path, **overrides: object
) -> None:
    payload: dict[str, object] = {
        "request_id": "request-0001-start-run-cli",
        "repository_root": str(repo),
        "git_common_dir": str(repo / ".git"),
        "workspace": str(repo),
        "run_root": str(run_root),
        "root_issue_id": ROOT_ISSUE,
        "scope_issue_ids": ["scc-lane-a"],
        "actor": "parent",
        "base_git_commit": "a" * 40,
        "authority_snapshot_sha256": "b" * 64,
        "workspace_identity_sha256": "c" * 64,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_start_run_happy_path_reports_success_for_active_disposition(
    tmp_path, monkeypatch, capsys
):
    repo, run_root = _prepare_repo(tmp_path)
    fake, _box = common.tracker_double(ROOT_ISSUE)
    monkeypatch.setattr(safe_bd, "run_profile", fake)

    input_path = tmp_path / "start-run.json"
    _write_input(input_path, repo=repo, run_root=run_root)

    exit_code = bc.main(
        ["start-run", "--input", str(input_path), "--actor", "parent", "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["status"] == "success"
    assert out["error_code"] is None
    assert out["blockers"] == []
    assert out["coverage_gaps"] == []
    # "creation" is reserved by the schema for issue-creation/dependency-edit
    # operations (an object shape, or null) -- start_run has no such shape
    # available, so a schema-conforming envelope must always report it as
    # null, success or not. The bootstrap disposition itself is fully
    # represented via status/error_code/coverage_gaps instead.
    assert out["creation"] is None
    run_id = out["run_id"]
    assert run_id.startswith("run-")
    # run_directory is not part of the schema-legal envelope (see above), but
    # the caller already knows run_root from their own --input and receives
    # run_id, so the actual bootstrap side effect can still be verified
    # directly on disk.
    assert (run_root / run_id).is_dir()
    # The root pointer must have actually been published exactly once as
    # part of reaching "active" -- not merely reported as such.
    assert fake.count("set_run_pointer") == 1


def test_start_run_second_call_with_same_root_issue_reports_held(
    tmp_path, monkeypatch, capsys
):
    """A second, independent bootstrap attempt for the same root issue (a
    fresh request_id, so a distinct run id is allocated -- not a resume of
    the first) must be reported as blocked, not silently re-reported as
    success.

    Traced directly from ``coordinator_state.bootstrap_run`` (lines
    1155-1438): ``_requests`` mapping is keyed by ``request_id``, so a new
    request_id always allocates a brand-new run directory rather than
    reusing the first one. Root-issue ownership is then inspected via
    ``beads_ownership.OwnershipStore.inspect`` against the *shared*
    ``run_root``: the first call already holds it (``current["status"] ==
    "active"`` -> disposition ``"held"``) under the *first* run's name. The
    second call's own run name never matches that held record's run_id, so
    it fails the "already mine, already active" fast-path (line 1228) and
    also isn't "unheld"/"released" (line 1325), so it falls through to the
    plain pass-through branch at line 1437-1438:
    ``return BootstrapResult(inspected.disposition, run.name, run, None, None)``
    -- i.e. disposition ``"held"``, not ``"conflict"`` (``"conflict"`` is a
    distinct disposition, only returned when ``owner_store.acquire`` itself
    fails at line 1420-1421, which cannot happen here since this run never
    attempts to acquire -- it only inspects).
    """
    repo, run_root = _prepare_repo(tmp_path)
    fake, _box = common.tracker_double(ROOT_ISSUE)
    monkeypatch.setattr(safe_bd, "run_profile", fake)

    first_input = tmp_path / "start-run-1.json"
    _write_input(
        first_input, repo=repo, run_root=run_root, request_id="request-0001-first-call"
    )
    first_exit = bc.main(
        ["start-run", "--input", str(first_input), "--actor", "parent", "--json"]
    )
    capsys.readouterr()
    assert first_exit == 0

    second_input = tmp_path / "start-run-2.json"
    _write_input(
        second_input,
        repo=repo,
        run_root=run_root,
        request_id="request-0002-second-call",
    )
    second_exit = bc.main(
        ["start-run", "--input", str(second_input), "--actor", "parent", "--json"]
    )
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "blocked"
    assert out["error_code"] == "HELD"
    assert out["creation"] is None
    assert out["blockers"] == ["HELD"]
    assert out["coverage_gaps"] != []
    assert second_exit != 0
