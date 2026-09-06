"""Guarded tracker facade: claim-front and root-pointer bootstrap ordering.

This module sits directly on top of :mod:`direct_operation` and owns two
things ``bootstrap_run`` itself cannot own because they are policy, not
mechanism:

* :func:`claim_front` — decide which lanes in a run are even eligible to be
  claimed (the root issue never is), acquire local ownership for each
  eligible lane, dispatch the native claim through the guarded four-way
  saga, and compensate (release) local ownership the moment the native
  tracker disagrees with what we intended.
* :func:`pointer_callbacks` — the :class:`coordinator_state.PointerCallbacks`
  pair that :func:`coordinator_state.bootstrap_run` needs to read and write
  the root run-pointer.  ``bootstrap_run`` only knows how to *sequence*
  ownership, checkpoint 2 and request-activation; it has no opinion on how a
  pointer is actually read from or written to the tracker.  That is exactly
  the seam this module fills.
* :func:`start_run` — a thin, ordering-preserving wrapper that builds the
  callbacks and calls ``coordinator_state.bootstrap_run`` with them, so a
  caller never has to remember to wire the two together correctly.

Every mutation here — the native claim, the native pointer publish — goes
through :func:`direct_operation.execute`.  Nothing in this module calls
``safe_bd.run_profile`` directly for a mutation; the one direct ``runner``
call in :func:`pointer_callbacks` is a plain tracker *read* (``issue_get``),
which ``direct_operation`` itself does not treat as a mutation either (see
its own ``_observe`` helper).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).parent))

import beads_ownership
import coordinator_state as state
import direct_operation
import safe_bd
import schema_runtime

__all__ = [
    "LaneClaimResult",
    "claim_front",
    "pointer_callbacks",
    "start_run",
]

# The metadata key ``bootstrap_run`` publishes the run pointer under.  This is
# the tracker-side name for ``beads.run-pointer.v1``; it is not exported by
# ``coordinator_state`` or ``safe_bd``, so it lives here, next to the one
# module that reads and writes it.
_POINTER_METADATA_KEY = "hermes.beads_run.v1"

# Mirrors ``direct_operation._OBSERVATION_ERRORS`` exactly (that tuple is
# private, so it cannot be imported).  Any of these means "the tracker did
# not answer" rather than "the tracker answered no" — an unreadable
# observation, never a false absence.
_OBSERVATION_ERRORS = (safe_bd.SafeBdError, state.StateError, OSError)


# ---------------------------------------------------------------------------
# Pointer callbacks (AC-T12-003)
# ---------------------------------------------------------------------------


def _select_pointer(data: Any, *, issue_id: str) -> Any:
    """Dig the run-pointer value out of an ``issue_get`` payload.

    Returns the parsed pointer dict, :data:`direct_operation.ABSENT` when the
    issue exists but carries no pointer metadata yet, or ``None`` when the
    payload could not be read as a pointer at all (missing issue, wrong
    shape, or metadata that fails to parse as JSON) — an unusable
    observation, never a false absence.
    """
    issue = direct_operation.default_select(data, issue_id=issue_id)
    if not isinstance(issue, Mapping):
        return issue  # None (unusable) or ABSENT (issue itself missing).
    metadata = issue.get("metadata")
    raw = metadata.get(_POINTER_METADATA_KEY) if isinstance(metadata, Mapping) else None
    if raw is None:
        return direct_operation.ABSENT
    if not isinstance(raw, str):
        return None
    try:
        return schema_runtime.strict_json_loads(raw.encode("utf-8"))
    except schema_runtime.JsonLoadFailure:
        return None


def _pointer_observation(
    classification: str, observed: Any, *, intended: Mapping[str, object]
) -> state.PointerObservation:
    """Build a :class:`state.PointerObservation` that satisfies
    ``coordinator_state._validate_pointer_observation`` for every branch.

    That validator requires ``state_sha256`` to always be a valid 64-hex
    digest, requires an exact-match digest whenever ``observed_value`` is not
    ``None``, and — critically — requires ``state_sha256`` to equal the
    fixed genesis sentinel for ``prestate_unchanged`` rather than a computed
    hash of "nothing".  Tracing every call site in ``bootstrap_run`` shows
    the ``expected_before_sha256`` it compares against is always
    ``GENESIS_SHA256`` in practice (the first PREPARED write always records
    that value, and every later record inherits it verbatim), so hard-coding
    it here is not a shortcut, it is the actual contract.

    ``insufficient_observation`` carries no downstream constraint at all
    beyond "valid hex"; the genesis sentinel is reused there too, since no
    other value is externally agreed for "nothing was observed."
    """
    if classification == direct_operation.INTENDED_EFFECT_PRESENT:
        # A terminal short-circuit inside direct_operation.execute reports
        # classification without observed (observed=None) because it never
        # re-reads the tracker on a cached resolution.  We already know what
        # "present" means in that case: exactly the intended pointer.
        value = dict(observed) if isinstance(observed, Mapping) else dict(intended)
        digest = state.sha256_bytes(state.canonical_payload_bytes(value))
        return state.PointerObservation(classification, digest, value)
    if classification == direct_operation.CONFLICTING_EFFECT and isinstance(
        observed, Mapping
    ):
        value = dict(observed)
        digest = state.sha256_bytes(state.canonical_payload_bytes(value))
        return state.PointerObservation(classification, digest, value)
    # prestate_unchanged, insufficient_observation, or a conflicting_effect
    # whose evidence did not survive a terminal-shortcut replay: no field
    # constraint applies beyond a valid digest, so report the fixed sentinel.
    return state.PointerObservation(classification, state.GENESIS_SHA256, None)


def pointer_callbacks(
    request: state.StartRunInput,
    *,
    actor: str,
    runner: Callable[..., Any] | None = None,
    sensitive: Any = direct_operation.NO_SENSITIVE,
    crash_hook: Callable[[str], None] = state.NOOP_HOOK,
) -> state.PointerCallbacks:
    """Build the ``observe``/``publish`` pair ``bootstrap_run`` needs.

    ``request`` (not a ``RunContext``) is the only object the caller has
    *before* ``bootstrap_run`` creates the run directory, and it already
    carries the two things these closures need: ``repository_root`` (to
    read the tracker at all) and ``root_issue_id`` (which issue's metadata
    the pointer lives on).  ``publish`` opens its own
    :class:`direct_operation.RunContext` lazily, on each call, using the
    ``run_id`` embedded in the pointer dict it is given — by the time
    ``bootstrap_run`` calls ``publish`` the run directory already exists.
    """
    dispatch = runner if runner is not None else safe_bd.run_profile
    repository_root = Path(request.repository_root)
    root_issue_id = request.root_issue_id
    run_root = Path(request.run_root)

    def _read(pointer: Mapping[str, object]) -> tuple[Any, str]:
        try:
            result = dispatch(
                safe_bd.SafeBdRequest(
                    "issue_get", {"issue_id": root_issue_id}, repository_root, None
                ),
                sensitive=sensitive,
            )
        except _OBSERVATION_ERRORS:
            return None, direct_operation.INSUFFICIENT_OBSERVATION
        if getattr(result, "status", None) != "ok":
            return None, direct_operation.INSUFFICIENT_OBSERVATION
        observed = _select_pointer(result.data, issue_id=root_issue_id)
        classification = direct_operation.classify_observation(
            observed, intended=dict(pointer), prestate=direct_operation.ABSENT
        )
        return observed, classification

    def observe(pointer: Mapping[str, object]) -> state.PointerObservation:
        observed, classification = _read(pointer)
        return _pointer_observation(classification, observed, intended=pointer)

    def publish(pointer: Mapping[str, object]) -> state.PointerObservation:
        run_directory = run_root / str(pointer["run_id"])
        context = direct_operation.open_run(run_directory, crash_hook=crash_hook)
        value = state.canonical_payload_bytes(dict(pointer)).decode("utf-8")
        raw_ownership_epoch = pointer.get("ownership_epoch")
        ownership_epoch = (
            None
            if isinstance(raw_ownership_epoch, bool)
            or not isinstance(raw_ownership_epoch, int)
            else raw_ownership_epoch
        )
        operation = direct_operation.DirectOperation(
            # Content-addressed: identical pointer content always maps to
            # the same caller key, so a retry with the same pointer always
            # converges on the same operation_id inside execute().
            caller_key=f"run-pointer/{state.sha256_bytes(state.canonical_payload_bytes(pointer))}",
            effect_type="TRACKER_RUN_POINTER",
            target_identity=f"bd://issue/{root_issue_id}#run-pointer",
            issue_id=root_issue_id,
            ownership_epoch=ownership_epoch,
            arguments={"issue_id": root_issue_id, "value": value},
            readback=direct_operation.Readback(
                profile="issue_get",
                arguments={"issue_id": root_issue_id},
                intended=dict(pointer),
                prestate=direct_operation.ABSENT,
                select=lambda data: _select_pointer(data, issue_id=root_issue_id),
            ),
            # The marker note exists to guard non-idempotent effects across
            # attempts a run's own journal cannot see.  TRACKER_RUN_POINTER is
            # a metadata *set*, safe to redispatch verbatim, and its caller
            # key is already content-addressed on the full pointer, so the
            # journal alone fully covers idempotency here; a marker comment
            # on the root issue for every bootstrap attempt would be noise
            # with no corresponding safety gap to close.
            marker_enabled=False,
        )
        result = direct_operation.execute(
            context, operation, actor=actor, runner=dispatch, sensitive=sensitive
        )
        return _pointer_observation(
            result.classification, result.observed, intended=pointer
        )

    return state.PointerCallbacks(observe=observe, publish=publish)


def start_run(
    request: state.StartRunInput,
    *,
    actor: str,
    now: str,
    runner: Callable[..., Any] | None = None,
    sensitive: Any = direct_operation.NO_SENSITIVE,
    run_id_factory: Callable[[], str] | None = None,
    secret_factory: Callable[[], bytes] | None = None,
    crash_hook: Callable[[str], None] = state.NOOP_HOOK,
) -> state.BootstrapResult:
    """Drive AC-T12-003's ordering gate: pointer publish, checkpoint 2, then
    request-active — in that order, every time, resumable at every step.

    This is deliberately thin.  The ordering itself (root-pointer
    publication and readback must reach APPLIED before checkpoint 2 is
    accepted, which must complete before the request mapping flips to
    "active") is enforced by ``coordinator_state.bootstrap_run`` alone; this
    wrapper's only job is to make sure ``bootstrap_run`` is never called
    without the matching pointer callbacks, so that guarantee can never be
    bypassed by a caller that forgets to wire the two together.
    """
    callbacks = pointer_callbacks(
        request, actor=actor, runner=runner, sensitive=sensitive, crash_hook=crash_hook
    )
    kwargs: dict[str, Any] = {"now": now, "crash_hook": crash_hook}
    if run_id_factory is not None:
        kwargs["run_id_factory"] = run_id_factory
    if secret_factory is not None:
        kwargs["secret_factory"] = secret_factory
    return state.bootstrap_run(request, callbacks, **kwargs)


# ---------------------------------------------------------------------------
# Claim-front (AC-T12-001)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneClaimResult:
    """One lane's outcome from :func:`claim_front`.

    ``status`` is one of the four-way outcomes (``APPLIED``, ``NOT_APPLIED``,
    ``CONFLICT``, ``UNKNOWN``) or ``REFUSED`` for a lane that was never
    attempted at all — currently only the root issue.
    """

    issue_id: str
    status: str
    classification: str | None
    ownership_epoch: int | None
    error_code: str | None


def _release_operation_id(*, run_id: str, issue_id: str, actor: str, epoch: int) -> str:
    """A deterministic id for the compensating release, distinct from the
    paired acquire's id.

    ``beads_ownership._append_event`` treats operation_id reuse as a hard
    conflict unless the reused id's *own* prior event was a matching
    ``release_prepared`` -> ``released`` pair.  An acquire's own history
    event has ``status="active"``, so reusing the acquire's operation_id for
    the release call fails closed on the very first write.  Keying this id
    on a distinct ``effect_type`` guarantees it never collides with the
    acquire id derived from the same inputs.
    """
    payload = {"run_id": run_id, "issue_id": issue_id, "actor": actor, "epoch": epoch}
    return state.semantic_operation_id(
        {
            "schema": "beads.claim-front-ownership.v1",
            "effect_type": "CLAIM_FRONT_RELEASE",
            "target_identity": issue_id,
            "immutable_input_sha256": state.sha256_bytes(
                state.canonical_payload_bytes(payload)
            ),
            "ownership_epoch": epoch,
        }
    )


def _acquire_operation_id(*, run_id: str, issue_id: str, actor: str, epoch: int) -> str:
    payload = {"run_id": run_id, "issue_id": issue_id, "actor": actor, "epoch": epoch}
    return state.semantic_operation_id(
        {
            "schema": "beads.claim-front-ownership.v1",
            "effect_type": "OWNERSHIP_ACQUIRE",
            "target_identity": issue_id,
            "immutable_input_sha256": state.sha256_bytes(
                state.canonical_payload_bytes(payload)
            ),
            "ownership_epoch": epoch,
        }
    )


def _acquire_local_ownership(
    *,
    ownership: beads_ownership.OwnershipStore,
    context: direct_operation.RunContext,
    issue_id: str,
    actor: str,
) -> tuple[str, int | None]:
    """Inspect-then-branch local ownership acquire, mirroring
    ``coordinator_state.bootstrap_run``'s own root-ownership idiom exactly
    (not a fixed-epoch placeholder): reuse an already-held record for this
    run, otherwise acquire the next epoch, otherwise report the pre-existing
    disposition without ever calling ``acquire`` on a state it does not own.

    Returns ``("held", epoch)`` on success or ``(disposition, None)`` for
    every case where this lane must not be dispatched — ``"conflict"``
    (held by someone else, or a racing acquire), or ``"unknown"``.
    """
    try:
        inspected = ownership.inspect(issue_id)
    except beads_ownership.OwnershipError:
        return "unknown", None

    if inspected.disposition == "held":
        record = inspected.record
        if record is not None and record.get("run_id") == context.run_id:
            return "held", record["epoch"]
        return "conflict", None

    if inspected.disposition not in ("unheld", "released"):
        # "conflict" or "unknown" at the inspect stage: never acquire on top
        # of a state we do not understand.
        return inspected.disposition, None

    proposed_epoch = (inspected.record["epoch"] + 1) if inspected.record else 1
    operation_id = _acquire_operation_id(
        run_id=context.run_id, issue_id=issue_id, actor=actor, epoch=proposed_epoch
    )
    try:
        acquired = ownership.acquire(
            issue_id=issue_id,
            actor=actor,
            run_directory=context.run_directory,
            # bootstrap_run's own root-ownership acquire passes this same
            # fixed sentinel, not a computed digest of tracker state; there
            # is no other value this call site is defined against.
            tracker_state_sha256=state.GENESIS_SHA256,
            operation_id=operation_id,
            now=None,
        )
    except beads_ownership.OwnershipError:
        return "unknown", None

    if acquired.disposition != "held" or acquired.record is None:
        return "conflict", None
    return "held", acquired.record["epoch"]


def claim_front(
    context: direct_operation.RunContext,
    *,
    ownership: beads_ownership.OwnershipStore,
    issues: Sequence[str],
    actor: str,
    runner: Callable[..., Any] | None = None,
    sensitive: Any = direct_operation.NO_SENSITIVE,
) -> list[LaneClaimResult]:
    """Claim every eligible lane in ``issues``, one guarded operation each.

    The root issue (``context.manifest["root_issue_id"]``) is never
    eligible: it is refused outright, with no ownership inspect and no
    dispatch, because the root is the coordination anchor, not a lane to
    claim.  For every other issue: acquire local ownership first (skipping
    the acquire if this run already holds it), then dispatch the native
    claim through :func:`direct_operation.execute` under that epoch.  If the
    native tracker does not converge on what was intended (``NOT_APPLIED``
    or ``CONFLICT``), the local ownership just acquired is released again —
    holding it would leave this run believing it owns a lane the tracker
    disagrees about.  ``UNKNOWN`` never triggers a release: an unreadable
    post-effect probe means the claim's real outcome is unknown, and
    releasing ownership on top of that uncertainty could hand the lane to
    another run while the native claim may in fact have gone through.
    """
    dispatch = runner if runner is not None else safe_bd.run_profile
    root_issue_id = context.manifest["root_issue_id"]
    results: list[LaneClaimResult] = []

    for issue_id in issues:
        if issue_id == root_issue_id:
            results.append(
                LaneClaimResult(
                    issue_id=issue_id,
                    status="REFUSED",
                    classification=None,
                    ownership_epoch=None,
                    error_code="COORDINATOR_TRACKER_ROOT_ISSUE_INELIGIBLE",
                )
            )
            continue

        disposition, epoch = _acquire_local_ownership(
            ownership=ownership, context=context, issue_id=issue_id, actor=actor
        )
        if disposition != "held":
            status = "UNKNOWN" if disposition == "unknown" else "CONFLICT"
            results.append(
                LaneClaimResult(
                    issue_id=issue_id,
                    status=status,
                    classification=None,
                    ownership_epoch=None,
                    error_code=f"COORDINATOR_TRACKER_OWNERSHIP_{status}",
                )
            )
            continue

        # disposition == "held" guarantees epoch is set; the two are
        # correlated by _acquire_local_ownership's own contract, not by its
        # return type.
        assert epoch is not None

        operation = direct_operation.DirectOperation(
            caller_key=f"claim-front/{issue_id}",
            effect_type="TRACKER_CLAIM",
            target_identity=f"bd://issue/{issue_id}",
            issue_id=issue_id,
            ownership_epoch=epoch,
            arguments={"issue_id": issue_id},
            readback=direct_operation.Readback(
                profile="issue_get",
                arguments={"issue_id": issue_id},
                intended={"status": "in_progress", "assignee": actor},
                prestate={"status": "open"},
            ),
        )
        outcome = direct_operation.execute(
            context, operation, actor=actor, runner=dispatch, sensitive=sensitive
        )

        if outcome.status in ("NOT_APPLIED", "CONFLICT"):
            ownership.release(
                issue_id,
                context.run_directory,
                epoch=epoch,
                operation_id=_release_operation_id(
                    run_id=context.run_id, issue_id=issue_id, actor=actor, epoch=epoch
                ),
                now=None,
            )

        results.append(
            LaneClaimResult(
                issue_id=issue_id,
                status=outcome.status,
                classification=outcome.classification,
                ownership_epoch=epoch,
                error_code=outcome.error_code,
            )
        )

    return results
