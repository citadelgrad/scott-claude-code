"""Authority-aware finish fixtures for the solo lifecycle (scc-0pu.15 / t14).

Exercises ``protected_action.py`` (t12 / scc-0pu.13) for a single worker's
protected-effect gate, adapting the proven precedent in the frozen sibling
suite ``beads_tracker/test_protected_action.py`` with distinct fixture
identities and framing, and ties it to ``direct_operation.execute`` (also
t12) to exercise the finish taxonomy in
``skills/beads/references/solo-execution.md`` section 6 ("Finish under
authority"): "tracker closed locally" is a plain direct-operation effect a
solo worker can complete on its own; "tracker synchronized to a Dolt remote"
is a *protected* effect that only becomes real with an independently
observed, agreeing readback — never from a parent-attested grant alone,
since ``dolt_remote`` is one of ``protected_action.NEVER_GRANTABLE_CLASSES``.
Covers:

- AC-T14-004 (receipt resolution): a matching receipt with an agreeing
  readback resolves APPLIED, a disagreeing readback resolves CONFLICT, an
  unreadable probe resolves UNKNOWN, every receipt-binding mismatch refuses
  before any dispatch, and an already-resolved operation refuses a second
  resolution outright.
- AC-T14-004 (remote authority absence): absent a resolved remote receipt, a
  Dolt-remote sync stays a dangling ``HUMAN_ACTION_REQUIRED`` intent with
  zero dispatch, and no grant — however parent-attested — can manufacture
  that remote completion in its place, while the issue's own local close
  still completes for real. A finish report bound to this evidence may claim
  only the local milestone, never the remote one.
"""

from __future__ import annotations

import inspect

import pytest

from . import _common as common

m = common.modules("direct_operation", "protected_action")
do = m.direct_operation
pa = m.protected_action
state = m.coordinator_state
# ``pa`` imports ``safe_output`` itself under the shared ``sys.path`` prelude,
# landing in ``sys.modules`` under the plain name — a different object from
# ``m.safe_output`` (loaded under a synthetic per-call name by
# ``common.modules()``). ``resolve_protected_action``'s
# ``except safe_output.SafeOutputError`` only recognises exceptions built
# from the class on *its own* import, so a fixture must raise
# ``pa.safe_output.SafeOutputError``, not ``m.safe_output``'s.
safe_output = pa.safe_output

ISSUE = "solo-finish-remote-1"
ACTOR = "solo-finish-worker"

CLAIMED = {"id": ISSUE, "status": "in_progress", "assignee": ACTOR}
CLOSED = {"id": ISSUE, "status": "closed", "assignee": ACTOR}

# ``target_sha256`` is the hash of whatever identity a *readback* must
# observe to count as the intended effect (see ``classify_observation``), so
# the "agreeing" scenario's fake probe reply must echo this exact text back
# for the hashes to line up.
OBSERVED_TARGET_TEXT = (
    "harness confirms the tracker state landed on the Dolt remote head"
)
TARGET_SHA = state.sha256_bytes(OBSERVED_TARGET_TEXT.encode("utf-8"))
PRECONDITION_SHA = state.sha256_bytes(b"solo-finish-precondition-prior-remote-head")
OTHER_SHA = state.sha256_bytes(b"solo-finish-neither-target-nor-precondition")


def context(run):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=m.coordinator_state.NOOP_HOOK,
    )


def prepare(run, *, precondition_sha256=PRECONDITION_SHA, target_sha256=TARGET_SHA):
    return pa.prepare_protected_action(
        context(run),
        effect_type="TRACKER_DOLT_PUSH",
        target_identity="dolt://origin/main",
        target_sha256=target_sha256,
        precondition_sha256=precondition_sha256,
        summary="sync tracker state to the Dolt remote",
        # ``dolt_remote_head`` is not a value in recovery-probe-v1.schema.json's
        # ``probe_type`` enum; ``dolt_history`` is the closest Dolt-specific
        # probe kind the schema actually declares (mirroring the sibling
        # ``beads_tracker/test_protected_action.py`` fixture's use of
        # ``git_remote_ref`` for its analogous ``GIT_PUSH`` scenario).
        probe_type="dolt_history",
        probe_argv=["/usr/bin/dolt", "ls-remote", "origin", "main"],
        issue_id=ISSUE,
        ownership_epoch=run["epochs"][ISSUE],
    )


