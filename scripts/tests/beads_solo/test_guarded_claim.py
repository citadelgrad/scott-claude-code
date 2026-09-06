"""Guarded solo claim fixtures (scc-0pu.15 / t14).

Exercises ``coordinator_tracker.claim_front`` (t12 / scc-0pu.13) for a single
worker claiming its own lane, adapting the proven precedent in the frozen
sibling suite ``beads_tracker/test_coordinator_tracker.py`` with distinct
issue identities and fixture framing. Covers:

- AC-T14-001: an eligible lane claims cleanly with exact epoch/state readback
  and, for a root/rival-held lane, zero native dispatch at all.
- AC-T14-002: a native claim that does not apply, one that lands on someone
  else, and an unreadable post-effect probe all resolve to the exact typed
  outcome, with ownership released only when it is truly safe to.
"""

from __future__ import annotations

from . import _common as common

m = common.modules("direct_operation", "coordinator_tracker")
do = m.direct_operation
ct = m.coordinator_tracker

ROOT = "solo-root"
LANE_A = "solo-lane-a"
LANE_B = "solo-lane-b"
ACTOR = "solo-worker"

OPEN_A = {"id": LANE_A, "status": "open", "assignee": None}
CLAIMED_A = {"id": LANE_A, "status": "in_progress", "assignee": ACTOR}
OTHER_A = {"id": LANE_A, "status": "in_progress", "assignee": "someone-else"}


def context(run):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=m.coordinator_state.NOOP_HOOK,
    )


def native(responses, allowed):
    return common.FakeNative(m.safe_bd, responses=responses, allowed=allowed)


def make_run(tmp_path, *, run_id=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return common.make_run(m, repo, run_id=run_id, root_issue_id=ROOT, actor=ACTOR)


def test_claim_front_refuses_the_root_issue_with_zero_dispatch(tmp_path):
    run = make_run(tmp_path)
    fake = native({}, allowed=[])
    results = ct.claim_front(
        context(run),
        ownership=run["ownership"],
        issues=[ROOT],
        actor=ACTOR,
        runner=fake,
    )
    assert results == [
        ct.LaneClaimResult(
            issue_id=ROOT,
            status="REFUSED",
            classification=None,
            ownership_epoch=None,
            error_code="COORDINATOR_TRACKER_ROOT_ISSUE_INELIGIBLE",
        )
    ]
    assert fake.calls == []
    assert run["ownership"].inspect(ROOT).disposition == "unheld"


def test_claim_front_claims_an_eligible_lane_with_exact_readback(tmp_path):
    run = make_run(tmp_path)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [OPEN_A, CLAIMED_A],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED_A,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    results = ct.claim_front(
        context(run),
        ownership=run["ownership"],
        issues=[LANE_A],
        actor=ACTOR,
        runner=fake,
    )
    result = results[0]
    assert result.status == do.APPLIED
    assert result.classification == do.INTENDED_EFFECT_PRESENT
    assert result.error_code is None
    assert result.ownership_epoch == 1
    held = run["ownership"].inspect(LANE_A)
    assert held.disposition == "held"
    assert held.record["epoch"] == 1
    assert held.record["run_id"] == context(run).run_id
    assert fake.count("claim_exact") == 1


def test_claim_front_compensates_when_native_claim_does_not_apply(tmp_path):
    run = make_run(tmp_path)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [OPEN_A, OPEN_A],
            "append_marker_note": {"ok": True},
            "claim_exact": OPEN_A,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    results = ct.claim_front(
        context(run),
        ownership=run["ownership"],
        issues=[LANE_A],
        actor=ACTOR,
        runner=fake,
    )
    result = results[0]
    assert result.status == do.NOT_APPLIED
    released = run["ownership"].inspect(LANE_A)
    assert released.disposition == "released"
    operation_ids = {event["operation_id"] for event in released.history}
    assert len(operation_ids) == 2


def test_claim_front_compensates_on_native_conflict(tmp_path):
    run = make_run(tmp_path)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [OPEN_A, OTHER_A],
            "append_marker_note": {"ok": True},
            "claim_exact": OTHER_A,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    results = ct.claim_front(
        context(run),
        ownership=run["ownership"],
        issues=[LANE_A],
        actor=ACTOR,
        runner=fake,
    )
    result = results[0]
    assert result.status == do.CONFLICT
    assert run["ownership"].inspect(LANE_A).disposition == "released"


def test_claim_front_records_unknown_without_a_false_release(tmp_path):
    run = make_run(tmp_path)
    probe = common.FakeNative(m.safe_bd)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": probe.error("issue_get"),
        },
        allowed=["issue_comments", "issue_get"],
    )
    results = ct.claim_front(
        context(run),
        ownership=run["ownership"],
        issues=[LANE_A],
        actor=ACTOR,
        runner=fake,
    )
    result = results[0]
    assert result.status == do.UNKNOWN
    assert result.ownership_epoch == 1
    held = run["ownership"].inspect(LANE_A)
    assert held.disposition == "held"


def test_claim_front_processes_multiple_lanes_independently(tmp_path):
    run = make_run(tmp_path)
    rival = common.make_run(m, run["repo"], root_issue_id=ROOT, actor="rival-worker")
    claimed = rival["ownership"].acquire(
        issue_id=LANE_B,
        actor="rival-worker",
        run_directory=rival["run_directory"],
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=None,
    )
    assert claimed.disposition == "held"

    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [OPEN_A, CLAIMED_A],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED_A,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    results = ct.claim_front(
        context(run),
        ownership=run["ownership"],
        issues=[ROOT, LANE_A, LANE_B],
        actor=ACTOR,
        runner=fake,
    )
    by_issue = {result.issue_id: result for result in results}
    assert by_issue[ROOT].status == "REFUSED"
    assert by_issue[LANE_A].status == do.APPLIED
    assert by_issue[LANE_B].status == "CONFLICT"
    assert by_issue[LANE_B].error_code == "COORDINATOR_TRACKER_OWNERSHIP_CONFLICT"
    assert fake.count("claim_exact") == 1
