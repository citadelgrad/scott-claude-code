"""Tests for build_candidate / record_combined_review / apply_candidate (t10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import _common as common

RUN_ID = "run-0123456789abcdef-20260904T010000.000000Z-AAAAAAAC"


def _setup(tmp_path: Path, lanes: list[dict], *, freeze_lanes: bool = True):
    """lanes: [{issue_id, file, content, allowed}] -> frozen+verified fixture.

    ``freeze_lanes=False`` stops after the attempt import, leaving each lane's
    artifact state un-``packaged``. That is the only way to reach the guard
    AC-T10-001 is about: mutable worker state that never acquired an immutable
    transfer identity.
    """
    m = common.modules()
    factory = common.factory()
    repo, lane_worktree, head = _clean_repo(factory, tmp_path)
    run = common.make_run(m, repo, RUN_ID, [item["issue_id"] for item in lanes])
    results = {}
    for index, spec in enumerate(lanes):
        worktree = lane_worktree if index == 0 else _add_worktree(repo, index)
        outbox = worktree / "outbox"
        epoch = run["epochs"][spec["issue_id"]]
        packet = common.lane_packet(
            factory,
            tmp_path,
            issue_id=spec["issue_id"],
            run_id=RUN_ID,
            worktree=worktree,
            outbox=outbox,
            head=head,
            allowed=spec.get("allowed", ["src/**"]),
        )
        digest, result_sha = common.import_attempt(
            m,
            run["run_directory"],
            issue_id=spec["issue_id"],
            epoch=epoch,
            worktree=worktree,
            outbox=outbox,
            head=head,
            changed_file=spec["file"],
            file_content=spec["content"],
            packet=packet,
        )
        entry = common.issue_entry(
            m, epoch=epoch, result_sha=result_sha, packet_sha=digest
        )
        results[spec["issue_id"]] = {
            "entry": entry,
            "epoch": epoch,
            "result_sha": result_sha,
            "packet": packet,
            "worktree": worktree,
            "outbox": outbox,
        }
    run["finish"]({issue_id: item["entry"] for issue_id, item in results.items()})
    context = m.coordinator_integration.open_run(run["run_directory"])
    freeze_shas = {}
    for issue_id, item in results.items() if freeze_lanes else ():
        frozen = m.coordinator_integration.freeze_lane(
            context, issue_id, expected_result_sha256=item["result_sha"]
        )
        assert frozen["status"] == "success"
        verified = m.coordinator_integration.verify_lane(context, issue_id)
        if verified["status"] == "partial":
            review = common.reviewer_record(
                run_id=RUN_ID,
                target_kind="lane_freeze",
                target_sha256=frozen["freeze_sha256"],
                reviewer_id=f"reviewer-{issue_id}",
                implementer_ids=[f"implementer-{issue_id}"],
            )
            path = common.write_review(m, run["run_directory"], review)
            outcome = m.coordinator_integration.record_review(context, issue_id, path)
            assert outcome["status"] == "success"
        freeze_shas[issue_id] = frozen["freeze_sha256"]
    return {
        "m": m,
        "repo": repo,
        "head": head,
        "run": run,
        "run_directory": run["run_directory"],
        "context": context,
        "results": results,
        "freeze_shas": freeze_shas,
    }


def _clean_repo(factory, tmp_path: Path):
    """Base repo whose known run artifacts are gitignored (clean primary)."""
    repo, lane_worktree, head = factory._git_lane(tmp_path)
    (repo / ".gitignore").write_text(
        ".hermes/\nlane/\nlane-*/\nsensitive-values.json\nsnapshot.json\n"
    )
    common.run_git(repo, "add", ".gitignore")
    common.run_git(repo, "commit", "-m", "ignore run artifacts")
    common.run_git(lane_worktree, "merge", "--ff-only", "main")
    head = common.run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return repo, lane_worktree, head


def _add_worktree(repo: Path, index: int) -> Path:
    import subprocess

    worktree = repo / f"lane-{index}"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", f"lane-{index}"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return worktree


def test_single_lane_build_and_apply(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    freeze_shas = list(fixture["freeze_shas"].values())

    built = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=freeze_shas
    )
    assert built["status"] == "success"
    assert built["review_required"] is False

    # Deterministic ordering: same lane set in any order, identical candidate.
    built_again = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=list(reversed(freeze_shas))
    )
    assert built_again["status"] == "success"
    assert built_again["candidate_tree_sha256"] == built["candidate_tree_sha256"]

    applied = m.coordinator_integration.apply_candidate(
        context,
        built["candidate_id"],
        expected_predecessor=fixture["head"],
    )
    assert applied["status"] == "success"
    # Exact readback of the primary after apply.
    head_tree = (
        m.coordinator_integration._git(fixture["repo"], "rev-parse", "HEAD^{tree}")
        .decode()
        .strip()
    )
    assert head_tree == built["candidate_tree_sha256"]
    status = m.coordinator_integration._git(
        fixture["repo"], "status", "--porcelain"
    ).decode()
    assert status.strip() == ""
    current = context.checkpoints.current(rebuild_pointer=True)
    assert (
        current.value["issues"]["scc-a"]["integration"]["state"] == "primary_integrated"
    )
    # The applied change is present in the primary checkout.
    assert (fixture["repo"] / "src" / "alpha.py").read_text() == "a = 1\n"


def test_apply_refuses_wrong_predecessor_and_dirty_primary(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    built = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )
    with pytest.raises(m.coordinator_integration.IntegrationError) as wrong:
        m.coordinator_integration.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor="0" * 40,
        )
    assert wrong.value.code in {
        "CANDIDATE_PREDECESSOR_MISMATCH",
        "PRIMARY_PREDECESSOR_MISMATCH",
    }
    # Primary untouched.
    assert not (fixture["repo"] / "src" / "alpha.py").exists()

    # A dirty primary checkout refuses application.
    (fixture["repo"] / "uncommitted.txt").write_text("dirty\n")
    with pytest.raises(m.coordinator_integration.IntegrationError) as dirty:
        m.coordinator_integration.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor=fixture["head"],
        )
    assert dirty.value.code == "PRIMARY_DIRTY"
    (fixture["repo"] / "uncommitted.txt").unlink()


def test_unpackaged_lane_is_never_verified_or_integrated(tmp_path: Path) -> None:
    """AC-T10-001: mutable worker state with no immutable transfer identity.

    Every other test in this file freezes its lanes first, so the guard that
    stops an un-packaged lane is never reached. Here the attempt is imported
    and the run is finished, but ``freeze_lane`` is never called -- exactly
    the state a coordinator is in when a worker has produced changes that
    nothing has yet bound to a hash. Verification must refuse it, and the
    candidate build must have nothing to work from.
    """
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
        freeze_lanes=False,
    )
    m = fixture["m"]
    context = fixture["context"]
    assert fixture["freeze_shas"] == {}

    with pytest.raises(m.coordinator_integration.IntegrationError) as unpackaged:
        m.coordinator_integration.verify_lane(context, "scc-a")
    assert unpackaged.value.code == "LANE_NOT_PACKAGED"
    assert unpackaged.value.status == "conflict"

    # No freeze exists, so there is no candidate to build either.
    with pytest.raises(m.coordinator_integration.IntegrationError) as empty:
        m.coordinator_integration.build_candidate(context, lane_freeze_sha256s=[])
    assert empty.value.code == "CANDIDATE_LANES_EMPTY"

    # The primary checkout never saw the worker's file.
    assert not (fixture["repo"] / "src" / "alpha.py").exists()


def test_apply_readback_mismatch_is_unknown_never_success(
    tmp_path: Path, monkeypatch
) -> None:
    """AC-T10-004: a readback mismatch resolves unknown, never success.

    ``PRIMARY_READBACK_UNKNOWN`` is the one apply-phase outcome no test
    reaches: the predecessor and dirty-primary guards fire *before* the
    mutation, and the crash tests interrupt it, but none of them let the
    mutation run and then observe a primary that does not match the candidate.
    Suppress just the ``git reset --hard`` in the primary repository, so the
    ref moves but the worktree does not follow. That is the real shape of a
    half-applied primary.
    """
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    ci = m.coordinator_integration
    built = ci.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )
    assert built["status"] == "success"

    import subprocess

    real_run = subprocess.run
    repository = fixture["repo"].resolve()

    def suppress_primary_reset(argv, *args, **kwargs):
        if (
            list(argv[:3]) == ["git", "reset", "--hard"]
            and Path(str(kwargs.get("cwd", ""))).resolve() == repository
        ):
            return subprocess.CompletedProcess(list(argv), 0, b"", b"")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(ci.subprocess, "run", suppress_primary_reset)
    with pytest.raises(ci.IntegrationError) as unknown:
        ci.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor=fixture["head"],
        )
    monkeypatch.undo()

    assert unknown.value.code == "PRIMARY_READBACK_UNKNOWN"
    assert unknown.value.status == "unknown"
    # The refusal is journaled as UNKNOWN, and never as APPLIED. Scope this to
    # the primary-integration effect: the same journal legitimately carries
    # APPLIED resolutions for the checkpoint and freeze effects that ran to
    # success earlier in the fixture, so a whole-file substring check would
    # assert the wrong thing.
    resolutions = [
        json.loads(line)
        for line in context.journal.path.read_text().splitlines()
        if line.strip()
    ]
    apply_states = [
        entry["status"]
        for entry in resolutions
        if entry.get("effect_type") == ci.EFFECT_APPLY
        and entry.get("phase") == "RESOLUTION"
    ]
    assert apply_states == ["UNKNOWN"]
    # No issue is marked integrated on an unknown outcome.
    current = context.checkpoints.current(rebuild_pointer=True)
    assert (
        current.value["issues"]["scc-a"]["integration"]["state"] != "primary_integrated"
    )


def test_apply_crash_before_and_after_recovery(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    built = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )

    # Crash after PRIMARY_INTEGRATION_PREPARED, before the primary moves.
    fired = {"n": 0}

    def crash_hook(event: str) -> None:
        if event == "after_journal_fsync":
            fired["n"] += 1
            if fired["n"] == 1:
                raise RuntimeError("crash before apply")

    context.crash_hook = crash_hook
    with pytest.raises(RuntimeError):
        m.coordinator_integration.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor=fixture["head"],
        )
    head = (
        m.coordinator_integration._git(fixture["repo"], "rev-parse", "HEAD")
        .decode()
        .strip()
    )
    assert head == fixture["head"]
    context2 = m.coordinator_integration.open_run(fixture["run_directory"])
    applied = m.coordinator_integration.apply_candidate(
        context2,
        built["candidate_id"],
        expected_predecessor=fixture["head"],
    )
    assert applied["status"] == "success"

    # Crash after the primary moved, before the resolution: replay probe
    # classifies APPLIED and never applies twice.
    fired2 = {"n": 0}

    def crash_hook2(event: str) -> None:
        if event == "after_journal_fsync":
            fired2["n"] += 1
            if fired2["n"] == 1:
                raise RuntimeError("crash after apply")

    (tmp_path / "second").mkdir()
    fixture2 = _setup(
        tmp_path / "second",
        [{"issue_id": "scc-b", "file": "src/beta.py", "content": "b = 2\n"}],
    )
    m2 = fixture2["m"]
    context3 = fixture2["context"]
    built2 = m2.coordinator_integration.build_candidate(
        context3, lane_freeze_sha256s=list(fixture2["freeze_shas"].values())
    )
    context3.crash_hook = crash_hook2
    with pytest.raises(RuntimeError):
        m2.coordinator_integration.apply_candidate(
            context3,
            built2["candidate_id"],
            expected_predecessor=fixture2["head"],
        )
    context4 = m2.coordinator_integration.open_run(fixture2["run_directory"])
    replay = m2.coordinator_integration.apply_candidate(
        context4,
        built2["candidate_id"],
        expected_predecessor=fixture2["head"],
    )
    assert replay["status"] == "success"
    head_tree = (
        m2.coordinator_integration._git(fixture2["repo"], "rev-parse", "HEAD^{tree}")
        .decode()
        .strip()
    )
    assert head_tree == built2["candidate_tree_sha256"]


def test_two_lanes_require_combined_review(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        [
            {"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"},
            {"issue_id": "scc-b", "file": "src/beta.py", "content": "b = 2\n"},
        ],
    )
    m = fixture["m"]
    context = fixture["context"]
    shas = list(fixture["freeze_shas"].values())
    built = m.coordinator_integration.build_candidate(context, lane_freeze_sha256s=shas)
    assert built["status"] == "partial"
    assert built["pending_actions"][0]["target_kind"] == "combined_candidate"

    # Apply refuses until the independent combined review is recorded.
    with pytest.raises(m.coordinator_integration.IntegrationError) as required:
        m.coordinator_integration.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor=fixture["head"],
        )
    assert required.value.code == "CANDIDATE_REVIEW_REQUIRED"

    review = common.reviewer_record(
        run_id=RUN_ID,
        target_kind="combined_candidate",
        target_sha256=built["candidate_record_sha256"],
        reviewer_id="combined-reviewer",
        implementer_ids=["implementer-a", "implementer-b"],
    )
    review_path = common.write_review(m, fixture["run_directory"], review)
    outcome = m.coordinator_integration.record_combined_review(context, review_path)
    assert outcome["status"] == "success"

    applied = m.coordinator_integration.apply_candidate(
        context,
        built["candidate_id"],
        expected_predecessor=fixture["head"],
    )
    assert applied["status"] == "success"
    assert (fixture["repo"] / "src" / "alpha.py").exists()
    assert (fixture["repo"] / "src" / "beta.py").exists()
    current = context.checkpoints.current(rebuild_pointer=True)
    assert (
        current.value["issues"]["scc-a"]["integration"]["state"] == "primary_integrated"
    )
    assert (
        current.value["issues"]["scc-b"]["integration"]["state"] == "primary_integrated"
    )


def test_conflicting_lanes_stop_without_autorender(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        [
            {
                "issue_id": "scc-a",
                "file": "src/alpha.py",
                "content": "value = 'lane-a'\n",
            },
            {
                "issue_id": "scc-b",
                "file": "src/alpha.py",
                "content": "value = 'lane-b'\n",
            },
        ],
    )
    m = fixture["m"]
    context = fixture["context"]
    with pytest.raises(m.coordinator_integration.IntegrationError) as conflict:
        m.coordinator_integration.build_candidate(
            context,
            lane_freeze_sha256s=list(fixture["freeze_shas"].values()),
        )
    assert conflict.value.code in {"CANDIDATE_CONFLICT", "CANDIDATE_TREE_FAILED"}
    # Primary unchanged and both lane artifacts remain recoverable.
    head = (
        m.coordinator_integration._git(fixture["repo"], "rev-parse", "HEAD")
        .decode()
        .strip()
    )
    assert head == fixture["head"]
    lanes = fixture["run_directory"] / "lanes"
    assert len(list(lanes.iterdir())) == 2
    current = context.checkpoints.current(rebuild_pointer=True)
    assert current.value["issues"]["scc-a"]["integration"]["state"] == "failed"
    assert current.value["issues"]["scc-b"]["integration"]["state"] == "failed"


def test_candidate_tests_failure_leaves_primary_unchanged(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    # Corrupt the imported packet's required command: the bytes no longer
    # match the entry's pinned packet hash, so candidate tests must refuse
    # the run-dir bytes with a typed error instead of trusting them.
    imports = (
        fixture["run_directory"]
        / "imports"
        / m.coordinator_state.issue_key("scc-a")
        / "packet.json"
    )
    packet = json.loads(imports.read_bytes())
    packet["verification"]["required_commands"] = [["/usr/bin/false"]]
    imports.write_bytes(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(m.coordinator_integration.IntegrationError) as invalid:
        m.coordinator_integration.build_candidate(
            context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
        )
    assert invalid.value.code == "CANDIDATE_PACKET_INVALID"
    head = (
        m.coordinator_integration._git(fixture["repo"], "rev-parse", "HEAD")
        .decode()
        .strip()
    )
    assert head == fixture["head"]
    current = context.checkpoints.current(rebuild_pointer=True)
    assert current.value["issues"]["scc-a"]["integration"]["state"] == "failed"


def test_candidate_test_command_failure_is_refused_and_recoverable(
    tmp_path: Path, monkeypatch
) -> None:
    """AC-T10-003: a required command that actually runs and exits nonzero.

    Distinct from ``test_candidate_tests_failure_leaves_primary_unchanged``,
    which corrupts the pinned packet hash and never lets a command run at
    all. Here the packet stays valid throughout, so ``build_candidate``
    reaches the genuine ``tests_passed=False`` *return* path (``status=
    "refused"``) rather than raising -- callers must inspect the result, not
    catch an exception.

    The worker's own attempt and the lane freeze/verify both re-run the same
    pinned required command (``_common.import_attempt`` and ``verify_lane``'s
    ``_rerun_commands``), so it must genuinely succeed there or the fixture
    could never freeze at all. Only ``build_candidate``'s own re-run, in
    ``_run_candidate_tests``, is made to fail -- by swapping in a really
    failing argv (``/usr/bin/false``) for just that one call, the same
    surgical-substitution technique ``test_apply_readback_mismatch_is_unknown_never_success``
    uses on ``subprocess.run`` for AC-T10-004. This is a low-level dependency
    swap, not a mock of the refusal logic under test.
    """
    fixture = _setup(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    ci = m.coordinator_integration

    import dataclasses

    real_run_command = ci.safe_output.run_command

    def fail_required_command(spec, *args, **kwargs):
        if spec.profile == "verification" and spec.argv == ("/usr/bin/printf", "ok"):
            spec = dataclasses.replace(spec, argv=("/usr/bin/false",))
        return real_run_command(spec, *args, **kwargs)

    monkeypatch.setattr(ci.safe_output, "run_command", fail_required_command)
    built = ci.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )
    monkeypatch.undo()
    assert built["status"] == "refused"
    assert built["error_code"] == "CANDIDATE_TESTS_FAILED"
    assert built["tests_passed"] is False

    # Primary untouched: build_candidate only ever wrote to a disposable
    # worktree under the run directory.
    head = ci._git(fixture["repo"], "rev-parse", "HEAD").decode().strip()
    assert head == fixture["head"]
    assert not (fixture["repo"] / "src" / "alpha.py").exists()

    # The candidate record and the lane freeze artifact survive the failure,
    # recoverable for inspection or retry.
    record_path = ci._candidate_directory(context, built["candidate_id"]) / (
        "candidate.json"
    )
    assert record_path.is_file()
    lanes = fixture["run_directory"] / "lanes"
    assert len(list(lanes.iterdir())) == 1
    current = context.checkpoints.current(rebuild_pointer=True)
    assert current.value["issues"]["scc-a"]["integration"]["state"] == "failed"

    # A failing-tests record can never be applied.
    with pytest.raises(ci.IntegrationError) as refused:
        ci.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor=fixture["head"],
        )
    assert refused.value.code == "CANDIDATE_TESTS_FAILED"
    head_after = ci._git(fixture["repo"], "rev-parse", "HEAD").decode().strip()
    assert head_after == fixture["head"]


def test_combined_review_rejects_self_review_and_failing_verdict(
    tmp_path: Path,
) -> None:
    """AC-T10-005: the combined-candidate review is independent of lane self-report.

    Two lanes force ``review_required``. A lane implementer submitting the
    *combined*-candidate review is refused by ``record_combined_review`` with
    its own error code (``CANDIDATE_REVIEW_INVALID``) -- not the
    ``REVIEW_NOT_INDEPENDENT`` code ``test_freeze_lane.py`` proves for
    per-lane self-report -- showing the combined-candidate gate is a
    distinct, independently-enforced check rather than inherited from lane
    review. A genuinely independent reviewer who fails the candidate has
    that verdict stored, but the candidate is refused both immediately
    (``record_combined_review``'s own return) and again at apply time
    (``apply_candidate``'s ``_validate_combined_review`` re-check), and the
    primary checkout is never touched.
    """
    fixture = _setup(
        tmp_path,
        [
            {"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"},
            {"issue_id": "scc-b", "file": "src/beta.py", "content": "b = 2\n"},
        ],
    )
    m = fixture["m"]
    context = fixture["context"]
    ci = m.coordinator_integration
    shas = list(fixture["freeze_shas"].values())
    built = ci.build_candidate(context, lane_freeze_sha256s=shas)
    assert built["status"] == "partial"

    self_review = common.reviewer_record(
        run_id=RUN_ID,
        target_kind="combined_candidate",
        target_sha256=built["candidate_record_sha256"],
        reviewer_id="implementer-a",
        implementer_ids=["implementer-a", "implementer-b"],
    )
    self_path = common.write_review(m, fixture["run_directory"], self_review)
    with pytest.raises(ci.IntegrationError) as dependent:
        ci.record_combined_review(context, self_path)
    assert dependent.value.code == "CANDIDATE_REVIEW_INVALID"

    failing_review = common.reviewer_record(
        run_id=RUN_ID,
        target_kind="combined_candidate",
        target_sha256=built["candidate_record_sha256"],
        reviewer_id="combined-reviewer",
        implementer_ids=["implementer-a", "implementer-b"],
        verdict="fail",
    )
    failing_path = common.write_review(m, fixture["run_directory"], failing_review)
    outcome = ci.record_combined_review(context, failing_path)
    assert outcome["status"] == "refused"
    assert outcome["error_code"] == "CANDIDATE_REVIEW_FAILED"

    with pytest.raises(ci.IntegrationError) as refused:
        ci.apply_candidate(
            context,
            built["candidate_id"],
            expected_predecessor=fixture["head"],
        )
    assert refused.value.code == "CANDIDATE_REVIEW_FAILED"

    head = ci._git(fixture["repo"], "rev-parse", "HEAD").decode().strip()
    assert head == fixture["head"]
    assert not (fixture["repo"] / "src" / "alpha.py").exists()
    assert not (fixture["repo"] / "src" / "beta.py").exists()