def receipt_for(prepared, pending_action, *, outcome="applied", **overrides):
    body = {
        "schema_version": "beads.harness-receipt.v1",
        "receipt_variant": "protected_harness_effect",
        "action_id": pending_action["action_id"],
        "operation_id": pending_action["operation_id"],
        "target_sha256": pending_action["target_sha256"],
        "action_sha256": pending_action["action_id"],
        "outcome": outcome,
        "observed_identity": "harness observed the dolt push landed"
        if outcome == "applied"
        else None,
        "observed_sha256": TARGET_SHA if outcome == "applied" else None,
        "evidence": {
            "path": "/tmp/dolt-push.log" if outcome == "applied" else None,
            "sha256": state.sha256_bytes(b"dolt-push-evidence")
            if outcome == "applied"
            else None,
            "summary": "harness ran the dolt push",
        },
        "harness": "current-harness",
        "provider": "manual",
        "version": "1.0.0",
        "timestamp": common.now(),
    }
    body.update(overrides)
    return body


def grant_for(
    *, authorized_operation_id, protected_class="design_decision", **overrides
):
    body = {
        "schema_version": "beads.approval-record.v1",
        "approval_id": state.sha256_bytes(
            f"solo-finish-approval/{authorized_operation_id}".encode()
        ),
        "run_id": common.make_run_id(seed=141),
        "issue_id": ISSUE,
        "ownership_epoch": 1,
        "provenance_class": "parent_attested",
        "approver_identity": "parent",
        "evidence_source": "parent-transcript",
        "evidence_sha256": state.sha256_bytes(
            b"solo-finish-parent-transcript-evidence"
        ),
        "authorized_operation_id": authorized_operation_id,
        "decision_type": protected_class,
        "target_sha256": TARGET_SHA,
        "precondition_sha256": PRECONDITION_SHA,
        "scope": [protected_class],
        "one_shot": True,
        "granted_at": common.now(),
        "expiry_policy": "no_expiry",
        "expires_at": None,
        "revocation_channel": "parent-transcript",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return common.make_run(m, repo, issue_ids=[ISSUE], actor=ACTOR)


# ---------------------------------------------------------------------------
# AC-T14-004 (1): preparing never dispatches, and always demands a human.
# ---------------------------------------------------------------------------


def test_prepare_returns_human_action_required_with_no_dispatch(run):
    # No ``runner`` parameter exists on this function at all: there is no
    # code path through which it could dispatch a probe or an effect.
    assert "runner" not in inspect.signature(pa.prepare_protected_action).parameters

    result = prepare(run)

    assert result["status"] == pa.HUMAN_ACTION_REQUIRED
    prepared = common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="PREPARED"
    )
    resolved = common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="RESOLUTION"
    )
    assert len(prepared) == 1
    assert not resolved


# ---------------------------------------------------------------------------
# AC-T14-004 (2): a receipt cannot resolve the same operation twice.
# ---------------------------------------------------------------------------


def test_resolving_the_same_operation_twice_is_refused(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    runner = common.FakeRunner(replies=[common_probe_reply(OBSERVED_TARGET_TEXT)])

    first = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )
    assert first["status"] == pa.APPLIED
    assert runner.count == 1

    with pytest.raises(pa.ProtectedActionError) as excinfo:
        pa.resolve_protected_action(
            context(run),
            prepared=prepared_result["prepared"],
            pending_action=prepared_result["pending_action"],
            receipt=receipt,
            runner=runner,
        )
    assert excinfo.value.code == "OPERATION_ALREADY_RESOLVED"
    # The second call never reached the probe: the spy's count is unchanged
    # from the first, successful resolution.
    assert runner.count == 1


# ---------------------------------------------------------------------------
# AC-T14-004 (3-5): the probe, not the receipt's own claim, decides.
# ---------------------------------------------------------------------------


def test_matching_receipt_with_agreeing_readback_is_applied(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    runner = common.FakeRunner(replies=[common_probe_reply(OBSERVED_TARGET_TEXT)])

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )

    assert result["status"] == pa.APPLIED
    assert result["classification"] == do.INTENDED_EFFECT_PRESENT
    resolved = common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="RESOLUTION"
    )
    assert len(resolved) == 1
    assert resolved[0]["status"] == pa.APPLIED
    assert resolved[0]["error"] is None


def test_matching_receipt_with_disagreeing_readback_is_conflict(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    # The harness claims success, but an independent readback observes
    # something that matches neither the intended remote head nor the prior
    # precondition: a real conflicting effect, not silence.
    runner = common.FakeRunner(
        replies=[common_probe_reply("something else entirely happened")]
    )

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )

    assert result["status"] == pa.CONFLICT
    assert result["classification"] == do.CONFLICTING_EFFECT
    resolved = common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="RESOLUTION"
    )
    assert resolved[0]["status"] == pa.CONFLICT
    assert resolved[0]["error"]["code"] == "PROTECTED_ACTION_CONFLICT"


