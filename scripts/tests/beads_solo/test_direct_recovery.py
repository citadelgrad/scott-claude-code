"""Direct-operation crash/retry fixtures for the solo lifecycle (scc-0pu.15 / t14).

Exercises ``direct_operation.execute`` (t12 / scc-0pu.13) for a single worker's
create/edge/note/close effects, adapting the proven precedent in the frozen
sibling suite ``beads_tracker/test_direct_operation.py`` with distinct fixture
identities and framing. Covers AC-T14-003: crash/retry fixtures for every
effect family converge to a stable typed outcome, or remain a recoverable
``UNKNOWN``, and never duplicate a non-idempotent native effect.

- ``TRACKER_CLOSE``: a crash right after the journal's ``PREPARED`` write
  resumes the same dangling operation instead of starting a new one; repeated
  attempts against the same caller-key/payload share one intent hash but get
  distinct per-attempt operation ids.
- ``TRACKER_CREATE``: an ``UNKNOWN`` attempt (the create dispatched, but the
  post-effect readback failed) converges to ``APPLIED`` on resume purely from
  observation, with zero duplicate ``create_exact`` dispatch.
- ``TRACKER_NOTE``: unlike the guarded effects, a prior ``UNKNOWN`` is safe to
  replay, because appending a marker note is not a destructive effect.
- ``TRACKER_DEPENDENCY_ADD``: a guarded effect whose marker is already present
  after an ``UNKNOWN`` attempt must never redispatch; the ambiguity is
  reported, not silently resolved.
"""

from __future__ import annotations

import pytest

from . import _common as common

m = common.modules("direct_operation")
do = m.direct_operation

ACTOR = "solo-recovery-worker"

CLOSE_ID = "solo-direct-close-1"
OPEN_CLOSE = {"id": CLOSE_ID, "status": "open", "assignee": None}
CLAIMED_CLOSE = {"id": CLOSE_ID, "status": "in_progress", "assignee": ACTOR}
CLOSED_CLOSE = {"id": CLOSE_ID, "status": "closed", "assignee": ACTOR}

CREATE_ID = "solo-direct-create-1"

NOTE_ID = "solo-direct-note-1"
NOTE_TEXT = "solo recovery fixture note"

DEPENDENT_ID = "solo-direct-dep-1"
PREREQ_ID = "solo-direct-prereq-1"


def context(run, *, crash_hook=None):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=crash_hook or m.coordinator_state.NOOP_HOOK,
    )


def native(responses, allowed):
    return common.FakeNative(m.safe_bd, responses=responses, allowed=allowed)


def make_run(tmp_path, *, run_id=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return common.make_run(
        m, repo, run_id=run_id, issue_ids=(CLOSE_ID, DEPENDENT_ID), actor=ACTOR
    )


def marker_note(run, operation):
    ctx = context(run)
    _path, sha, changed = do._freeze_intent(ctx, operation, actor=ACTOR)
    assert not changed
    return do.marker_text(
        run_id=run["run_id"], intent_sha256=sha, effect_type=operation.effect_type
    )


def close_operation(run, *, caller_key="close/solo-direct-close-1"):
    return do.DirectOperation(
        caller_key=caller_key,
        effect_type="TRACKER_CLOSE",
        target_identity=f"bd://issue/{CLOSE_ID}",
        issue_id=CLOSE_ID,
        ownership_epoch=run["epochs"].get(CLOSE_ID),
        arguments={"issue_id": CLOSE_ID, "reason": "solo recovery fixture"},
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": CLOSE_ID},
            intended={"status": "closed"},
            prestate={"status": "in_progress"},
        ),
    )


def create_operation(run, *, caller_key="create/solo-direct-create-1"):
    return do.DirectOperation(
        caller_key=caller_key,
        effect_type="TRACKER_CREATE",
        target_identity=f"bd://issue/{CREATE_ID}",
        issue_id=CREATE_ID,
        ownership_epoch=None,
        arguments={
            "issue_id": CREATE_ID,
            "title": "Solo direct create fixture",
            "issue_type": "task",
            "priority": 2,
        },
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": CREATE_ID},
            intended={"id": CREATE_ID},
            prestate=do.ABSENT,
        ),
    )


