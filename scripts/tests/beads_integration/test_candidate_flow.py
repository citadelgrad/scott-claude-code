"""Tests for build_candidate / record_combined_review / apply_candidate (t10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import _common as common

RUN_ID = "run-0123456789abcdef-20260904T010000.000000Z-AAAAAAAC"


def _setup(tmp_path: Path, lanes: list[dict]):
    """lanes: [{issue_id, file, content, allowed}] -> frozen+verified fixture."""
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
    for issue_id, item in results.items():
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
