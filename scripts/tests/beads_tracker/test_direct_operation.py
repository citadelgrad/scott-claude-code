"""AC-T12-002: direct operations converge, conflict, or stay honestly unknown.

The suite drives ``direct_operation.execute`` through :class:`FakeNative`, a
scripted ``safe_bd`` stand-in that raises on any profile the test did not
allow.  That turns "never blindly replays" from a claim into an assertion: a
test that expects no close simply omits ``close_exact`` from ``allowed``, and
a dispatch would fail the test rather than pass unnoticed.
"""

from __future__ import annotations

import json

import pytest

from . import _common as common

m = common.modules("direct_operation")
do = m.direct_operation
state = m.coordinator_state
safe_bd = m.safe_bd


ISSUE = "scc-lane-a"
OPEN = {"id": ISSUE, "status": "open", "assignee": None}
CLAIMED = {"id": ISSUE, "status": "in_progress", "assignee": "parent"}
OTHER = {"id": ISSUE, "status": "in_progress", "assignee": "someone-else"}
CLOSED = {"id": ISSUE, "status": "closed", "assignee": "parent"}


def context(run, *, crash_hook=None):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=crash_hook or state.NOOP_HOOK,
    )


def claim_operation(run, *, caller_key="claim/scc-lane-a"):
    return do.DirectOperation(
        caller_key=caller_key,
        effect_type="TRACKER_CLAIM",
        target_identity=f"bd://issue/{ISSUE}",
        issue_id=ISSUE,
        ownership_epoch=run["epochs"].get(ISSUE),
        arguments={"issue_id": ISSUE},
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": ISSUE},
            intended={"status": "in_progress", "assignee": "parent"},
            prestate={"status": "open"},
        ),
    )


def close_operation(run, *, caller_key="close/scc-lane-a"):
    return do.DirectOperation(
        caller_key=caller_key,
        effect_type="TRACKER_CLOSE",
        target_identity=f"bd://issue/{ISSUE}",
        issue_id=ISSUE,
        ownership_epoch=run["epochs"].get(ISSUE),
        arguments={"issue_id": ISSUE, "reason": "done"},
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": ISSUE},
            intended={"status": "closed"},
            prestate={"status": "in_progress"},
        ),
    )


def native(responses, allowed):
    return common.FakeNative(safe_bd, responses=responses, allowed=allowed)


def comments(*bodies):
    """Script one ``issue_comments`` reply carrying the given comment bodies.

    ``FakeNative`` reads a list value as a queue of successive replies, so a
    reply that is itself a list has to be wrapped once.  Every comment payload
    goes through this helper so that trap stays in one place.
    """
    return [[{"text": body} for body in bodies]]


def marker_note(run, operation, *, effect_type=None):
    """Rebuild the marker text a prior attempt would have written."""
    ctx = context(run)
    path, sha, changed = do._freeze_intent(ctx, operation, actor="parent")
    assert not changed
    return do.marker_text(
        run_id=run["run_id"],
        intent_sha256=sha,
        effect_type=effect_type or operation.effect_type,
    )


@pytest.fixture()
def run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return common.make_run(m, repo, issue_ids=[ISSUE])


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


def test_classification_status_map_is_the_canonical_four_way_taxonomy():
    assert do.CLASSIFICATION_STATUS == {
        "intended_effect_present": "APPLIED",
        "prestate_unchanged": "NOT_APPLIED",
        "conflicting_effect": "CONFLICT",
        "insufficient_observation": "UNKNOWN",
    }


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (CLAIMED, do.INTENDED_EFFECT_PRESENT),
        (OPEN, do.PRESTATE_UNCHANGED),
        (OTHER, do.CONFLICTING_EFFECT),
        (None, do.INSUFFICIENT_OBSERVATION),
        (do.ABSENT, do.CONFLICTING_EFFECT),
    ],
)
def test_classify_observation_covers_every_branch(observed, expected):
    assert (
        do.classify_observation(
            observed,
            intended={"status": "in_progress", "assignee": "parent"},
            prestate={"status": "open"},
        )
        == expected
    )


