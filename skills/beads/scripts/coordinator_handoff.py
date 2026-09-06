"""AC-T12-006: the acceptance gate for durable, out-of-process executors.

``direct_operation`` assumes the effect it dispatches runs inside the same
process, on the same attempt loop, under the same crash-recovery discipline.
A durable executor breaks every one of those assumptions: it is launched,
may run for hours outside this process, may crash and be retried by
something else entirely, and reports back on its own schedule.  Its self
report is therefore not evidence the parent can trust by default -- it is a
claim from an untrusted, unsupervised party about what happened to state the
parent alone is allowed to change.

This module is the seam between "a durable executor says it is done" and
"the parent tracker actually reflects that."  It does two things, and only
two things:

``launch`` durably records that *this run* handed a scope of work to a named
executor under a named handoff.  It is pure bookkeeping -- it does not spawn
or supervise the executor process, which is entirely outside its authority.
It exists so that later, ``accept`` can prove a result traces back to a
launch this run actually issued, rather than trusting the caller's word for
it.

``accept`` is the gate itself.  Eight independent conditions -- identity,
artifact, epoch, handoff-record, expiry, unauthorized Beads mutation,
duplicate/late delivery, and unknown launch -- each block acceptance outright
and leave the tracker untouched.  A result that clears every guard is still
not auto-accepted: it is handed to :func:`direct_operation.execute`, whose
own independent readback -- not the executor's self-reported ``status`` --
decides APPLIED / NOT_APPLIED / CONFLICT / UNKNOWN.  A child's claim of
success is evidence a human or a later attempt can inspect; it is never the
verdict.  That is the same discipline ``direct_operation`` applies to the
parent's own attempts, extended to a party the parent cannot supervise.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).parent))

import beads_ownership  # noqa: E402
import coordinator_state as state  # noqa: E402
import direct_operation  # noqa: E402
import schema_runtime  # noqa: E402

__all__ = [
    "HandoffError",
    "HandoffLaunchResult",
    "HandoffAcceptanceResult",
    "launch",
    "accept",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Free-form (schema-wise) but fixed values this module commits to, so every
# journal record it writes is greppable by effect_type without ambiguity
# against direct_operation's own TRACKER_* effect types.
LAUNCH_EFFECT_TYPE = "HANDOFF_LAUNCH"
ACCEPT_EFFECT_TYPE = "HANDOFF_ACCEPT"

HANDOFF_SCHEMA = "durable-handoff-v1.schema.json"
RESULT_SCHEMA = "durable-executor-result-v1.schema.json"

# Generous but bounded: durable-handoff-v1 allows large arrays of long
# strings (stages, resume_topology, scope_issue_ids, ...), so a real handoff
# can be several hundred KB of canonical JSON. This is a corruption/DoS
# backstop, not a tight budget.
MAX_HANDOFF_BYTES = 1_048_576
MAX_EVIDENCE_BYTES = 65_536
# An executor artifact is a build/patch/log output, not a short intent
# payload -- bounded generously so a legitimate artifact never trips this,
# while an unbounded read is still never attempted.
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024

_ATTEMPT_ID = "attempt-001"


class HandoffError(ValueError):
    """A typed, fail-closed refusal raised by handoff bookkeeping itself.

    Guard failures inside :func:`accept` do NOT raise this -- a bad result is
    an expected outcome the caller must handle, not an exceptional one, so
    guards report their code through :class:`HandoffAcceptanceResult`
    instead. This is reserved for precondition violations in ``launch``
    (e.g. a handoff minted for a different run) that indicate caller error
    rather than an untrusted executor's result.
    """

    def __init__(self, code: str, *, status: str = direct_operation.CONFLICT) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class HandoffLaunchResult:
    """The outcome of durably recording a handoff launch."""

    status: str
    handoff_id: str
    operation_id: str
    already_launched: bool
    prepared: dict[str, Any] | None
    resolution: dict[str, Any] | None


@dataclass(frozen=True)
class HandoffAcceptanceResult:
    """The outcome of running an executor result through the acceptance gate.

    ``guard_passed`` is False for every one of the eight AC-T12-006 blocking
    conditions (and the duplicate/late/unknown-launch structural checks);
    ``error_code`` names exactly which one fired.  When ``guard_passed`` is
    True, ``status``/``error_code`` are copied straight from the nested
    ``operation_result`` -- the parent's own readback, not this gate --
    because passing every guard earns the result a fair hearing, not a
    verdict.
    """

    status: str
    handoff_id: str
    accept_operation_id: str
    guard_passed: bool
    error_code: str | None
    accept_prepared: dict[str, Any] | None
    accept_resolution: dict[str, Any] | None
    operation_result: direct_operation.DirectOperationResult | None


# ---------------------------------------------------------------------------
# Small private helpers, mirroring direct_operation's own idioms
# ---------------------------------------------------------------------------


def _operations_directory(context: direct_operation.RunContext) -> Path:
    return direct_operation.operations_directory(context.run_directory)


def _handoff_path(context: direct_operation.RunContext, handoff_id: str) -> Path:
    return _operations_directory(context) / f"{handoff_id}.handoff.json"


def _read_owned_bytes(path: Path, *, root: Path, max_bytes: int) -> bytes:
    """Read a file this process itself wrote, refusing anything oversized.

    Mirrors ``direct_operation``'s own bounded-read idiom: resolve through
    the owner-directory guard first, then open with ``O_NOFOLLOW`` so a
    symlink swapped in after the guard check can't redirect the read.
    """
    checked = state.validate_owner_file(path, root=root)
    fd = os.open(checked, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise HandoffError("HANDOFF_RECORD_TOO_LARGE", status=direct_operation.UNKNOWN)
    return raw


def _freeze_handoff(
    context: direct_operation.RunContext, handoff: Mapping[str, Any]
) -> tuple[Path, str]:
    """Publish the launched handoff to an immutable, content-addressed file.

    ``accept`` never trusts a caller-supplied handoff dict to *be* what this
    run actually launched -- it derives ``accept``'s and ``launch``'s
    operation ids from the handoff's own canonical bytes, so a tampered or
    substituted handoff simply fails to match any recorded launch
    (HANDOFF_UNKNOWN_LAUNCH). Freezing it to disk here is what makes the
    launch replayable and auditable after a crash.
    """
    directory = _operations_directory(context)
    raw = state.canonical_bytes(dict(handoff))
    path = directory / f"{handoff['handoff_id']}.handoff.json"
    if path.exists():
        existing = _read_owned_bytes(path, root=directory, max_bytes=MAX_HANDOFF_BYTES)
        return path, state.sha256_bytes(existing)
    state.atomic_write(
        path,
        raw,
        root=context.run_directory,
        max_bytes=MAX_HANDOFF_BYTES,
        hook=context.crash_hook,
    )
    return path, state.sha256_bytes(raw)


def _write_evidence(
    context: direct_operation.RunContext,
    operation_id: str,
    payload: Mapping[str, Any],
) -> tuple[Path, str]:
    """Persist the gate's own decision record as journal readback evidence."""
    directory = _operations_directory(context)
    raw = state.canonical_bytes(dict(payload))
    path = directory / f"{operation_id}.handoff-evidence.json"
    if path.exists():
        existing = _read_owned_bytes(path, root=directory, max_bytes=MAX_EVIDENCE_BYTES)
        return path, state.sha256_bytes(existing)
    state.atomic_write(
        path,
        raw,
        root=context.run_directory,
        max_bytes=MAX_EVIDENCE_BYTES,
        hook=context.crash_hook,
    )
    return path, state.sha256_bytes(raw)


