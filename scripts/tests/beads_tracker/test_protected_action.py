"""AC-T12-004 / AC-T12-005: protected effects never run themselves.

``protected_action.prepare_protected_action`` never dispatches anything (it
has no ``runner`` parameter at all), and
``protected_action.resolve_protected_action`` only ever accepts a matching
receipt *plus* an independently observed readback that agrees with it.  This
suite proves both halves the same way ``test_direct_operation.py`` proves
"never blindly replays": a scripted double that raises the moment it is
asked to do something a test did not expect, so "no dispatch" and "resolved
exactly once" are assertions rather than claims.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from . import _common as common

m = common.modules("direct_operation", "protected_action")
do = m.direct_operation
pa = m.protected_action
state = m.coordinator_state
# ``pa`` imports ``safe_output`` itself, via its own ``import safe_output``
# statement under the shared ``sys.path`` prelude.  That load is keyed by the
# plain module name, so it lands in ``sys.modules`` as a *different* object
# from ``m.safe_output`` (which ``common.modules()`` loads under a synthetic
# per-call name).  ``resolve_protected_action``'s ``except safe_output.SafeOutputError``
# only recognises exceptions built from the class on *its own* import, so a
# fixture must raise ``pa.safe_output.SafeOutputError``, not ``m.safe_output``'s.
safe_output = pa.safe_output


ISSUE = "scc-lane-a"

# ``target_sha256`` is the hash of whatever identity a *readback* must
# observe to count as the intended effect (see ``classify_observation``), not
# an arbitrary opaque value — so the "agreeing" scenario's fake probe reply
# must echo this exact text back for the hashes to line up.
OBSERVED_TARGET_TEXT = "harness confirms the push landed on origin/main"
TARGET_SHA = state.sha256_bytes(OBSERVED_TARGET_TEXT.encode("utf-8"))
PRECONDITION_SHA = state.sha256_bytes(b"protected-precondition-prior-identity")
OTHER_SHA = state.sha256_bytes(b"protected-neither-target-nor-precondition")


def context(run, *, crash_hook=None):
    return do.RunContext(
        run_directory=run["run_directory"],
        manifest=run["manifest"],
        journal=run["journal"],
        checkpoints=run["checkpoints"],
        crash_hook=crash_hook or state.NOOP_HOOK,
    )


class RunnerDenied(AssertionError):
    """The probe runner was invoked when the test expected no dispatch."""


class FakeRunner:
    """Scripted stand-in for ``resolve_protected_action``'s injectable ``runner``.

    Mirrors :class:`common.FakeNative`'s denial-spy contract for the one
    subprocess surface this module owns: every :class:`safe_output.CommandSpec`
    it is handed is recorded, and a call is refused outright unless the test
    explicitly armed a reply for it.  A reply is either a ``(result,
    observed_text)`` tuple returned verbatim, or an exception instance/class
    raised to simulate a probe that could not be dispatched at all.
    """

    def __init__(self, *, allowed: bool = True, replies=()) -> None:
        self.allowed = allowed
        self.replies = list(replies)
        self.calls: list[Any] = []

    def __call__(self, spec, *, sensitive, callback):
        self.calls.append(spec)
        if not self.allowed:
            raise RunnerDenied(spec.profile)
        if not self.replies:
            raise RunnerDenied(f"unscripted:{spec.profile}")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, type) and issubclass(reply, BaseException):
            raise reply()
        return reply

    @property
    def count(self) -> int:
        return len(self.calls)


def probe_reply(
    observed_text: str, *, exit_code: int = 0, error_code: str | None = None
):
    """Build one scripted ``(result, observed_text)`` runner reply."""
    result = type(
        "FakeSafeCommandResult",
        (),
        {"exit_code": exit_code, "error_code": error_code, "status": "SUCCESS"},
    )()
    return (result, observed_text)


@pytest.fixture()
def run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return common.make_run(m, repo, issue_ids=[ISSUE])


def prepare(run, *, precondition_sha256=PRECONDITION_SHA, target_sha256=TARGET_SHA):
    return pa.prepare_protected_action(
        context(run),
        effect_type="GIT_PUSH",
        target_identity="git://origin/main",
        target_sha256=target_sha256,
        precondition_sha256=precondition_sha256,
        summary="push main to origin",
        probe_type="git_remote_ref",
        probe_argv=["/usr/bin/git", "ls-remote", "origin", "main"],
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
        "observed_identity": "harness observed the push landed"
        if outcome == "applied"
        else None,
        "observed_sha256": TARGET_SHA if outcome == "applied" else None,
        "evidence": {
            "path": "/tmp/evidence.log" if outcome == "applied" else None,
            "sha256": state.sha256_bytes(b"evidence") if outcome == "applied" else None,
            "summary": "harness ran the push",
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
            f"approval/{authorized_operation_id}".encode()
        ),
        "run_id": common.make_run_id(seed=99),
        "issue_id": ISSUE,
        "ownership_epoch": 1,
        "provenance_class": "parent_attested",
        "approver_identity": "parent",
        "evidence_source": "parent-transcript",
        "evidence_sha256": state.sha256_bytes(b"parent-transcript-evidence"),
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


# ---------------------------------------------------------------------------
# AC-T12-004 (1): preparing never dispatches, and always demands a human.
# ---------------------------------------------------------------------------


def test_prepare_returns_human_action_required_with_no_dispatch(run):
    # No ``runner`` parameter exists on this function at all: there is no
    # code path through which it could dispatch a probe or an effect.
    assert "runner" not in inspect.signature(pa.prepare_protected_action).parameters

    result = prepare(run)

    assert result["status"] == pa.HUMAN_ACTION_REQUIRED
    prepared = common.journal_records(run, effect_type="GIT_PUSH", phase="PREPARED")
    resolved = common.journal_records(run, effect_type="GIT_PUSH", phase="RESOLUTION")
    assert len(prepared) == 1
    assert not resolved


# ---------------------------------------------------------------------------
# AC-T12-004 (2): a receipt cannot resolve the same operation twice.
# ---------------------------------------------------------------------------


def test_resolving_the_same_operation_twice_is_refused(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    runner = FakeRunner(replies=[probe_reply(OBSERVED_TARGET_TEXT)])

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
    # The second call never reached the probe: the denial spy's count is
    # unchanged from the first, successful resolution.
    assert runner.count == 1


# ---------------------------------------------------------------------------
# AC-T12-004 (3-5): the probe, not the receipt's own claim, decides.
# ---------------------------------------------------------------------------


def test_matching_receipt_with_agreeing_readback_is_applied(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    runner = FakeRunner(replies=[probe_reply(OBSERVED_TARGET_TEXT)])

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )

    assert result["status"] == pa.APPLIED
    assert result["classification"] == do.INTENDED_EFFECT_PRESENT
    resolved = common.journal_records(run, effect_type="GIT_PUSH", phase="RESOLUTION")
    assert len(resolved) == 1
    assert resolved[0]["status"] == pa.APPLIED
    assert resolved[0]["error"] is None


def test_matching_receipt_with_disagreeing_readback_is_conflict(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    # The harness claims success, but an independent readback observes
    # something that matches neither the intended target nor the prior
    # precondition: a real conflicting effect, not silence.
    runner = FakeRunner(replies=[probe_reply("something else entirely happened")])

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )

    assert result["status"] == pa.CONFLICT
    assert result["classification"] == do.CONFLICTING_EFFECT
    resolved = common.journal_records(run, effect_type="GIT_PUSH", phase="RESOLUTION")
    assert resolved[0]["status"] == pa.CONFLICT
    assert resolved[0]["error"]["code"] == "PROTECTED_ACTION_CONFLICT"


def test_matching_receipt_with_unreadable_readback_is_unknown(run):
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"], prepared_result["pending_action"]
    )
    # The probe itself could not be dispatched or read back at all.
    runner = FakeRunner(replies=[safe_output.SafeOutputError("PROBE_UNREADABLE")])

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
    )

    assert result["status"] == pa.UNKNOWN
    assert result["classification"] == do.INSUFFICIENT_OBSERVATION
    resolved = common.journal_records(run, effect_type="GIT_PUSH", phase="RESOLUTION")
    assert resolved[0]["status"] == pa.UNKNOWN
    assert resolved[0]["error"]["code"] == "PROTECTED_ACTION_UNKNOWN"


# ---------------------------------------------------------------------------
# Required verification: secret redaction.  A receipt field can carry a
# secret a harness should never have echoed back (a credential embedded in
# its own self-description).  Nothing the harness hands back becomes durable
# — journalled, checkpointed, or returned to the caller — until it has run
# through the shared redaction gate, so this proves the gate actually runs
# on the real, owned code path (not a mock of ``_sanitize_receipt`` itself),
# on both the in-memory result and the evidence file written to disk.
# ---------------------------------------------------------------------------


def test_resolve_redacts_a_secret_embedded_in_the_receipt_before_it_becomes_durable(
    run,
):
    secret = "sk-live-classified-token-value"  # >= 8 UTF-8 bytes, so redactable.
    prepared_result = prepare(run)
    receipt = receipt_for(
        prepared_result["prepared"],
        prepared_result["pending_action"],
        harness=f"current-harness ({secret})",
        evidence={
            "path": "/tmp/evidence.log",
            "sha256": state.sha256_bytes(b"evidence"),
            "summary": f"harness ran the push using {secret}",
        },
    )
    # The readback the probe observes never contains the secret: redaction of
    # the receipt's own fields is what this test is proving, not redaction of
    # the observed identity.
    runner = FakeRunner(replies=[probe_reply(OBSERVED_TARGET_TEXT)])
    sensitive = safe_output.SensitiveSet((secret,))

    result = pa.resolve_protected_action(
        context(run),
        prepared=prepared_result["prepared"],
        pending_action=prepared_result["pending_action"],
        receipt=receipt,
        runner=runner,
        sensitive=sensitive,
    )

    assert result["status"] == pa.APPLIED
    assert secret not in result["receipt"]["harness"]
    assert secret not in result["receipt"]["evidence"]["summary"]
    assert "[REDACTED]" in result["receipt"]["harness"]
    assert "[REDACTED]" in result["receipt"]["evidence"]["summary"]

    resolved = common.journal_records(run, effect_type="GIT_PUSH", phase="RESOLUTION")
    evidence_path = resolved[0]["readback_evidence_path"]
    evidence_bytes = Path(evidence_path).read_bytes()
    assert secret.encode("utf-8") not in evidence_bytes
    assert b"[REDACTED]" in evidence_bytes


# ---------------------------------------------------------------------------
# AC-T12-004 (6): every binding field is checked, and checked before dispatch.
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
    runner = FakeRunner(allowed=False)

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
    assert not common.journal_records(run, effect_type="GIT_PUSH", phase="RESOLUTION")


# ---------------------------------------------------------------------------
# AC-T12-005 (7): a grant authorizes exactly one operation, once.
# ---------------------------------------------------------------------------


def test_grant_authorizes_one_operation_and_refuses_a_second(run):
    op_a = state.sha256_bytes(b"design-decision/first")
    op_b = state.sha256_bytes(b"design-decision/second")
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
# AC-T12-005 (8): a grant can never authorize a protected effect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "protected_class",
    ["production", "spend", "destructive", "secret", "push", "dolt_remote"],
)
def test_grant_refuses_every_never_grantable_class(run, protected_class):
    op_id = state.sha256_bytes(f"never-grantable/{protected_class}".encode())
    grant = grant_for(authorized_operation_id=op_id, decision_type=protected_class)

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