def note_operation(run, *, caller_key="note/solo-direct-note-1"):
    return do.DirectOperation(
        caller_key=caller_key,
        effect_type="TRACKER_NOTE",
        target_identity=f"bd://issue/{NOTE_ID}",
        issue_id=NOTE_ID,
        ownership_epoch=None,
        arguments={"issue_id": NOTE_ID, "content": NOTE_TEXT},
        readback=do.Readback(
            profile="issue_comments",
            arguments={"issue_id": NOTE_ID},
            intended={"present": True},
            prestate={"present": False},
            select=lambda data: {
                "present": any(
                    NOTE_TEXT in (item.get("text") or "") for item in (data or [])
                )
            },
        ),
    )


def dependency_add_operation(run, *, caller_key="dep-add/solo-direct-dep-1"):
    return do.DirectOperation(
        caller_key=caller_key,
        effect_type="TRACKER_DEPENDENCY_ADD",
        target_identity=f"bd://dependency/{DEPENDENT_ID}->{PREREQ_ID}",
        issue_id=DEPENDENT_ID,
        ownership_epoch=run["epochs"].get(DEPENDENT_ID),
        arguments={"dependent": DEPENDENT_ID, "prerequisite": PREREQ_ID},
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": DEPENDENT_ID},
            intended={"has_edge": True},
            prestate={"has_edge": False},
            select=lambda data: {
                "has_edge": PREREQ_ID in (data or {}).get("blocked_by", [])
            },
        ),
    )


def test_close_crash_after_prepared_resumes_the_same_dangling_operation(tmp_path):
    run = make_run(tmp_path)
    crash = common.crash_after("after_operation_prepared")
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": CLAIMED_CLOSE,
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED_CLOSE,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    with pytest.raises(crash.Crash):
        do.execute(
            context(run, crash_hook=crash),
            close_operation(run),
            actor=ACTOR,
            runner=fake,
        )

    dangling = common.journal_records(
        run, effect_type="TRACKER_CLOSE", phase="PREPARED"
    )
    assert len(dangling) == 1
    assert not common.journal_records(
        run, effect_type="TRACKER_CLOSE", phase="RESOLUTION"
    )

    marker = marker_note(run, close_operation(run))
    resume = native(
        {"issue_comments": common.comments(marker), "issue_get": CLAIMED_CLOSE},
        allowed=["issue_comments", "issue_get"],
    )
    result = do.execute(context(run), close_operation(run), actor=ACTOR, runner=resume)

    assert result.operation_id == dangling[0]["operation_id"]
    assert result.status == do.UNKNOWN
    assert result.error_code == "DIRECT_OPERATION_AMBIGUOUS_CAUSALITY"
    assert (
        len(common.journal_records(run, effect_type="TRACKER_CLOSE", phase="PREPARED"))
        == 1
    )
    assert resume.count("close_exact") == 0


def _first_close_attempt_unknown(run):
    probe = common.FakeNative(m.safe_bd)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [CLAIMED_CLOSE, probe.error("issue_get")],
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED_CLOSE,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    result = do.execute(context(run), close_operation(run), actor=ACTOR, runner=fake)
    assert result.status == do.UNKNOWN
    return result


def test_close_attempts_share_one_intent_but_get_distinct_operation_ids(tmp_path):
    run = make_run(tmp_path)
    first = _first_close_attempt_unknown(run)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [CLAIMED_CLOSE, CLOSED_CLOSE],
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED_CLOSE,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    second = do.execute(context(run), close_operation(run), actor=ACTOR, runner=fake)
    assert first.operation_id != second.operation_id
    assert (first.attempt_id, second.attempt_id) == ("attempt-001", "attempt-002")
    prepared = common.journal_records(
        run, effect_type="TRACKER_CLOSE", phase="PREPARED"
    )
    assert len({r["immutable_input_sha256"] for r in prepared}) == 1