def _launch_operation_id(handoff_id: str, handoff_sha256: str) -> str:
    return state.semantic_operation_id(
        {
            "schema": "beads.handoff-launch.v1",
            "effect_type": LAUNCH_EFFECT_TYPE,
            "target_identity": handoff_id,
            "immutable_input_sha256": handoff_sha256,
            "ownership_epoch": None,
        }
    )


def _accept_operation_id(handoff_id: str, handoff_sha256: str) -> str:
    # Keyed off the HANDOFF's content only (never the result's), so this id
    # is stable across every attempt to accept a *result* for the same
    # handoff -- which is exactly what makes "one journalled decision per
    # handoff, ever" enforceable as a single operation_id lookup.
    return state.semantic_operation_id(
        {
            "schema": "beads.handoff-accept.v1",
            "effect_type": ACCEPT_EFFECT_TYPE,
            "target_identity": handoff_id,
            "immutable_input_sha256": handoff_sha256,
            "ownership_epoch": None,
        }
    )


def _handoff_probe(
    *,
    probe_type: str,
    target_identity: str,
    expected_before_sha256: str,
    intended_after_sha256: str,
    descriptor: Sequence[str],
    timeout_seconds: int = direct_operation.PROBE_TIMEOUT_SECONDS,
    required_authority: str = direct_operation.PROBE_AUTHORITY,
) -> dict[str, Any]:
    """Build a recovery-probe *request* for a handoff bookkeeping event.

    ``direct_operation.tracker_probe`` hardcodes ``probe_type="tracker_state"``
    because every ``direct_operation`` effect is, in the end, a tracker read.
    A handoff launch or acceptance decision is not a tracker read -- it is
    dispatch/receipt bookkeeping about a durable executor -- so it needs the
    ``hermes_dispatch`` / ``worker_receipt`` probe types the schema reserves
    for exactly this instead.
    """
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": probe_type,
        "target_identity": target_identity,
        "expected_before_sha256": expected_before_sha256,
        "intended_after_identity": target_identity,
        "intended_after_sha256": intended_after_sha256,
        "descriptor": [str(part) for part in descriptor],
        "timeout_seconds": int(timeout_seconds),
        "required_authority": required_authority,
    }


