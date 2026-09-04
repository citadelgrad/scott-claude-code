"""Tests for freeze_lane / verify_lane / record_review (t10)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _common as common

RUN_ID = "run-0123456789abcdef-20260904T010000.000000Z-AAAAAAAB"
ISSUE = "scc-lane1"


def _setup(
    tmp_path: Path,
    *,
    issue_id: str = ISSUE,
    changed_file: str = "src/code.py",
    content: str = "print('lane1')\n",
    allowed: list[str] | None = None,
):
    m = common.modules()
    factory = common.factory()
    repo, lane, head = factory._git_lane(tmp_path)
    outbox = lane / "outbox"
    run = common.make_run(m, repo, RUN_ID, [issue_id])
    epoch = run["epochs"][issue_id]
    packet = common.lane_packet(
        factory,
        tmp_path,
        issue_id=issue_id,
        run_id=RUN_ID,
        worktree=lane,
        outbox=outbox,
        head=head,
        allowed=allowed or ["src/**"],
    )
    digest, result_sha = common.import_attempt(
        m,
        run["run_directory"],
        issue_id=issue_id,
        epoch=epoch,
        worktree=lane,
        outbox=outbox,
        head=head,
        changed_file=changed_file,
        file_content=content,
        packet=packet,
    )
    entry = common.issue_entry(m, epoch=epoch, result_sha=result_sha, packet_sha=digest)
    run["finish"]({issue_id: entry})
    context = m.coordinator_integration.open_run(run["run_directory"])
    return {
        "m": m,
        "repo": repo,
        "lane": lane,
        "outbox": outbox,
        "head": head,
        "epoch": epoch,
        "packet_sha": digest,
        "result_sha": result_sha,
        "run": run,
        "run_directory": run["run_directory"],
        "context": context,
        "issue_id": issue_id,
    }


def test_freeze_lane_packages_accepted_lane(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    m = fixture["m"]
    result = m.coordinator_integration.freeze_lane(
        fixture["context"],
        fixture["issue_id"],
        expected_result_sha256=fixture["result_sha"],
    )
    assert result["status"] == "success"
    freeze_path = (
        fixture["run_directory"]
        / "lanes"
        / m.coordinator_state.issue_key(fixture["issue_id"])
        / "lane-freeze.json"
    )
    assert freeze_path.is_file()
    freeze = json.loads(freeze_path.read_bytes())
    assert freeze["transfer_mode"] == "patch_package"
    assert freeze["reproduction_status"] == "reproduced"
    assert freeze["worker_result_sha256"] == fixture["result_sha"]
    assert freeze["ownership_epoch"] == fixture["epoch"]

    records = fixture["context"].journal.read().records
    freeze_events = [r for r in records if r["effect_type"] == "LANE_FREEZE_PREPARED"]
    assert {r["phase"] for r in freeze_events} == {"PREPARED", "RESOLUTION"}
    assert freeze_events[1]["status"] == "APPLIED"

    current = fixture["context"].checkpoints.current(rebuild_pointer=True)
    entry = current.value["issues"][fixture["issue_id"]]
    assert entry["artifact"]["state"] == "packaged"
    assert entry["artifact"]["lane_freeze_sha256"] == result["freeze_sha256"]

    # Idempotent replay of the same accepted result is a no-op success.
    again = m.coordinator_integration.freeze_lane(
        fixture["context"],
        fixture["issue_id"],
        expected_result_sha256=fixture["result_sha"],
    )
    assert again["status"] == "success"


def test_freeze_lane_refuses_without_accepted_result(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    m = fixture["m"]
    with pytest.raises(m.coordinator_integration.IntegrationError) as refused:
        m.coordinator_integration.freeze_lane(
            fixture["context"],
            fixture["issue_id"],
            expected_result_sha256="0" * 64,
        )
    assert refused.value.code == "LANE_FREEZE_IDENTITY_MISMATCH"
    key = m.coordinator_state.issue_key(fixture["issue_id"])
    assert not (fixture["run_directory"] / "lanes" / key / "lane-freeze.json").exists()


def test_freeze_lane_refuses_lane_drift(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    m = fixture["m"]
    (fixture["lane"] / "src" / "code.py").write_text("mutated\n")
    with pytest.raises(m.coordinator_integration.IntegrationError) as drift:
        m.coordinator_integration.freeze_lane(
            fixture["context"],
            fixture["issue_id"],
            expected_result_sha256=fixture["result_sha"],
        )
    assert drift.value.code == "LANE_FREEZE_LANE_DRIFT"
    current = fixture["context"].checkpoints.current(rebuild_pointer=True)
    entry = current.value["issues"][fixture["issue_id"]]
    assert entry["artifact"]["state"] == "invalid"


def test_freeze_lane_crash_between_prepared_and_write_recovers(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    m = fixture["m"]
    context = fixture["context"]
    fired = {"n": 0}

    def crash_hook(event: str) -> None:
        if event == "after_journal_fsync":
            fired["n"] += 1
            if fired["n"] == 1:
                raise RuntimeError("simulated crash after PREPARED fsync")

    context.crash_hook = crash_hook
    with pytest.raises(RuntimeError):
        m.coordinator_integration.freeze_lane(
            context,
            fixture["issue_id"],
            expected_result_sha256=fixture["result_sha"],
        )
    records = context.journal.read().records
    assert any(
        r["phase"] == "PREPARED" and r["effect_type"] == "LANE_FREEZE_PREPARED"
        for r in records
    )
    context2 = m.coordinator_integration.open_run(fixture["run_directory"])
    result = m.coordinator_integration.freeze_lane(
        context2,
        fixture["issue_id"],
        expected_result_sha256=fixture["result_sha"],
    )
    assert result["status"] == "success"
    records = context2.journal.read().records
    freeze_events = [r for r in records if r["effect_type"] == "LANE_FREEZE_PREPARED"]
    # The dangling PREPARED is reused in flight: no second prepared payload,
    # exactly one package write, one APPLIED resolution.
    assert len(freeze_events) == 2
    prepared_events = [r for r in freeze_events if r["phase"] == "PREPARED"]
    assert len(prepared_events) == 1
    resolutions = [r for r in freeze_events if r["phase"] == "RESOLUTION"]
    assert {r["status"] for r in resolutions} == {"APPLIED"}


def test_freeze_lane_conflicting_recovery_is_conflict(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    m = fixture["m"]
    context = fixture["context"]
    fired = {"n": 0}

    def crash_hook(event: str) -> None:
        if event == "after_journal_fsync":
            fired["n"] += 1
            if fired["n"] == 1:
                raise RuntimeError("crash")

    context.crash_hook = crash_hook
    with pytest.raises(RuntimeError):
        m.coordinator_integration.freeze_lane(
            context,
            fixture["issue_id"],
            expected_result_sha256=fixture["result_sha"],
        )
    key = m.coordinator_state.issue_key(fixture["issue_id"])
    lanes = fixture["run_directory"] / "lanes" / key
    lanes.mkdir(parents=True, exist_ok=True)
    foreign = lanes / "lane-freeze.json"
    foreign.write_bytes(b'{"schema_version":"beads.lane-freeze.v1"}\n')
    foreign.chmod(0o600)
    context2 = m.coordinator_integration.open_run(fixture["run_directory"])
    with pytest.raises(m.coordinator_integration.IntegrationError) as conflict:
        m.coordinator_integration.freeze_lane(
            context2,
            fixture["issue_id"],
            expected_result_sha256=fixture["result_sha"],
        )
    assert conflict.value.code == "LANE_FREEZE_RECOVERY_CONFLICT"
    records = context2.journal.read().records
    assert any(
        r["phase"] == "RESOLUTION" and r["status"] == "CONFLICT" for r in records
    )


def test_verify_and_review_matrix(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    m = fixture["m"]
    context = fixture["context"]
    frozen = m.coordinator_integration.freeze_lane(
        context,
        fixture["issue_id"],
        expected_result_sha256=fixture["result_sha"],
    )
    assert frozen["status"] == "success"
    verified = m.coordinator_integration.verify_lane(context, fixture["issue_id"])
    assert verified["status"] == "success"
    current = context.checkpoints.current(rebuild_pointer=True)
    entry = current.value["issues"][fixture["issue_id"]]
    assert entry["verification"]["state"] == "passed"
    assert entry["artifact"]["state"] == "verified"
    assert entry["review"]["state"] == "not_required"
    record_path = Path(verified["verification_path"])
    record = json.loads(record_path.read_bytes())
    assert record["disposition"] == "accept"
    assert record["lane_freeze_sha256"] == frozen["freeze_sha256"]


def test_high_risk_lane_requires_independent_review(tmp_path: Path) -> None:
    fixture = _setup(
        tmp_path,
        issue_id="scc-auth",
        changed_file="src/auth_gate.py",
        content="def check(): return True\n",
    )
    m = fixture["m"]
    context = fixture["context"]
    frozen = m.coordinator_integration.freeze_lane(
        context, fixture["issue_id"], expected_result_sha256=fixture["result_sha"]
    )
    assert frozen["status"] == "success"
    verified = m.coordinator_integration.verify_lane(context, fixture["issue_id"])
    assert verified["status"] == "partial"
    assert verified["pending_actions"][0]["action"] == "review_request"
    current = context.checkpoints.current(rebuild_pointer=True)
    assert current.value["issues"][fixture["issue_id"]]["review"]["state"] == "required"

    # The implementer cannot review their own lane.
    review = common.reviewer_record(
        run_id=RUN_ID,
        target_kind="lane_freeze",
        target_sha256=frozen["freeze_sha256"],
        reviewer_id="implementer-agent",
        implementer_ids=["implementer-agent"],
    )
    path = common.write_review(m, fixture["run_directory"], review)
    with pytest.raises(m.coordinator_integration.IntegrationError) as dependent:
        m.coordinator_integration.record_review(context, fixture["issue_id"], path)
    assert dependent.value.code == "REVIEW_NOT_INDEPENDENT"

    # Target hash drift invalidates the review.
    stale = common.reviewer_record(
        run_id=RUN_ID,
        target_kind="lane_freeze",
        target_sha256="0" * 64,
        reviewer_id="reviewer-2",
        implementer_ids=["implementer-agent"],
    )
    stale_path = common.write_review(m, fixture["run_directory"], stale)
    with pytest.raises(m.coordinator_integration.IntegrationError) as stale_err:
        m.coordinator_integration.record_review(
            context, fixture["issue_id"], stale_path
        )
    assert stale_err.value.code == "REVIEW_TARGET_STALE"

    fresh = common.reviewer_record(
        run_id=RUN_ID,
        target_kind="lane_freeze",
        target_sha256=frozen["freeze_sha256"],
        reviewer_id="reviewer-2",
        implementer_ids=["implementer-agent"],
    )
    fresh_path = common.write_review(m, fixture["run_directory"], fresh)
    outcome = m.coordinator_integration.record_review(
        context, fixture["issue_id"], fresh_path
    )
    assert outcome["status"] == "success"
    current = context.checkpoints.current(rebuild_pointer=True)
    assert current.value["issues"][fixture["issue_id"]]["review"]["state"] == "passed"
