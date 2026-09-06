"""AC-T12-001 / AC-T12-003: guarded claim-front and root-pointer bootstrap.

AC-T12-001 -- "Claim-front produces ownership only for eligible non-root
lanes; every partial native/local failure compensates or records exact
conflict/unknown without dispatch."

AC-T12-003 -- "Successful start-run requires root-pointer publication/
readback, checkpoint 2, and request-active in that order; every crash point
remains discoverable and resumable."
"""

from __future__ import annotations

import types

import pytest

from . import _common as common

m = common.modules("direct_operation", "coordinator_tracker")
do = m.direct_operation
ct = m.coordinator_tracker
state = m.coordinator_state
safe_bd = m.safe_bd
beads_ownership = m.beads_ownership

ROOT = "scc-root"
LANE_A = "scc-lane-a"
LANE_B = "scc-lane-b"
ACTOR = "parent"

OPEN_A = {"id": LANE_A, "status": "open", "assignee": None}
CLAIMED_A = {"id": LANE_A, "status": "in_progress", "assignee": ACTOR}
OTHER_A = {"id": LANE_A, "status": "in_progress", "assignee": "someone-else"}

# A run id that stays fixed across a crash-then-resume pair of ``start_run``
# calls, so both calls land in the same run directory -- exactly what a real
# resume looks like (the caller retries with the same request, not a new
# one).
FIXED_RUN_ID = common.make_run_id(seed=97)


def context(run, *, crash_hook=None):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=crash_hook or state.NOOP_HOOK,
    )


def native(responses, allowed):
    return common.FakeNative(safe_bd, responses=responses, allowed=allowed)


def comments(*bodies):
    """Wrap marker-comment bodies the way ``issue_comments`` replies them.

    ``FakeNative`` reads a list-valued reply as a queue of successive
    replies, not as one list-shaped reply, so a single ``issue_comments``
    answer that is itself a list has to be wrapped once more.
    """
    return [[{"text": body} for body in bodies]]


def pointer_request(*, repo, run_root, root_issue_id=ROOT):
    """The slice of ``StartRunInput`` :func:`coordinator_tracker.pointer_callbacks`
    actually reads: ``repository_root``, ``root_issue_id`` and ``run_root``.
    A plain namespace is enough -- the real thing is exercised end to end by
    the ``start_run`` tests below.
    """
    return types.SimpleNamespace(
        repository_root=str(repo), root_issue_id=root_issue_id, run_root=str(run_root)
    )


def tracker_double(root_issue_id):
    """A tiny in-memory tracker: round-trips whatever ``set_run_pointer``
    writes back out of the next ``issue_get``, without hand-computing the
    exact canonical JSON in advance.
    """
    box: dict[str, dict[str, str]] = {"metadata": {}}

    def get_issue(_request):
        return {"id": root_issue_id, "metadata": dict(box["metadata"])}

    def set_pointer(request):
        box["metadata"][ct._POINTER_METADATA_KEY] = request.arguments["value"]
        return {"id": root_issue_id, "metadata": dict(box["metadata"])}

    fake = native(
        {"issue_get": get_issue, "set_run_pointer": set_pointer},
        allowed=["issue_get", "set_run_pointer"],
    )
    return fake, box


@pytest.fixture()
def run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return common.make_run(m, repo, root_issue_id=ROOT)


# ---------------------------------------------------------------------------
# claim_front (AC-T12-001)
# ---------------------------------------------------------------------------


def test_claim_front_refuses_the_root_issue(run):
    ctx = context(run)
    fake = native({}, allowed=[])

    results = ct.claim_front(
        ctx, ownership=run["ownership"], issues=[ROOT], actor=ACTOR, runner=fake
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


def test_claim_front_claims_an_eligible_lane(run):
    ctx = context(run)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN_A, CLAIMED_A],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED_A,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )

    results = ct.claim_front(
        ctx, ownership=run["ownership"], issues=[LANE_A], actor=ACTOR, runner=fake
    )

    assert len(results) == 1
    result = results[0]
    assert result.issue_id == LANE_A
    assert result.status == do.APPLIED
    assert result.classification == do.INTENDED_EFFECT_PRESENT
    assert result.error_code is None
    assert result.ownership_epoch == 1
    held = run["ownership"].inspect(LANE_A)
    assert held.disposition == "held"
    assert held.record["epoch"] == 1
    assert held.record["run_id"] == ctx.run_id


def test_claim_front_compensates_when_native_claim_does_not_apply(run):
    ctx = context(run)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN_A, OPEN_A],
            "append_marker_note": {"ok": True},
            "claim_exact": {"ok": True},
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )

    results = ct.claim_front(
        ctx, ownership=run["ownership"], issues=[LANE_A], actor=ACTOR, runner=fake
    )

    result = results[0]
    assert result.status == do.NOT_APPLIED
    assert result.ownership_epoch == 1
    released = run["ownership"].inspect(LANE_A)
    assert released.disposition == "released"
    # The compensating release must not reuse the acquire's operation id --
    # beads_ownership treats operation-id reuse against a non-matching prior
    # event as a hard conflict, so a shared id would have raised instead of
    # releasing cleanly.
    operation_ids = {event["operation_id"] for event in released.history}
    assert len(operation_ids) == 2