def test_matching_receipt_with_unreadable_readback_is_unknown(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    # The probe itself could not be dispatched or read back at all.
    runner = common.FakeRunner(
        replies=[safe_output.SafeOutputError("PROBE_UNREADABLE")]
    )

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )

    assert result["status"] == pa.UNKNOWN
    assert result["classification"] == do.INSUFFICIENT_OBSERVATION
    resolved = common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="RESOLUTION"
    )
    assert resolved[0]["status"] == pa.UNKNOWN
    assert resolved[0]["error"]["code"] == "PROTECTED_ACTION_UNKNOWN"


# ---------------------------------------------------------------------------
# AC-T14-004 (6): every binding field is checked, and checked before dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_code"),
    [
        ("receipt_variant", "hermes_dispatch", "RECEIPT_VARIANT_MISMATCH"),
        ("operation_id", "0" * 64, "RECEIPT_OPERATION_MISMATCH"),
        ("target_sha256", OTHER_SHA, "RECEIPT_TARGET_MISMATCH"),
        ("action_sha256", "1" * 64, "RECEIPT_ACTION_MISMATCH"),
    ],
)
def test_receipt_binding_mismatch_is_refused_without_dispatch(
    run, field, bad_value, expected_code
):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"],
        prepared_result["pending_action"],
        **{field: bad_value},
    )
    runner = common.FakeRunner(replies=[])

    with pytest.raises(pa.ProtectedActionError) as excinfo:
        pa.resolve_protected_action(
            context(run),
            prepared=prepared_result["prepared"],
            pending_action=prepared_result["pending_action"],
            receipt=receipt,
            runner=runner,
        )

    assert excinfo.value.code == expected_code
    assert runner.count == 0
    assert not common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="RESOLUTION"
    )


# ---------------------------------------------------------------------------
# AC-T14-004 (7): a grant authorizes exactly one operation, once.
# ---------------------------------------------------------------------------


def test_grant_authorizes_one_operation_and_refuses_a_second(run):
    op_a = state.sha256_bytes(b"solo-finish-design-decision/first")
    op_b = state.sha256_bytes(b"solo-finish-design-decision/second")
    grant = grant_for(authorized_operation_id=op_a)

    first = pa.consume_approval_grant(
        context(run),
        grant,
        protected_class="design_decision",
        authorized_operation_id=op_a,
        target_sha256=TARGET_SHA,
        precondition_sha256=PRECONDITION_SHA,
        ownership_epoch=1,
        responder_identity="parent",
        issue_id=ISSUE,
    )
    assert first["consume_operation_id"]
    resolved = common.journal_records(
        run, effect_type=pa.APPROVAL_CONSUME_EFFECT_TYPE, phase="RESOLUTION"
    )
    assert len(resolved) == 1
    assert resolved[0]["status"] == pa.APPLIED

    # The same grant, aimed at a different operation, is refused before it
    # ever reaches the journal a second time.
    with pytest.raises(pa.ProtectedActionError) as excinfo:
        pa.consume_approval_grant(
            context(run),
            grant,
            protected_class="design_decision",
            authorized_operation_id=op_b,
            target_sha256=TARGET_SHA,
            precondition_sha256=PRECONDITION_SHA,
            ownership_epoch=1,
            responder_identity="parent",
            issue_id=ISSUE,
        )
    assert excinfo.value.code == "GRANT_OPERATION_MISMATCH"
    assert (
        len(
            common.journal_records(
                run, effect_type=pa.APPROVAL_CONSUME_EFFECT_TYPE, phase="RESOLUTION"
            )
        )
        == 1
    )