def _real_artifact_identity(path: Path, *, root: Path) -> tuple[int, str] | None:
    """Independently observe an artifact's true size and content hash.

    The executor's self-reported ``size_bytes``/``sha256`` are exactly the
    fields a buggy or dishonest child could get wrong or fake, so acceptance
    must never trust them -- it re-derives both from the bytes that are
    actually on disk, the same discipline ``operation_result``'s own
    artifact verification uses. Returns ``None`` for anything that is not a
    clean, in-bounds, readable file: a missing file, a path escaping the
    handoff's isolation target, or an artifact too large to be a real
    verifiable result all collapse to the same refusal.
    """
    try:
        checked = state.validate_owner_file(path, root=root)
    except state.StateError:
        return None
    try:
        size = checked.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            return None
        with open(checked, "rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None
    return size, digest


# ---------------------------------------------------------------------------
# Guards -- each returns an error code, or None if the check passed
# ---------------------------------------------------------------------------


def _guard_unknown_launch(
    records: Sequence[Mapping[str, Any]], launch_operation_id: str
) -> str | None:
    """A result may only be judged against a handoff this run itself issued.

    Without this, any caller could hand ``accept`` a well-formed handoff it
    invented on the spot and have it evaluated as if this run had actually
    launched that scope of work to that executor.
    """
    for record in records:
        if (
            record.get("operation_id") == launch_operation_id
            and record.get("phase") == "RESOLUTION"
            and record.get("status") == direct_operation.APPLIED
        ):
            return None
    return "HANDOFF_UNKNOWN_LAUNCH"


def _guard_record_mismatch(
    handoff: Mapping[str, Any], result: Mapping[str, Any], handoff_sha256: str
) -> str | None:
    """The result must bind to this exact handoff record, not merely one like it."""
    if result["handoff_id"] != handoff["handoff_id"]:
        return "HANDOFF_RECORD_MISMATCH"
    if result["run_id"] != handoff["run_id"]:
        return "HANDOFF_RECORD_MISMATCH"
    if result["handoff_sha256"] != handoff_sha256:
        return "HANDOFF_RECORD_MISMATCH"
    result_issues = {entry["issue_id"]: entry["epoch"] for entry in result["issues"]}
    handoff_issues = {
        entry["issue_id"]: entry["epoch"] for entry in handoff["held_ownership"]
    }
    if result_issues != handoff_issues:
        return "HANDOFF_RECORD_MISMATCH"
    return None


def _guard_identity_mismatch(
    handoff: Mapping[str, Any], result: Mapping[str, Any]
) -> str | None:
    """Only the executor actually launched may return a result for a handoff."""
    if result["executor_identity"] != handoff["executor_identity"]:
        return "HANDOFF_EXECUTOR_IDENTITY_MISMATCH"
    return None


def _guard_epoch_mismatch(
    handoff: Mapping[str, Any], ownership: beads_ownership.OwnershipStore
) -> str | None:
    """Ownership must still be held, at the pinned epoch, for every scoped issue.

    This is a LIVE check against the ownership store, distinct from the
    static handoff/result comparison in ``_guard_record_mismatch``: it
    catches the epoch moving *while the child ran* -- e.g. a lease expiring
    and being reacquired -- which no amount of comparing the two payloads to
    each other could ever detect.
    """
    for entry in handoff["held_ownership"]:
        try:
            probe = ownership.inspect_readonly(entry["issue_id"])
        except state.StateError:
            # An issue this handoff claims to hold that the ownership store
            # cannot even locate is exactly as disqualifying as one whose
            # epoch has visibly moved -- fail closed either way.
            return "HANDOFF_EPOCH_MISMATCH"
        if probe.disposition != "held":
            return "HANDOFF_EPOCH_MISMATCH"
        if probe.record is None or probe.record.get("epoch") != entry["epoch"]:
            return "HANDOFF_EPOCH_MISMATCH"
    return None


def _guard_artifact_mismatch(
    handoff: Mapping[str, Any], result: Mapping[str, Any]
) -> str | None:
    """The artifact must be verified by content, never trusted by name or size alone."""
    artifact = result["artifact"]
    observed = _real_artifact_identity(
        Path(artifact["path"]), root=Path(handoff["isolation_target"])
    )
    if observed is None:
        return "HANDOFF_ARTIFACT_MISMATCH"
    size, digest = observed
    if digest != artifact["sha256"] or size != artifact["size_bytes"]:
        return "HANDOFF_ARTIFACT_MISMATCH"
    return None


def _guard_beads_mutation(result: Mapping[str, Any]) -> str | None:
    """Only the parent may write the tracker; a child that mutated it is disqualified.

    The schema constrains ``beads_mutated`` to ``const: false``, so a result
    that fails schema validation never reaches this guard at all -- this is
    the defense-in-depth check for a caller that bypassed validation, and
    it fails closed on anything that is not an explicit ``False``.
    """
    if result.get("beads_mutated") is not False:
        return "HANDOFF_BEADS_MUTATION_DETECTED"
    return None


def _guard_expired(handoff: Mapping[str, Any], *, at: str) -> str | None:
    """A result arriving after the handoff's deadline is refused, however clean it is.

    Both timestamps are fixed-width, zero-padded ISO-8601 UTC
    (``direct_operation.utc_now``'s own format), so ordinary string
    comparison is exact -- no parsing, no timezone ambiguity.
    """
    if at > handoff["expires_at"]:
        return "HANDOFF_EXPIRED"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def launch(
    context: direct_operation.RunContext,
    *,
    handoff: Mapping[str, Any],
    actor: str,
    now: Callable[[], str] = direct_operation.utc_now,
) -> HandoffLaunchResult:
    """Durably record that this run handed scope to a durable executor.

    Dispatching the executor process itself -- spawning it, supervising it,
    feeding it the handoff -- is entirely outside this function's authority
    and happens elsewhere. ``launch`` only makes the handoff provable
    afterward: it is what lets ``accept`` refuse a result for any handoff
    this run never actually issued (AC-T12-006, "unknown launch") instead of
    trusting a caller's say-so.

    There is no tracker effect to dispatch or probe here -- launching is
    pure bookkeeping -- so the resolution is written immediately as APPLIED
    once the handoff record is durably frozen to disk.
    """
    schema_runtime.require_valid(HANDOFF_SCHEMA, dict(handoff))
    if handoff["run_id"] != context.run_id:
        raise HandoffError("HANDOFF_RUN_MISMATCH")

    handoff_path, handoff_sha256 = _freeze_handoff(context, handoff)
    operation_id = _launch_operation_id(handoff["handoff_id"], handoff_sha256)

    records = context.journal.read().records
    prior_resolutions = [
        r
        for r in records
        if r.get("operation_id") == operation_id and r.get("phase") == "RESOLUTION"
    ]
    if prior_resolutions:
        record = prior_resolutions[0]
        return HandoffLaunchResult(
            status=str(record["status"]),
            handoff_id=handoff["handoff_id"],
            operation_id=operation_id,
            already_launched=True,
            prepared=None,
            resolution=dict(record),
        )

    dangling = [
        r
        for r in records
        if r.get("operation_id") == operation_id and r.get("phase") == "PREPARED"
    ]
    if dangling:
        prepared = dict(dangling[0])
    else:
        probe = _handoff_probe(
            probe_type="hermes_dispatch",
            target_identity=handoff["handoff_id"],
            expected_before_sha256=direct_operation.GENESIS,
            intended_after_sha256=handoff_sha256,
            descriptor=["beads", "handoff", "launch"],
        )
        prepared = direct_operation.prepared_event(
            run_id=context.run_id,
            operation_id=operation_id,
            effect_type=LAUNCH_EFFECT_TYPE,
            input_path=handoff_path,
            input_sha256=handoff_sha256,
            expected_pre_state_sha256=direct_operation.GENESIS,
            probe=probe,
            timestamp=now(),
            issue_id=None,
            ownership_epoch=None,
            attempt_id=_ATTEMPT_ID,
        )
        prepared = context.journal.append(prepared, hook=context.crash_hook)
        context.crash_hook("after_handoff_launch_prepared")

    resolution = direct_operation.resolution_event(
        prepared,
        status=direct_operation.APPLIED,
        observed_post_state_sha256=handoff_sha256,
        readback_evidence_path=handoff_path,
        readback_evidence_sha256=handoff_sha256,
        timestamp=now(),
    )
    resolution = context.journal.append(resolution, hook=context.crash_hook)
    context.crash_hook("after_handoff_launch_resolved")

    return HandoffLaunchResult(
        status=direct_operation.APPLIED,
        handoff_id=handoff["handoff_id"],
        operation_id=operation_id,
        already_launched=False,
        prepared=prepared,
        resolution=resolution,
    )


def accept(
    context: direct_operation.RunContext,
    *,
    handoff: Mapping[str, Any],
    result: Mapping[str, Any],
    operation: direct_operation.DirectOperation,
    actor: str,
    ownership: beads_ownership.OwnershipStore,
    runner: Callable[..., Any] | None = None,
    sensitive: Any = direct_operation.NO_SENSITIVE,
    now: Callable[[], str] = direct_operation.utc_now,
) -> HandoffAcceptanceResult:
    """Decide whether a durable executor's result may reach the tracker.

    Eight independent conditions (AC-T12-006) each block acceptance outright
    and leave the tracker untouched: identity mismatch, artifact mismatch,
    epoch mismatch, handoff-record mismatch, expiry, an unauthorized Beads
    mutation by the child, a duplicate or late result, and a result for a
    handoff this run never launched. Every guard failure is journalled as
    its own PREPARED/RESOLUTION pair under this handoff's accept operation
    id, so each condition is independently provable from the journal, and
    provably free of any tracker dispatch.

    A result that clears every guard is still not auto-accepted: it is
    handed to :func:`direct_operation.execute`, whose own independent
    readback -- never the executor's self-reported ``status`` -- decides
    APPLIED / NOT_APPLIED / CONFLICT / UNKNOWN. That delegation is the only
    path in this function that can touch the tracker; ``runner`` is passed
    straight through to it, so ``None`` resolves to ``safe_bd.run_profile``
    exactly as it would for a direct-operation call this module never makes
    itself.
    """
    schema_runtime.require_valid(HANDOFF_SCHEMA, dict(handoff))
    schema_runtime.require_valid(RESULT_SCHEMA, dict(result))

    handoff_sha256 = state.sha256_bytes(state.canonical_bytes(dict(handoff)))
    accept_operation_id = _accept_operation_id(handoff["handoff_id"], handoff_sha256)

    records = context.journal.read().records
    prior_resolutions = [
        r
        for r in records
        if r.get("operation_id") == accept_operation_id
        and r.get("phase") == "RESOLUTION"
    ]
    if prior_resolutions:
        # Once this gate has resolved a handoff once -- accepted or refused
        # -- that decision is terminal. A second result presented for it is
        # either a duplicate of one already accepted, or late for one
        # already closed out; either way nothing further is evaluated or
        # dispatched.
        record = prior_resolutions[0]
        code = (
            "HANDOFF_RESULT_DUPLICATE"
            if record.get("status") == direct_operation.APPLIED
            else "HANDOFF_RESULT_LATE"
        )
        return HandoffAcceptanceResult(
            status=direct_operation.CONFLICT,
            handoff_id=handoff["handoff_id"],
            accept_operation_id=accept_operation_id,
            guard_passed=False,
            error_code=code,
            accept_prepared=None,
            accept_resolution=dict(record),
            operation_result=None,
        )

    launch_operation_id = _launch_operation_id(handoff["handoff_id"], handoff_sha256)
    error_code = _guard_unknown_launch(records, launch_operation_id)
    if error_code is None:
        error_code = _guard_record_mismatch(handoff, result, handoff_sha256)
    if error_code is None:
        error_code = _guard_identity_mismatch(handoff, result)
    if error_code is None:
        error_code = _guard_epoch_mismatch(handoff, ownership)
    if error_code is None:
        error_code = _guard_artifact_mismatch(handoff, result)
    if error_code is None:
        error_code = _guard_beads_mutation(result)
    if error_code is None:
        error_code = _guard_expired(handoff, at=now())

    gate_status = direct_operation.CONFLICT if error_code else direct_operation.APPLIED

    dangling = [
        r
        for r in records
        if r.get("operation_id") == accept_operation_id and r.get("phase") == "PREPARED"
    ]
    if dangling:
        prepared = dict(dangling[0])
    else:
        probe = _handoff_probe(
            probe_type="worker_receipt",
            target_identity=handoff["handoff_id"],
            expected_before_sha256=direct_operation.GENESIS,
            intended_after_sha256=handoff_sha256,
            descriptor=["beads", "handoff", "accept"],
        )
        prepared = direct_operation.prepared_event(
            run_id=context.run_id,
            operation_id=accept_operation_id,
            effect_type=ACCEPT_EFFECT_TYPE,
            input_path=_handoff_path(context, handoff["handoff_id"]),
            input_sha256=handoff_sha256,
            expected_pre_state_sha256=direct_operation.GENESIS,
            probe=probe,
            timestamp=now(),
            issue_id=None,
            ownership_epoch=None,
            attempt_id=_ATTEMPT_ID,
        )
        prepared = context.journal.append(prepared, hook=context.crash_hook)
        context.crash_hook("after_handoff_accept_prepared")

    evidence_path, evidence_sha256 = _write_evidence(
        context,
        accept_operation_id,
        {
            "schema_version": "beads.handoff-accept-evidence.v1",
            "handoff_id": handoff["handoff_id"],
            "accept_operation_id": accept_operation_id,
            "guard_passed": error_code is None,
            "error_code": error_code,
        },
    )
    resolution = direct_operation.resolution_event(
        prepared,
        status=gate_status,
        observed_post_state_sha256=handoff_sha256,
        readback_evidence_path=evidence_path,
        readback_evidence_sha256=evidence_sha256,
        timestamp=now(),
        error_code=error_code,
        template_id="coordinator_handoff_unresolved",
        field_path="/coordinator_handoff",
    )
    resolution = context.journal.append(resolution, hook=context.crash_hook)
    context.crash_hook("after_handoff_accept_resolved")

    if error_code is not None:
        return HandoffAcceptanceResult(
            status=direct_operation.CONFLICT,
            handoff_id=handoff["handoff_id"],
            accept_operation_id=accept_operation_id,
            guard_passed=False,
            error_code=error_code,
            accept_prepared=prepared,
            accept_resolution=resolution,
            operation_result=None,
        )

    # Every guard passed. The result is credible evidence, not a verdict:
    # what actually happened to the tracker is decided by the parent's own
    # independent readback inside direct_operation.execute, exactly as it
    # would be for an effect the parent attempted itself.
    op_result = direct_operation.execute(
        context,
        operation,
        actor=actor,
        runner=runner,
        sensitive=sensitive,
        now=now,
    )
    return HandoffAcceptanceResult(
        status=op_result.status,
        handoff_id=handoff["handoff_id"],
        accept_operation_id=accept_operation_id,
        guard_passed=True,
        error_code=op_result.error_code,
        accept_prepared=prepared,
        accept_resolution=resolution,
        operation_result=op_result,
    )
