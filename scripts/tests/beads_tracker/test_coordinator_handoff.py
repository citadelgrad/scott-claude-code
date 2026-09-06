"""AC-T12-006: the acceptance gate for durable, out-of-process executors.

``coordinator_handoff.accept`` guards eight independent conditions -- unknown
launch, handoff-record mismatch, executor identity mismatch, ownership epoch
mismatch, artifact mismatch, an unauthorized Beads mutation by the child,
expiry, and duplicate/late delivery -- before a durable executor's self
reported result is even handed to ``direct_operation.execute`` for the
parent's own independent verification. Every guard test proves its blocking
condition two ways at once: the returned error code, and -- via the
``allowed=[...]`` command-denial spy on :class:`common.FakeNative` -- that the
blocked result never touched the tracker at all.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from . import _common as common

m = common.modules("direct_operation", "coordinator_handoff")
do = m.direct_operation
ch = m.coordinator_handoff
state = m.coordinator_state
safe_bd = m.safe_bd


ISSUE = "scc-lane-a"
GHOST_ISSUE = "scc-ghost"
OPEN = {"id": ISSUE, "status": "open", "assignee": None}
CLAIMED = {"id": ISSUE, "status": "in_progress", "assignee": "parent"}
OTHER = {"id": ISSUE, "status": "in_progress", "assignee": "someone-else"}

EXECUTOR_IDENTITY = "pas-executor-0001"
HANDOFF_ID = "a" * 64

FAR_FUTURE = "2999-01-01T00:00:00.000000Z"
PAST_EXPIRY = "2020-01-01T00:00:00.000000Z"
AFTER_PAST_EXPIRY = "2030-01-01T00:00:00.000000Z"


# ---------------------------------------------------------------------------
# Shared builders, mirroring test_direct_operation.py's own idioms
# ---------------------------------------------------------------------------


def context(run, *, crash_hook=None):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=crash_hook or state.NOOP_HOOK,
    )


def accept_operation(run, *, caller_key="handoff-accept/scc-lane-a") -> object:
    """The tracker mutation the parent verifies once every guard has passed.

    Any real effect type would do here -- the guards never look at it. A
    claim on the scoped issue keeps the fixture identical in shape to
    ``test_direct_operation.py``'s own ``claim_operation``.
    """
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


def native(responses, allowed):
    return common.FakeNative(safe_bd, responses=responses, allowed=allowed)


def comments(*bodies):
    """Script one ``issue_comments`` reply carrying the given comment bodies.

    ``FakeNative`` reads a list value as a queue of successive replies, so a
    reply that is itself a list has to be wrapped once. Defined locally
    rather than imported from ``_common.py``, per suite convention.
    """
    return [[{"text": body} for body in bodies]]


def handoff_sha(handoff) -> str:
    return state.sha256_bytes(state.canonical_bytes(dict(handoff)))


def write_artifact(run, data: bytes) -> Path:
    """Write a real executor artifact the way the accept-gate itself would trust.

    Must go through ``atomic_write`` (0o600, owner-directory validated) --
    never a plain ``Path.write_bytes()`` -- or ``_real_artifact_identity``'s
    ``validate_owner_file`` check refuses the file outright, which a naive
    fixture would misreport as a genuine artifact mismatch.
    """
    path = run["run_directory"] / "artifact.bin"
    state.atomic_write(
        path, data, root=run["run_directory"], max_bytes=ch.MAX_ARTIFACT_BYTES
    )
    return path


def make_handoff(
    run,
    *,
    held_ownership=None,
    issue_ids=(ISSUE,),
    executor_identity=EXECUTOR_IDENTITY,
    isolation_target=None,
    expires_at=FAR_FUTURE,
    handoff_id=HANDOFF_ID,
    **overrides,
):
    if held_ownership is None:
        held_ownership = [
            {"issue_id": issue_id, "epoch": run["epochs"][issue_id]}
            for issue_id in issue_ids
        ]
    handoff = {
        "schema_version": "beads.durable-handoff.v1",
        "run_id": run["run_id"],
        "handoff_id": handoff_id,
        "root_issue_id": run["root_issue_id"],
        "scope_issue_ids": [entry["issue_id"] for entry in held_ownership],
        "held_ownership": held_ownership,
        "tracker_sha256": "0" * 64,
        "dependency_sha256": "0" * 64,
        "ready_front_sha256": "0" * 64,
        "authority_sha256": "0" * 64,
        "base_commit": "0" * 40,
        "isolation_target": isolation_target or str(run["run_directory"]),
        "unresolved_gates": [],
        "stages": ["execute"],
        "resume_topology": ["execute"],
        "executor_type": "pas",
        "executor_identity": executor_identity,
        "beads_authority": "readonly",
        "allowed_effects": {
            "repository_write": True,
            "local_commit": False,
            "git_remote": False,
            "dolt_remote": False,
            "external_side_effects": False,
            "beads_mutation": False,
        },
        "required_result_schema": "/durable-executor-result-v1.schema.json",
        "created_at": common.now(),
        "expires_at": expires_at,
    }
    handoff.update(overrides)
    return handoff


def make_result(
    handoff,
    *,
    handoff_sha256,
    artifact_path,
    artifact_sha256,
    artifact_size,
    executor_identity=None,
    issues=None,
    status="completed",
    beads_mutated=False,
    **overrides,
):
    result = {
        "schema_version": "beads.durable-executor-result.v1",
        "handoff_id": handoff["handoff_id"],
        "run_id": handoff["run_id"],
        "executor_identity": (
            handoff["executor_identity"]
            if executor_identity is None
            else executor_identity
        ),
        "issues": [
            dict(entry)
            for entry in (issues if issues is not None else handoff["held_ownership"])
        ],
        "handoff_sha256": handoff_sha256,
        "status": status,
        "artifact": {
            "type": "patch",
            "path": str(artifact_path),
            "size_bytes": artifact_size,
            "sha256": artifact_sha256,
        },
        "commands": [],
        "blockers": [],
        "skipped_checks": [],
        "residual_risks": [],
        "beads_mutated": beads_mutated,
    }
    result.update(overrides)
    return result


def full_valid_pair(
    run, *, artifact_bytes=b"executor-artifact-payload", **handoff_overrides
):
    """Build a handoff/result pair that clears every guard, for tampering.

    Every guard-specific test starts here and mutates exactly one field, so
    every guard *earlier* in the sequential chain is guaranteed to still pass
    and the test proves only the guard under test.
    """
    handoff = make_handoff(run, **handoff_overrides)
    handoff_sha256 = handoff_sha(handoff)
    artifact_path = write_artifact(run, artifact_bytes)
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    result = make_result(
        handoff,
        handoff_sha256=handoff_sha256,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        artifact_size=len(artifact_bytes),
    )
    return handoff, handoff_sha256, result


@pytest.fixture()
def run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return common.make_run(m, repo, issue_ids=[ISSUE])


# ---------------------------------------------------------------------------
# Guard 1: unknown launch
# ---------------------------------------------------------------------------


def test_unknown_launch_blocks_acceptance(run):
    handoff, _, result = full_valid_pair(run)
    # Deliberately never launched -- this run has no record of issuing it.
    fake = native({}, allowed=[])

    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_UNKNOWN_LAUNCH"
    assert outcome.status == do.CONFLICT
    assert outcome.operation_result is None
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Guard 2: handoff-record mismatch
# ---------------------------------------------------------------------------


def test_record_mismatch_blocks_acceptance(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    tampered = dict(result)
    tampered["handoff_sha256"] = "f" * 64  # no longer binds to the launched record
    fake = native({}, allowed=[])

    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=tampered,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_RECORD_MISMATCH"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Guard 3: executor identity mismatch
# ---------------------------------------------------------------------------


def test_identity_mismatch_blocks_acceptance(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    tampered = dict(result)
    tampered["executor_identity"] = "impostor-executor"
    fake = native({}, allowed=[])

    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=tampered,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_EXECUTOR_IDENTITY_MISMATCH"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Guard 4: ownership epoch mismatch
# ---------------------------------------------------------------------------


def test_epoch_mismatch_blocks_acceptance_when_the_epoch_moved(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    # The epoch moves *while the child ran*: a lease expiring and being
    # reacquired. The handoff still pins the old epoch; the live store does
    # not agree any more. Neither the handoff nor the result is touched --
    # only a live check against the ownership store can catch this.
    run["ownership"].release(
        ISSUE,
        run["run_directory"],
        epoch=run["epochs"][ISSUE],
        operation_id="1" * 64,
        now=datetime.now(timezone.utc),
    )
    reacquired = run["ownership"].acquire(
        issue_id=ISSUE,
        actor=run["actor"],
        run_directory=run["run_directory"],
        tracker_state_sha256="0" * 64,
        operation_id="2" * 64,
        now=datetime.now(timezone.utc),
    )
    assert reacquired.disposition == "held"
    assert reacquired.record["epoch"] != run["epochs"][ISSUE]

    fake = native({}, allowed=[])
    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_EPOCH_MISMATCH"
    assert fake.calls == []


def test_epoch_mismatch_blocks_acceptance_for_an_issue_never_held(run):
    # A handoff can claim to hold an issue whose ownership directory this run
    # never created at all. ``inspect_readonly`` raises rather than returning
    # a disposition -- the guard must fail closed on that too, exactly as it
    # does for a visibly moved epoch.
    handoff, _, result = full_valid_pair(
        run,
        held_ownership=[{"issue_id": GHOST_ISSUE, "epoch": 7}],
    )
    ch.launch(context(run), handoff=handoff, actor="parent")

    fake = native({}, allowed=[])
    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_EPOCH_MISMATCH"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Guard 5: artifact mismatch
# ---------------------------------------------------------------------------


def test_artifact_mismatch_blocks_acceptance(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    tampered = dict(result)
    tampered["artifact"] = dict(result["artifact"])
    # The file on disk is untouched; only the claimed digest is wrong -- this
    # is exactly the "trusted by name/size, not content" failure mode the
    # guard exists to catch.
    tampered["artifact"]["sha256"] = "e" * 64
    fake = native({}, allowed=[])

    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=tampered,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_ARTIFACT_MISMATCH"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Guard 6: unauthorized Beads mutation by the child
# ---------------------------------------------------------------------------


def test_beads_mutated_true_fails_schema_validation_before_any_guard(run):
    # The schema constrains beads_mutated to const:false, and both handoff
    # and result are validated before the duplicate/unknown-launch check --
    # let alone any of the seven guards -- ever runs. A result that lies
    # about this never reaches _guard_beads_mutation at all.
    handoff, _, result = full_valid_pair(run)
    tampered = dict(result)
    tampered["beads_mutated"] = True
    fake = native({}, allowed=[])

    with pytest.raises(ch.schema_runtime.ValidationFailure):
        ch.accept(
            context(run),
            handoff=handoff,
            result=tampered,
            operation=accept_operation(run),
            actor="parent",
            ownership=run["ownership"],
            runner=fake,
        )

    assert fake.calls == []


def test_guard_beads_mutation_fails_closed_on_anything_but_explicit_false():
    # Defense-in-depth unit test of the guard itself, for a caller that
    # bypassed schema validation: only an explicit False passes.
    assert ch._guard_beads_mutation({"beads_mutated": False}) is None
    assert (
        ch._guard_beads_mutation({"beads_mutated": None})
        == "HANDOFF_BEADS_MUTATION_DETECTED"
    )
    assert (
        ch._guard_beads_mutation({"beads_mutated": True})
        == "HANDOFF_BEADS_MUTATION_DETECTED"
    )
    assert ch._guard_beads_mutation({}) == "HANDOFF_BEADS_MUTATION_DETECTED"


# ---------------------------------------------------------------------------
# Guard 7: expiry
# ---------------------------------------------------------------------------


def test_expired_blocks_acceptance(run):
    handoff, _, result = full_valid_pair(run, expires_at=PAST_EXPIRY)
    ch.launch(context(run), handoff=handoff, actor="parent")

    fake = native({}, allowed=[])
    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
        now=lambda: AFTER_PAST_EXPIRY,
    )

    assert outcome.guard_passed is False
    assert outcome.error_code == "HANDOFF_EXPIRED"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Structural: duplicate / late delivery -- checked before any guard runs
# ---------------------------------------------------------------------------


def test_duplicate_result_blocks_a_second_acceptance_of_the_same_handoff(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    first_native = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    first = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=first_native,
    )
    assert first.status == do.APPLIED
    assert first.guard_passed is True

    # A second result for a handoff already accepted, whatever it says, is
    # refused before any guard is evaluated and never reaches the tracker.
    second_native = native({}, allowed=[])
    second = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=second_native,
    )

    assert second.guard_passed is False
    assert second.error_code == "HANDOFF_RESULT_DUPLICATE"
    assert second.status == do.CONFLICT
    assert second.operation_result is None
    assert second_native.calls == []


def test_late_result_blocks_a_second_acceptance_after_a_guard_already_refused(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    refused = dict(result)
    refused["executor_identity"] = "impostor-executor"
    first_native = native({}, allowed=[])
    first = ch.accept(
        context(run),
        handoff=handoff,
        result=refused,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=first_native,
    )
    assert first.guard_passed is False
    assert first.error_code == "HANDOFF_EXECUTOR_IDENTITY_MISMATCH"

    # This handoff's one decision is already made (a refusal). A later
    # result -- even a perfectly valid one -- is late, not a fresh chance.
    second_native = native({}, allowed=[])
    second = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=second_native,
    )

    assert second.guard_passed is False
    assert second.error_code == "HANDOFF_RESULT_LATE"
    assert second.status == do.CONFLICT
    assert second.operation_result is None
    assert second_native.calls == []


# ---------------------------------------------------------------------------
# The valid path: guards passing is a fair hearing, never a verdict
# ---------------------------------------------------------------------------


def test_valid_result_reenters_parent_verification_and_is_applied(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is True
    assert outcome.error_code is None
    assert outcome.status == do.APPLIED
    assert outcome.operation_result is not None
    assert outcome.operation_result.dispatched is True
    assert outcome.operation_result.status == do.APPLIED


def test_valid_result_with_disagreeing_parent_readback_is_conflict_not_applied(run):
    # Every guard passes -- the child's own report even looks successful
    # (claim_exact returns CLAIMED) -- but the parent's own independent
    # readback sees someone else holding the issue. A child's claim of
    # success is evidence, never a verdict.
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    fake = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, OTHER],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    outcome = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=fake,
    )

    assert outcome.guard_passed is True
    assert outcome.status == do.CONFLICT
    # No guard fired inside direct_operation.execute() itself -- the dispatch
    # happened and only the post-effect probe disagreed -- so the result's
    # own error_code stays unset; the CONFLICT label lives on the journalled
    # resolution's defaulted error code instead.
    assert outcome.error_code is None
    assert outcome.operation_result.dispatched is True
    assert (
        outcome.operation_result.resolution["error"]["code"]
        == "DIRECT_OPERATION_CONFLICT"
    )


# ---------------------------------------------------------------------------
# Resumability across a crash
# ---------------------------------------------------------------------------


def test_crash_after_launch_prepared_resumes_the_same_launch(run):
    handoff, _, _result = full_valid_pair(run)
    crash = common.crash_after("after_handoff_launch_prepared")

    with pytest.raises(crash.Crash):
        ch.launch(context(run, crash_hook=crash), handoff=handoff, actor="parent")

    dangling = common.journal_records(
        run, effect_type=ch.LAUNCH_EFFECT_TYPE, phase="PREPARED"
    )
    assert len(dangling) == 1
    assert not common.journal_records(
        run, effect_type=ch.LAUNCH_EFFECT_TYPE, phase="RESOLUTION"
    )

    resumed = ch.launch(context(run), handoff=handoff, actor="parent")

    assert resumed.operation_id == dangling[0]["operation_id"]
    assert resumed.status == do.APPLIED
    assert resumed.already_launched is False
    assert (
        len(
            common.journal_records(
                run, effect_type=ch.LAUNCH_EFFECT_TYPE, phase="PREPARED"
            )
        )
        == 1
    )
    assert (
        len(
            common.journal_records(
                run, effect_type=ch.LAUNCH_EFFECT_TYPE, phase="RESOLUTION"
            )
        )
        == 1
    )


def test_launch_is_idempotent_a_second_call_reports_already_launched(run):
    handoff, _, _result = full_valid_pair(run)

    first = ch.launch(context(run), handoff=handoff, actor="parent")
    second = ch.launch(context(run), handoff=handoff, actor="parent")

    assert first.already_launched is False
    assert second.already_launched is True
    assert second.operation_id == first.operation_id
    assert second.status == do.APPLIED
    assert (
        len(
            common.journal_records(
                run, effect_type=ch.LAUNCH_EFFECT_TYPE, phase="PREPARED"
            )
        )
        == 1
    )
    assert (
        len(
            common.journal_records(
                run, effect_type=ch.LAUNCH_EFFECT_TYPE, phase="RESOLUTION"
            )
        )
        == 1
    )


def test_crash_after_accept_prepared_resumes_the_same_accept(run):
    handoff, _, result = full_valid_pair(run)
    ch.launch(context(run), handoff=handoff, actor="parent")

    crash = common.crash_after("after_handoff_accept_prepared")
    # The crash fires before any guard result is journalled and long before
    # the delegated dispatch -- nothing should touch the tracker yet.
    crash_native = native({}, allowed=[])

    with pytest.raises(crash.Crash):
        ch.accept(
            context(run, crash_hook=crash),
            handoff=handoff,
            result=result,
            operation=accept_operation(run),
            actor="parent",
            ownership=run["ownership"],
            runner=crash_native,
        )

    assert crash_native.calls == []
    dangling = common.journal_records(
        run, effect_type=ch.ACCEPT_EFFECT_TYPE, phase="PREPARED"
    )
    assert len(dangling) == 1
    assert not common.journal_records(
        run, effect_type=ch.ACCEPT_EFFECT_TYPE, phase="RESOLUTION"
    )

    resume_native = native(
        {
            "issue_comments": comments(),
            "issue_get": [OPEN, CLAIMED],
            "append_marker_note": {"ok": True},
            "claim_exact": CLAIMED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "claim_exact"],
    )
    resumed = ch.accept(
        context(run),
        handoff=handoff,
        result=result,
        operation=accept_operation(run),
        actor="parent",
        ownership=run["ownership"],
        runner=resume_native,
    )

    assert resumed.accept_prepared["operation_id"] == dangling[0]["operation_id"]
    assert resumed.guard_passed is True
    assert resumed.status == do.APPLIED
    assert (
        len(
            common.journal_records(
                run, effect_type=ch.ACCEPT_EFFECT_TYPE, phase="PREPARED"
            )
        )
        == 1
    )
    assert (
        len(
            common.journal_records(
                run, effect_type=ch.ACCEPT_EFFECT_TYPE, phase="RESOLUTION"
            )
        )
        == 1
    )