# ---------------------------------------------------------------------------
# AC-T14-004 (8): a grant can never authorize a protected effect class.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "protected_class",
    ["production", "spend", "destructive", "secret", "push", "dolt_remote"],
)
def test_grant_refuses_every_never_grantable_class(run, protected_class):
    op_id = state.sha256_bytes(
        f"solo-finish-never-grantable/{protected_class}".encode()
    )
    grant = grant_for(authorized_operation_id=op_id, protected_class=protected_class)

    with pytest.raises(pa.ProtectedActionError) as excinfo:
        pa.consume_approval_grant(
            context(run),
            grant,
            protected_class=protected_class,
            authorized_operation_id=op_id,
            target_sha256=TARGET_SHA,
            precondition_sha256=PRECONDITION_SHA,
            ownership_epoch=1,
            responder_identity="parent",
            issue_id=ISSUE,
        )

    assert excinfo.value.code == "GRANT_CLASS_NOT_AUTHORIZABLE"
    assert excinfo.value.status == pa.HUMAN_ACTION_REQUIRED
    assert not common.journal_records(
        run, effect_type=pa.APPROVAL_CONSUME_EFFECT_TYPE, phase="RESOLUTION"
    )


# ---------------------------------------------------------------------------
# AC-T14-004 (9): "remote authority absence reports local-only completion"
# (solo-execution.md #6) — a real local close, no manufactured remote one.
# ---------------------------------------------------------------------------


def test_local_close_completes_while_remote_dolt_sync_stays_unresolved(run):
    fake = common.FakeNative(
        m.safe_bd,
        responses={
            "issue_comments": common.comments(),
            # Two distinct replies: ``direct_operation.execute`` reads back
            # once before dispatch (prestate) and once after (poststate). A
            # single reused value would make both reads see the same
            # "in_progress" state and misclassify the close as
            # ``PRESTATE_UNCHANGED`` (NOT_APPLIED) instead of APPLIED.
            "issue_get": [CLAIMED, CLOSED],
            "append_marker_note": {"ok": True},
            "close_exact": CLOSED,
        },
        allowed=["issue_comments", "issue_get", "append_marker_note", "close_exact"],
    )
    close_operation = do.DirectOperation(
        caller_key=f"close/{ISSUE}",
        effect_type="TRACKER_CLOSE",
        target_identity=f"bd://issue/{ISSUE}",
        issue_id=ISSUE,
        ownership_epoch=run["epochs"][ISSUE],
        arguments={"issue_id": ISSUE, "reason": "solo finish fixture"},
        readback=do.Readback(
            profile="issue_get",
            arguments={"issue_id": ISSUE},
            intended={"status": "closed"},
            prestate={"status": "in_progress"},
        ),
    )
    closed = do.execute(context(run), close_operation, actor=ACTOR, runner=fake)
    assert closed.status == do.APPLIED
    assert closed.classification == do.INTENDED_EFFECT_PRESENT
    assert (
        len(
            common.journal_records(run, effect_type="TRACKER_CLOSE", phase="RESOLUTION")
        )
        == 1
    )

    # solo-execution.md #6: "Generic implementation authority does not imply
    # ... Dolt pull/push ... authority." A parent-attested grant can never
    # manufacture the remote milestone in place of a real, independently
    # observed sync — ``dolt_remote`` is in ``NEVER_GRANTABLE_CLASSES``.
    op_id = state.sha256_bytes(b"solo-finish-dolt-sync/unauthorized")
    grant = grant_for(authorized_operation_id=op_id, protected_class="dolt_remote")
    with pytest.raises(pa.ProtectedActionError) as excinfo:
        pa.consume_approval_grant(
            context(run),
            grant,
            protected_class="dolt_remote",
            authorized_operation_id=op_id,
            target_sha256=TARGET_SHA,
            precondition_sha256=PRECONDITION_SHA,
            ownership_epoch=run["epochs"][ISSUE],
            responder_identity="parent",
            issue_id=ISSUE,
        )
    assert excinfo.value.code == "GRANT_CLASS_NOT_AUTHORIZABLE"
    assert excinfo.value.status == pa.HUMAN_ACTION_REQUIRED

    # Absent a resolved receipt, the sync itself stays a dangling
    # HUMAN_ACTION_REQUIRED intent: only the local close is real, and a
    # finish report bound to this evidence may claim ``success_local`` only.
    prepared_result = prepare(run)
    assert prepared_result["status"] == pa.HUMAN_ACTION_REQUIRED
    assert not common.journal_records(
        run, effect_type="TRACKER_DOLT_PUSH", phase="RESOLUTION"
    )
    assert not common.journal_records(
        run, effect_type=pa.APPROVAL_CONSUME_EFFECT_TYPE, phase="RESOLUTION"
    )


def common_probe_reply(observed_text, *, exit_code=0, error_code=None):
    """Build one scripted ``(result, observed_text)`` runner reply."""
    result = type(
        "FakeSafeCommandResult",
        (),
        {"exit_code": exit_code, "error_code": error_code, "status": "SUCCESS"},
    )()
    return (result, observed_text)