def test_classify_observation_handles_absence_on_both_sides():
    assert (
        do.classify_observation(
            do.ABSENT, intended=do.ABSENT, prestate={"status": "open"}
        )
        == do.INTENDED_EFFECT_PRESENT
    )
    assert (
        do.classify_observation(do.ABSENT, intended={"id": ISSUE}, prestate=do.ABSENT)
        == do.PRESTATE_UNCHANGED
    )
    assert (
        do.classify_observation(OPEN, intended={"status": "closed"}, prestate=do.ABSENT)
        == do.CONFLICTING_EFFECT
    )


def test_default_select_separates_absent_from_unreadable():
    assert do.default_select(None, issue_id=ISSUE) is None
    assert do.default_select({"issues": []}, issue_id=ISSUE) == do.ABSENT
    assert do.default_select({"issues": [OPEN]}, issue_id=ISSUE) == OPEN
    assert do.default_select(OPEN, issue_id=ISSUE) == OPEN
    assert do.default_select({"unexpected": 1}, issue_id=ISSUE) is None


# ---------------------------------------------------------------------------
# The happy path and its ordering
# ---------------------------------------------------------------------------


def test_applied_records_prepared_then_resolution(run):
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    assert result.status == do.APPLIED
    assert result.dispatched is True
    assert result.attempt_id == "attempt-001"

    prepared = common.journal_records(
        run, effect_type="TRACKER_CLAIM", phase="PREPARED"
    )
    resolved = common.journal_records(
        run, effect_type="TRACKER_CLAIM", phase="RESOLUTION"
    )
    assert len(prepared) == 1 and len(resolved) == 1
    assert resolved[0]["status"] == do.APPLIED
    assert resolved[0]["error"] is None
    assert resolved[0]["operation_id"] == prepared[0]["operation_id"]
    assert resolved[0]["attempt_id"] == "attempt-001"


def test_marker_is_written_before_the_mutation(run):
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    order = fake.profiles
    assert order.index("append_marker_note") < order.index("claim_exact")
    assert order[-1] == "issue_get", "the effect must be verified by a fresh readback"


def test_create_marker_follows_the_effect(run, tmp_path):
    new_id = "scc-new-1"
    operation = do.DirectOperation(
        caller_key="create/scc-new-1",
        effect_type="TRACKER_CREATE",
        target_identity=f"bd://issue/{new_id}",
        issue_id=new_id,
        ownership_epoch=None,
        arguments={
            "issue_id": new_id,
            "title": "New lane",
            "issue_type": "task",
            "priority": 2,
        },
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": new_id},
            intended={"id": new_id},
            prestate=do.ABSENT,
        ),
    )
    created = {"id": new_id, "status": "open"}
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [{"issues": []}, created],
            "create_exact": created,
            "append_marker_note": {"ok": True},
        },
        allowed=["issue_comments", "issue_get", "create_exact", "append_marker_note"],
    )
    result = do.execute(context(run), operation, actor="parent", runner=fake)

    assert result.status == do.APPLIED
    order = fake.profiles
    assert order.index("create_exact") < order.index("append_marker_note")


def test_actor_is_supplied_by_the_runtime_not_the_caller(run):
    with pytest.raises(do.DirectOperationError) as excinfo:
        do.DirectOperation(
            caller_key="claim/x",
            effect_type="TRACKER_CLAIM",
            target_identity="bd://issue/x",
            issue_id=ISSUE,
            ownership_epoch=None,
            arguments={"issue_id": ISSUE, "actor": "impostor"},
            readback=do.Readback("issue_get", {"issue_id": ISSUE}, {}, {}),
        )
    assert excinfo.value.code == "DIRECT_OPERATION_ACTOR_NOT_CALLER_SUPPLIED"


def test_unknown_effect_type_is_refused_at_construction():
    with pytest.raises(do.DirectOperationError) as excinfo:
        do.DirectOperation(
            caller_key="k",
            effect_type="TRACKER_DELETE_EVERYTHING",
            target_identity="bd://issue/x",
            issue_id=ISSUE,
            ownership_epoch=None,
            arguments={},
            readback=do.Readback("issue_get", {"issue_id": ISSUE}, {}, {}),
        )
    assert excinfo.value.code == "DIRECT_OPERATION_EFFECT_UNKNOWN"