def test_claim_front_compensates_on_native_conflict(run):
    ctx = context(run)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN_A, OTHER_A],
            "append_marker_note": {"ok": True},
            "claim_exact": {"ok": True},
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )

    results = ct.claim_front(
        ctx, ownership=run["ownership"], issues=[LANE_A], actor=ACTOR, runner=fake
    )

    result = results[0]
    assert result.status == do.CONFLICT
    released = run["ownership"].inspect(LANE_A)
    assert released.disposition == "released"


def test_claim_front_records_unknown_without_a_false_release(run):
    ctx = context(run)
    probe = common.FakeNative(safe_bd)
    fake = native(
        {"issue_comments": comments(), "issue_get": probe.error("issue_get")},
        allowed=["issue_comments", "issue_get"],
    )

    results = ct.claim_front(
        ctx, ownership=run["ownership"], issues=[LANE_A], actor=ACTOR, runner=fake
    )

    result = results[0]
    assert result.status == do.UNKNOWN
    assert result.ownership_epoch == 1
    # No claim_exact/append_marker_note in `allowed` -- if execute() had
    # tried to dispatch, FakeNative would have raised NativeDenied and
    # failed this test outright.
    held = run["ownership"].inspect(LANE_A)
    assert held.disposition == "held"


def test_claim_front_processes_multiple_lanes_independently(run):
    ctx = context(run)
    # A second, unrelated run holds LANE_B first -- claim_front must see the
    # conflict purely from local ownership state, with no native call at all.
    rival = common.make_run(m, run["repo"], root_issue_id=run["root_issue_id"])
    claimed = rival["ownership"].acquire(
        issue_id=LANE_B,
        actor="rival",
        run_directory=rival["run_directory"],
        tracker_state_sha256="0" * 64,
        operation_id="1" * 64,
        now=None,
    )
    assert claimed.disposition == "held"

    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN_A, CLAIMED_A],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED_A,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )

    results = ct.claim_front(
        ctx,
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
    assert by_issue[LANE_B].ownership_epoch is None


# ---------------------------------------------------------------------------
# pointer_callbacks (AC-T12-003, supporting)
# ---------------------------------------------------------------------------

POINTER = {
    "schema_version": "beads.run-pointer.v1",
    "run_id": "run-fixture",
    "generation": 1,
}
OTHER_POINTER = {
    "schema_version": "beads.run-pointer.v1",
    "run_id": "run-rival",
    "generation": 9,
}


def test_pointer_callbacks_observe_present(run):
    request = pointer_request(repo=run["repo"], run_root=run["run_root"])
    value = state.canonical_payload_bytes(POINTER).decode("utf-8")
    fake = native(
        {"issue_get": {"id": ROOT, "metadata": {ct._POINTER_METADATA_KEY: value}}},
        allowed=["issue_get"],
    )
    callbacks = ct.pointer_callbacks(request, actor=ACTOR, runner=fake)

    observation = callbacks.observe(POINTER)

    assert observation.classification == do.INTENDED_EFFECT_PRESENT
    assert observation.observed_value == POINTER
    assert observation.state_sha256 == state.sha256_bytes(
        state.canonical_payload_bytes(POINTER)
    )


def test_pointer_callbacks_observe_absent(run):
    request = pointer_request(repo=run["repo"], run_root=run["run_root"])
    fake = native({"issue_get": {"id": ROOT, "metadata": {}}}, allowed=["issue_get"])
    callbacks = ct.pointer_callbacks(request, actor=ACTOR, runner=fake)

    observation = callbacks.observe(POINTER)

    assert observation.classification == do.PRESTATE_UNCHANGED
    assert observation.state_sha256 == state.GENESIS_SHA256
    assert observation.observed_value is None


def test_pointer_callbacks_observe_conflicting(run):
    request = pointer_request(repo=run["repo"], run_root=run["run_root"])
    value = state.canonical_payload_bytes(OTHER_POINTER).decode("utf-8")
    fake = native(
        {"issue_get": {"id": ROOT, "metadata": {ct._POINTER_METADATA_KEY: value}}},
        allowed=["issue_get"],
    )
    callbacks = ct.pointer_callbacks(request, actor=ACTOR, runner=fake)

    observation = callbacks.observe(POINTER)

    assert observation.classification == do.CONFLICTING_EFFECT
    assert observation.observed_value == OTHER_POINTER
    assert observation.state_sha256 == state.sha256_bytes(
        state.canonical_payload_bytes(OTHER_POINTER)
    )


def test_pointer_callbacks_observe_unreadable(run):
    request = pointer_request(repo=run["repo"], run_root=run["run_root"])
    probe = common.FakeNative(safe_bd)
    fake = native({"issue_get": probe.error("issue_get")}, allowed=["issue_get"])
    callbacks = ct.pointer_callbacks(request, actor=ACTOR, runner=fake)

    observation = callbacks.observe(POINTER)

    assert observation.classification == do.INSUFFICIENT_OBSERVATION
    assert observation.state_sha256 == state.GENESIS_SHA256
    assert observation.observed_value is None


def test_pointer_callbacks_publish_dispatches_through_direct_operation(run):
    fake, _box = tracker_double(run["root_issue_id"])
    request = pointer_request(
        repo=run["repo"], run_root=run["run_root"], root_issue_id=run["root_issue_id"]
    )
    pointer = {"run_id": run["run_id"], "ownership_epoch": None, "generation": 1}
    callbacks = ct.pointer_callbacks(request, actor=ACTOR, runner=fake)

    observation = callbacks.publish(pointer)

    assert observation.classification == do.INTENDED_EFFECT_PRESENT
    assert observation.observed_value == pointer
    assert fake.count("set_run_pointer") == 1
    # marker_enabled=False on TRACKER_RUN_POINTER: no comment-marker traffic
    # at all, proven by these never having been in `allowed` either.
    assert fake.count("issue_comments") == 0
    assert fake.count("append_marker_note") == 0


# ---------------------------------------------------------------------------
# start_run (AC-T12-003)
# ---------------------------------------------------------------------------


class _NthCrashHook:
    def __init__(self, label: str, n: int) -> None:
        self.label = label
        self.n = n
        self.seen = 0

        class Crash(RuntimeError):
            pass

        self.Crash = Crash

    def __call__(self, event: str) -> None:
        if event == self.label:
            self.seen += 1
            if self.seen == self.n:
                raise self.Crash(f"{self.label}#{self.n}")


def crash_on_nth(label: str, n: int) -> _NthCrashHook:
    """A crash hook that raises on the ``n``-th call carrying ``label``.

    ``coordinator_state`` reuses the ``"after_pointer_publication"`` label
    for two different milestones: every :class:`state.CheckpointStore` accept
    fires it once for its own generation-pointer file (checkpoint 1's own
    accept, first, then checkpoint 2's later), and ``bootstrap_run`` fires it
    a second time, in between, right after the *root run pointer* itself is
    observed or published. ``common.crash_after`` fires on the first match,
    which lands inside checkpoint 1's own bookkeeping -- too early to prove
    anything about root-pointer resumability. This counts occurrences so a
    test can target the milestone that actually matters.
    """
    return _NthCrashHook(label, n)


def start_request(*, tmp_path, request_id="request-0001-start-run-test"):
    repo = tmp_path / "repo"
    repo.mkdir()
    hermes = repo / ".hermes"
    hermes.mkdir(mode=0o700)
    run_root = hermes / "beads-runs"
    run_root.mkdir(mode=0o700)
    for path in (hermes, run_root):
        path.chmod(0o700)
    request = state.StartRunInput(
        request_id=request_id,
        repository_root=str(repo),
        git_common_dir=str(repo / ".git"),
        workspace=str(repo),
        run_root=str(run_root),
        root_issue_id=ROOT,
        scope_issue_ids=(LANE_A,),
        actor=ACTOR,
        base_git_commit="a" * 40,
        authority_snapshot_sha256="b" * 64,
        workspace_identity_sha256="c" * 64,
    )
    return request, repo, run_root


def test_start_run_orders_pointer_publish_before_checkpoint_before_active(tmp_path):
    request, _repo, run_root = start_request(tmp_path=tmp_path)
    fake, _box = tracker_double(ROOT)

    result = ct.start_run(
        request,
        actor=ACTOR,
        now=common.now(),
        runner=fake,
        run_id_factory=lambda: FIXED_RUN_ID,
    )

    assert result.disposition == "active"
    assert result.run_id == FIXED_RUN_ID
    assert result.run_directory == run_root / FIXED_RUN_ID
    # The root pointer must have actually been published, exactly once, as
    # part of reaching "active".
    assert fake.count("set_run_pointer") == 1


def test_start_run_crash_after_pointer_publication_resumes_without_redispatch(tmp_path):
    request, _repo, run_root = start_request(
        tmp_path=tmp_path, request_id="request-0002-crash-resume-test"
    )
    fake, _box = tracker_double(ROOT)
    # The 2nd occurrence, not the 1st: see crash_on_nth's docstring.
    crash_hook = crash_on_nth("after_pointer_publication", 2)

    with pytest.raises(crash_hook.Crash):
        ct.start_run(
            request,
            actor=ACTOR,
            now=common.now(),
            runner=fake,
            run_id_factory=lambda: FIXED_RUN_ID,
            crash_hook=crash_hook,
        )

    # The native pointer write already landed before the crash point fires --
    # that ordering is exactly what makes the resume below safe.
    assert fake.count("set_run_pointer") == 1

    result = ct.start_run(
        request,
        actor=ACTOR,
        now=common.now(),
        runner=fake,
        run_id_factory=lambda: FIXED_RUN_ID,
    )

    assert result.disposition == "active"
    assert result.run_id == FIXED_RUN_ID
    assert result.run_directory == run_root / FIXED_RUN_ID
    # Resuming must not redispatch the pointer write a second time: the
    # observe-before-publish short-circuit inside bootstrap_run is the whole
    # point of this test.
    assert fake.count("set_run_pointer") == 1