def test_create_unknown_converges_to_applied_without_a_duplicate_create(tmp_path):
    run = make_run(tmp_path)
    probe = common.FakeNative(m.safe_bd)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [{"issues": []}, probe.error("issue_get")],
            "create_exact": {"id": CREATE_ID, "status": "open"},
            "append_marker_note": {"ok": True},
        },
        allowed=["issue_comments", "issue_get", "create_exact", "append_marker_note"],
    )
    first = do.execute(context(run), create_operation(run), actor=ACTOR, runner=fake)
    assert first.status == do.UNKNOWN
    assert first.dispatched is True
    assert fake.count("create_exact") == 1

    resume = native(
        {
            "issue_comments": common.comments(marker_note(run, create_operation(run))),
            "issue_get": {"id": CREATE_ID, "status": "open"},
        },
        allowed=["issue_comments", "issue_get"],
    )
    second = do.execute(context(run), create_operation(run), actor=ACTOR, runner=resume)
    assert second.status == do.APPLIED
    assert second.dispatched is False
    assert resume.count("create_exact") == 0


def test_note_unknown_may_safely_replay_because_notes_are_not_destructive(tmp_path):
    run = make_run(tmp_path)
    probe = common.FakeNative(m.safe_bd)
    fake = native(
        {
            "issue_comments": [[], [], probe.error("issue_comments")],
            "append_marker_note": [{"ok": True}, {"ok": True}],
        },
        allowed=["issue_comments", "append_marker_note"],
    )
    first = do.execute(context(run), note_operation(run), actor=ACTOR, runner=fake)
    assert first.status == do.UNKNOWN
    assert first.dispatched is True
    assert fake.count("append_marker_note") == 2

    marker = marker_note(run, note_operation(run))
    resume = native(
        {
            "issue_comments": [
                [{"text": marker}],
                [],
                [{"text": NOTE_TEXT}],
            ],
            "append_marker_note": [{"ok": True}],
        },
        allowed=["issue_comments", "append_marker_note"],
    )
    second = do.execute(context(run), note_operation(run), actor=ACTOR, runner=resume)
    assert second.status == do.APPLIED
    assert second.dispatched is True
    assert resume.count("append_marker_note") == 1


def test_dependency_add_unknown_with_marker_present_never_replays(tmp_path):
    run = make_run(tmp_path)
    probe = common.FakeNative(m.safe_bd)
    fake = native(
        {
            "issue_comments": common.comments(),
            "issue_get": [
                {"id": DEPENDENT_ID, "blocked_by": []},
                probe.error("issue_get"),
            ],
            "append_marker_note": {"ok": True},
            "dependency_add_exact": {"id": DEPENDENT_ID, "blocked_by": [PREREQ_ID]},
        },
        allowed=[
            "issue_comments",
            "issue_get",
            "append_marker_note",
            "dependency_add_exact",
        ],
    )
    first = do.execute(
        context(run), dependency_add_operation(run), actor=ACTOR, runner=fake
    )
    assert first.status == do.UNKNOWN
    assert first.dispatched is True
    assert fake.count("dependency_add_exact") == 1

    marker = marker_note(run, dependency_add_operation(run))
    resume = native(
        {
            "issue_comments": common.comments(marker),
            "issue_get": {"id": DEPENDENT_ID, "blocked_by": []},
        },
        allowed=["issue_comments", "issue_get"],
    )
    second = do.execute(
        context(run), dependency_add_operation(run), actor=ACTOR, runner=resume
    )
    assert second.status == do.UNKNOWN
    assert second.dispatched is False
    assert second.error_code == "DIRECT_OPERATION_AMBIGUOUS_CAUSALITY"
    assert resume.count("dependency_add_exact") == 0