# ---------------------------------------------------------------------------
# The probe, not the exit code, decides
# ---------------------------------------------------------------------------


def test_successful_mutation_with_disagreeing_readback_is_conflict(run):
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, OTHER],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    assert result.status == do.CONFLICT
    assert result.dispatched is True
    resolved = common.journal_records(
        run, effect_type="TRACKER_CLAIM", phase="RESOLUTION"
    )
    assert resolved[0]["status"] == do.CONFLICT
    assert resolved[0]["error"]["code"] == "DIRECT_OPERATION_CONFLICT"


def test_successful_mutation_with_unreadable_readback_is_unknown(run):
    fake = common.FakeNative(
        safe_bd,
        responses={
            "issue_comments": comments(),
            "issue_get": [OPEN, common.FakeNative(safe_bd).error("issue_get")],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    assert result.status == do.UNKNOWN
    assert result.classification == do.INSUFFICIENT_OBSERVATION


# ---------------------------------------------------------------------------
# Gates that must not reach the tracker at all
# ---------------------------------------------------------------------------


def test_already_intended_state_converges_without_dispatch(run):
    # ``claim_exact`` is omitted from ``allowed``: a dispatch would raise.
    fake = native(
        {"issue_comments": comments(), "issue_get": CLAIMED},
        allowed=["issue_comments", "issue_get"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    assert result.status == do.APPLIED
    assert result.dispatched is False
    assert fake.count("claim_exact") == 0
    assert fake.count("append_marker_note") == 0


def test_conflicting_prestate_refuses_without_dispatch(run):
    fake = native(
        {"issue_comments": comments(), "issue_get": OTHER},
        allowed=["issue_comments", "issue_get"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    assert result.status == do.CONFLICT
    assert result.dispatched is False
    assert fake.count("claim_exact") == 0


def test_unreadable_prestate_refuses_without_dispatch(run):
    probe = common.FakeNative(safe_bd)
    fake = native(
        {"issue_comments": comments(), "issue_get": probe.error("issue_get")},
        allowed=["issue_comments", "issue_get"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    assert result.status == do.UNKNOWN
    assert result.dispatched is False
    assert fake.count("claim_exact") == 0


def test_changed_request_under_the_same_caller_key_conflicts(run):
    first = claim_operation(run)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    assert do.execute(context(run), first, actor="parent", runner=fake).status == (
        do.APPLIED
    )

    # Same caller key, different intended outcome: nothing may reach the tracker.
    changed = do.DirectOperation(
        caller_key=first.caller_key,
        effect_type="TRACKER_CLAIM",
        target_identity=f"bd://issue/{ISSUE}",
        issue_id=ISSUE,
        ownership_epoch=run["epochs"][ISSUE],
        arguments={"issue_id": ISSUE},
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": ISSUE},
            intended={"status": "in_progress", "assignee": "a-different-worker"},
            prestate={"status": "open"},
        ),
    )
    strict = native({}, allowed=[])
    result = do.execute(context(run), changed, actor="parent", runner=strict)

    assert result.status == do.CONFLICT
    assert result.error_code == "DIRECT_OPERATION_INTENT_CONFLICT"
    assert strict.calls == []


def test_identical_retry_after_applied_does_not_redispatch(run):
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    strict = native({}, allowed=[])
    again = do.execute(
        context(run), claim_operation(run), actor="parent", runner=strict
    )

    assert again.status == do.APPLIED
    assert again.dispatched is False
    assert strict.calls == []
    assert (
        len(
            common.journal_records(run, effect_type="TRACKER_CLAIM", phase="RESOLUTION")
        )
        == 1
    )


# ---------------------------------------------------------------------------
# AC-T12-002: unknown close/create/edge causality never blindly replays
# ---------------------------------------------------------------------------


def _first_close_attempt_unknown(run):
    """Drive one close to UNKNOWN with its marker successfully written."""
    probe = common.FakeNative(safe_bd)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [CLAIMED, probe.error("issue_get")],
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    result = do.execute(context(run), close_operation(run), actor="parent", runner=fake)
    assert result.status == do.UNKNOWN
    return result


def test_unknown_close_with_marker_present_never_replays(run):
    _first_close_attempt_unknown(run)
    marker = marker_note(run, close_operation(run))

    # ``close_exact`` is denied: replaying would raise instead of passing.
    fake = native(
        {"issue_comments": comments(marker), "issue_get": CLAIMED},
        allowed=["issue_comments", "issue_get"],
    )
    second = do.execute(context(run), close_operation(run), actor="parent", runner=fake)

    assert second.status == do.UNKNOWN
    assert second.dispatched is False
    assert second.error_code == "DIRECT_OPERATION_AMBIGUOUS_CAUSALITY"
    assert fake.count("close_exact") == 0
    assert second.attempt_id == "attempt-002"


def test_unknown_close_without_marker_may_replay(run):
    _first_close_attempt_unknown(run)

    # No marker on the issue: the marker strictly precedes the close, so the
    # close cannot have run.  That is decisive, and the retry may proceed.
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [CLAIMED, CLOSED],
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    second = do.execute(context(run), close_operation(run), actor="parent", runner=fake)

    assert second.status == do.APPLIED
    assert second.dispatched is True


def test_unknown_close_with_unreadable_comments_never_replays(run):
    _first_close_attempt_unknown(run)
    probe = common.FakeNative(safe_bd)
    fake = native(
        {
            "issue_comments": probe.error("issue_comments"),
            "issue_get": CLAIMED,
        },
        allowed=["issue_comments", "issue_get"],
    )
    second = do.execute(context(run), close_operation(run), actor="parent", runner=fake)

    assert second.status == do.UNKNOWN
    assert second.error_code == "DIRECT_OPERATION_REPLAY_BLOCKED"
    assert fake.count("close_exact") == 0


def test_unknown_close_already_closed_converges_without_replay(run):
    _first_close_attempt_unknown(run)
    marker = marker_note(run, close_operation(run))
    fake = native(
        {"issue_comments": comments(marker), "issue_get": CLOSED},
        allowed=["issue_comments", "issue_get"],
    )
    second = do.execute(context(run), close_operation(run), actor="parent", runner=fake)

    assert second.status == do.APPLIED
    assert second.dispatched is False


def test_unknown_claim_replays_because_claims_are_not_destructive(run):
    probe = common.FakeNative(safe_bd)
    first = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, probe.error("issue_get")],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    assert (
        do.execute(
            context(run), claim_operation(run), actor="parent", runner=first
        ).status
        == do.UNKNOWN
    )

    marker = marker_note(run, claim_operation(run))
    second = native(
        {
            "issue_comments": comments(marker),
            "issue_get": [OPEN, CLAIMED],
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "claim_exact"],
    )
    result = do.execute(
        context(run), claim_operation(run), actor="parent", runner=second
    )

    assert result.status == do.APPLIED
    assert result.dispatched is True
    assert second.count("append_marker_note") == 0, "marker already present"


def test_guarded_effect_refuses_to_dispatch_without_its_marker(run):
    probe = common.FakeNative(safe_bd)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": CLAIMED,
            "append_marker_note": probe.error("append_marker_note"),
        },
        allowed=["issue_comments", "issue_get", "append_marker_note"],
    )
    result = do.execute(context(run), close_operation(run), actor="parent", runner=fake)

    assert result.status == do.NOT_APPLIED
    assert result.dispatched is False
    assert result.error_code == "DIRECT_OPERATION_MARKER_UNAVAILABLE"
    assert fake.count("close_exact") == 0


# ---------------------------------------------------------------------------
# Attempts, crash recovery, evidence
# ---------------------------------------------------------------------------


def test_each_attempt_gets_a_distinct_operation_id(run):
    first = _first_close_attempt_unknown(run)
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [CLAIMED, CLOSED],
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    second = do.execute(context(run), close_operation(run), actor="parent", runner=fake)

    assert first.operation_id != second.operation_id
    assert (first.attempt_id, second.attempt_id) == ("attempt-001", "attempt-002")
    # Both attempts share one frozen intent; only the attempt number differs.
    prepared = common.journal_records(
        run, effect_type="TRACKER_CLOSE", phase="PREPARED"
    )
    assert len({r["immutable_input_sha256"] for r in prepared}) == 1


def test_crash_after_prepared_resumes_the_same_operation(run):
    crash = common.crash_after("after_operation_prepared")
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": CLAIMED,
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    with pytest.raises(crash.Crash):
        do.execute(
            context(run, crash_hook=crash),
            close_operation(run),
            actor="parent",
            runner=fake,
        )

    dangling = common.journal_records(
        run, effect_type="TRACKER_CLOSE", phase="PREPARED"
    )
    assert len(dangling) == 1
    assert not common.journal_records(
        run, effect_type="TRACKER_CLOSE", phase="RESOLUTION"
    )

    # The resumed attempt reuses the dangling PREPARED rather than opening a
    # second one, and treats the interrupted dispatch as ambiguous.
    marker = marker_note(run, close_operation(run))
    resume = native(
        {"issue_comments": comments(marker), "issue_get": CLAIMED},
        allowed=["issue_comments", "issue_get"],
    )
    result = do.execute(
        context(run), close_operation(run), actor="parent", runner=resume
    )

    assert result.operation_id == dangling[0]["operation_id"]
    assert result.status == do.UNKNOWN
    assert result.error_code == "DIRECT_OPERATION_AMBIGUOUS_CAUSALITY"
    assert (
        len(common.journal_records(run, effect_type="TRACKER_CLOSE", phase="PREPARED"))
        == 1
    )
    assert resume.count("close_exact") == 0


def test_resolution_carries_a_matching_evidence_file(run):
    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    result = do.execute(context(run), claim_operation(run), actor="parent", runner=fake)

    resolved = common.journal_records(
        run, effect_type="TRACKER_CLAIM", phase="RESOLUTION"
    )[0]
    evidence_path = run["run_directory"] / do.OPERATIONS_DIRNAME
    assert resolved["readback_evidence_path"] == str(result.evidence_path)
    assert result.evidence_path.parent == evidence_path

    raw = result.evidence_path.read_bytes()
    assert state.sha256_bytes(raw) == resolved["readback_evidence_sha256"]
    evidence = json.loads(raw)
    assert evidence["classification"] == do.INTENDED_EFFECT_PRESENT
    assert evidence["operation_id"] == result.operation_id
    assert evidence["observed"] == CLAIMED


def test_intent_file_is_frozen_under_a_stable_caller_key(run):
    ctx = context(run)
    operation = claim_operation(run)
    path, first_sha, changed = do._freeze_intent(ctx, operation, actor="parent")
    assert not changed
    again_path, again_sha, changed_again = do._freeze_intent(
        ctx, operation, actor="parent"
    )
    assert (again_path, again_sha, changed_again) == (path, first_sha, False)

    payload = json.loads(path.read_bytes())
    assert payload["caller_key"] == operation.caller_key
    assert payload["actor"] == "parent"
    assert "select" not in payload["readback"]


def test_resolution_event_rejects_a_status_outside_the_taxonomy():
    with pytest.raises(do.DirectOperationError) as excinfo:
        do.resolution_event(
            {"phase": "PREPARED"},
            status="MAYBE",
            observed_post_state_sha256=None,
            readback_evidence_path=None,
            readback_evidence_sha256=None,
            timestamp=common.now(),
        )
    assert excinfo.value.code == "OPERATION_STATUS_INVALID"


def test_probe_result_rejects_a_classification_outside_the_taxonomy():
    request = do.tracker_probe(
        target_identity="bd://issue/x",
        expected_before_sha256=do.GENESIS,
        intended_after_sha256=do.GENESIS,
        descriptor=["beads"],
    )
    with pytest.raises(do.DirectOperationError) as excinfo:
        do.probe_result(
            request,
            classification="probably_fine",
            observed_identity=None,
            observed_sha256=None,
            observed_state="present",
            evidence_path="/tmp/evidence.json",
            evidence_sha256=do.GENESIS,
            observed_at=common.now(),
        )
    assert excinfo.value.code == "PROBE_CLASSIFICATION_INVALID"


def test_every_effect_type_maps_to_a_real_safe_bd_mutation_profile():
    for effect, profile in do.EFFECT_PROFILES.items():
        assert profile in safe_bd.PROFILES, effect
        assert safe_bd.PROFILES[profile].mutation is True, effect
