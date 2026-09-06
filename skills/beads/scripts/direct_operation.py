#!/usr/bin/env python3
"""Direct tracker operations: intent, marker, effect, probe, resolution.

Every tracker mutation the coordinator makes runs through :func:`execute`.  The
saga exists because a native ``bd`` call can fail in a way that leaves the
tracker changed anyway — a timeout, a killed process, a lost pipe.  Trusting
the exit code alone would let the coordinator report ``NOT_APPLIED`` for a
close that actually closed.  So the effect is always bracketed:

1. **intent** — the exact request is frozen to an immutable file, one per
   stable caller key.  Re-running the same caller key with a different payload
   is a ``CONFLICT`` and reaches the tracker not at all.
2. **marker** — a run-scoped note is appended to the issue before the effect
   (after it, for creates, since the issue does not exist yet).  The marker is
   what tells a later attempt whether *this* run caused the state it sees.
3. **effect** — the single ``safe_bd`` mutation profile for this effect type.
4. **probe** — an independent readback, classified into exactly the four
   ``recovery-probe-v1`` classifications.
5. **resolution** — an ``operation-journal-event-v1`` RESOLUTION carrying the
   probe evidence and one of ``APPLIED``/``NOT_APPLIED``/``CONFLICT``/
   ``UNKNOWN``.

The probe, never the exit code, decides the status.  A successful-looking
mutation whose readback disagrees resolves ``CONFLICT`` or ``UNKNOWN``.

This module is the base of the guarded-tracker layer: ``coordinator_tracker``,
``protected_action`` and ``coordinator_handoff`` import the journal-event
builders, the status constants and :class:`RunContext` from here, because
``coordinator_state`` keeps its own equivalents private to the bootstrap path.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).parent))

import coordinator_state as state  # noqa: E402
import safe_bd  # noqa: E402
import safe_output  # noqa: E402

__all__ = [
    "ABSENT",
    "APPLIED",
    "CLASSIFICATION_STATUS",
    "CONFLICT",
    "CONFLICTING_EFFECT",
    "DirectOperation",
    "DirectOperationError",
    "DirectOperationResult",
    "EFFECT_PROFILES",
    "INSUFFICIENT_OBSERVATION",
    "INTENDED_EFFECT_PRESENT",
    "MARKER_AFTER_EFFECT",
    "NOT_APPLIED",
    "PRESTATE_UNCHANGED",
    "REPLAY_GUARDED_EFFECTS",
    "Readback",
    "RunContext",
    "UNKNOWN",
    "classify_observation",
    "default_select",
    "execute",
    "marker_text",
    "open_run",
    "operations_directory",
    "prepared_event",
    "probe_result",
    "resolution_event",
    "tracker_probe",
    "utc_now",
]

# The four-way outcome taxonomy.  Every module in this layer reports one of
# these and nothing else; the tracker, the harness and the durable executor all
# funnel their observations into the same vocabulary.
APPLIED = "APPLIED"
NOT_APPLIED = "NOT_APPLIED"
CONFLICT = "CONFLICT"
UNKNOWN = "UNKNOWN"

INTENDED_EFFECT_PRESENT = "intended_effect_present"
PRESTATE_UNCHANGED = "prestate_unchanged"
CONFLICTING_EFFECT = "conflicting_effect"
INSUFFICIENT_OBSERVATION = "insufficient_observation"

CLASSIFICATION_STATUS = {
    INTENDED_EFFECT_PRESENT: APPLIED,
    PRESTATE_UNCHANGED: NOT_APPLIED,
    CONFLICTING_EFFECT: CONFLICT,
    INSUFFICIENT_OBSERVATION: UNKNOWN,
}

#: Sentinel for "the readback says this record is not there".  It is a plain
#: string so it survives the canonical-JSON round trip into the intent file;
#: no tracker field can legitimately hold this value.
ABSENT = "beads.direct-operation.absent.v1"

EFFECT_PROFILES = {
    "TRACKER_CLAIM": "claim_exact",
    "TRACKER_RESTORE_CLAIM": "restore_claim_fields",
    "TRACKER_NOTE": "append_marker_note",
    "TRACKER_CLOSE": "close_exact",
    "TRACKER_CREATE": "create_exact",
    "TRACKER_DEPENDENCY_ADD": "dependency_add_exact",
    "TRACKER_DEPENDENCY_REMOVE": "dependency_remove_exact",
    "TRACKER_RUN_POINTER": "set_run_pointer",
}

#: Effects whose replay can destroy work someone else did.  Re-closing an issue
#: a human reopened, re-creating an id, or re-adding a removed edge are all
#: irreversible from the coordinator's side, so an ambiguous prior attempt on
#: these must resolve UNKNOWN rather than dispatch again.
REPLAY_GUARDED_EFFECTS = frozenset(
    {
        "TRACKER_CLOSE",
        "TRACKER_CREATE",
        "TRACKER_DEPENDENCY_ADD",
        "TRACKER_DEPENDENCY_REMOVE",
    }
)

#: A create has no issue to carry a marker until it succeeds, so its marker is
#: appended afterwards.  Every other effect writes the marker first, which is
#: what makes "marker absent" evidence that the effect never ran.
MARKER_AFTER_EFFECT = frozenset({"TRACKER_CREATE"})

#: Guard codes that mean "we could not establish what happened".  The remaining
#: guard, ``DIRECT_OPERATION_MARKER_UNAVAILABLE``, is decisive the other way:
#: nothing was dispatched, so the outcome is a plain NOT_APPLIED.
_AMBIGUITY_GUARDS = frozenset(
    {"DIRECT_OPERATION_REPLAY_BLOCKED", "DIRECT_OPERATION_AMBIGUOUS_CAUSALITY"}
)

MARKER_PREFIX = "beads-direct-op-v1"
OPERATIONS_DIRNAME = "_operations"
PROBE_TIMEOUT_SECONDS = 30
PROBE_AUTHORITY = "tracker_write"
MAX_INTENT_BYTES = 16_384
MAX_EVIDENCE_BYTES = 65_536
MAX_ATTEMPTS = 999
AUTHORITY_CLASS = "coordinator_parent"
GENESIS = state.GENESIS_SHA256
NO_SENSITIVE = safe_output.SensitiveSet(())

# Errors a readback may legitimately raise; anything else (an assertion from a
# test's command-denial spy, for instance) must propagate rather than be
# laundered into "insufficient observation".
_OBSERVATION_ERRORS = (safe_bd.SafeBdError, state.StateError, OSError)


class DirectOperationError(ValueError):
    """Typed fail-closed refusal (``.code``, ``.status``)."""

    def __init__(self, code: str, *, status: str = CONFLICT) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass
class RunContext:
    """Everything a guarded operation needs from an open run directory."""

    run_directory: Path
    manifest: dict[str, Any]
    journal: state.OperationJournal
    checkpoints: state.CheckpointStore
    crash_hook: Callable[[str], None]

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    @property
    def repository_root(self) -> Path:
        return Path(self.manifest["repository_root"])


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def open_run(
    run_directory: Path, *, crash_hook: Callable[[str], None] = state.NOOP_HOOK
) -> RunContext:
    manifest = state.load_run_manifest(run_directory)
    journal = state.OperationJournal.create(run_directory)
    checkpoints = state.CheckpointStore.open_existing(run_directory, journal)
    return RunContext(run_directory, manifest, journal, checkpoints, crash_hook)


def operations_directory(run_directory: Path) -> Path:
    """Return (creating if needed) the run-owned directory for operation files."""
    target = run_directory / OPERATIONS_DIRNAME
    state.ensure_owner_directory(target, root=run_directory)
    return target


# ---------------------------------------------------------------------------
# Journal events.  ``coordinator_state`` keeps equivalents private to its
# bootstrap path; these are the public builders the whole guarded layer shares.
# ---------------------------------------------------------------------------


def prepared_event(
    *,
    run_id: str,
    operation_id: str,
    effect_type: str,
    input_path: Path,
    input_sha256: str,
    expected_pre_state_sha256: str,
    probe: Mapping[str, Any],
    timestamp: str,
    issue_id: str | None,
    ownership_epoch: int | None,
    attempt_id: str | None = None,
    authority_class: str = AUTHORITY_CLASS,
) -> dict[str, Any]:
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": operation_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "issue_id": issue_id,
        "ownership_epoch": ownership_epoch,
        "effect_type": effect_type,
        "immutable_input_path": str(Path(input_path).resolve()),
        "immutable_input_sha256": input_sha256,
        "expected_pre_state_sha256": expected_pre_state_sha256,
        "recovery_probe": dict(probe),
        "timestamp": timestamp,
        "authority_class": authority_class,
        "previous_event_sha256": GENESIS,
        "phase": "PREPARED",
    }


def resolution_event(
    prepared: Mapping[str, Any],
    *,
    status: str,
    observed_post_state_sha256: str | None,
    readback_evidence_path: Path | None,
    readback_evidence_sha256: str | None,
    timestamp: str,
    error_code: str | None = None,
    template_id: str = "direct_operation_unresolved",
    field_path: str = "/direct_operation",
) -> dict[str, Any]:
    if status not in CLASSIFICATION_STATUS.values():
        raise DirectOperationError("OPERATION_STATUS_INVALID")
    error: dict[str, Any] | None = None
    if status in {CONFLICT, UNKNOWN} or error_code is not None:
        error = {
            "code": error_code or f"DIRECT_OPERATION_{status}",
            "template_id": template_id,
            "field_path": field_path,
            "parameters": [],
        }
    return {
        **dict(prepared),
        "phase": "RESOLUTION",
        "timestamp": timestamp,
        "observed_post_state_sha256": observed_post_state_sha256,
        "readback_evidence_path": (
            str(Path(readback_evidence_path).resolve())
            if readback_evidence_path
            else None
        ),
        "readback_evidence_sha256": readback_evidence_sha256,
        "status": status,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def tracker_probe(
    *,
    target_identity: str,
    expected_before_sha256: str,
    intended_after_sha256: str,
    descriptor: Sequence[str],
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    required_authority: str = PROBE_AUTHORITY,
) -> dict[str, Any]:
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "tracker_state",
        "target_identity": target_identity,
        "expected_before_sha256": expected_before_sha256,
        "intended_after_identity": target_identity,
        "intended_after_sha256": intended_after_sha256,
        "descriptor": [str(part) for part in descriptor],
        "timeout_seconds": int(timeout_seconds),
        "required_authority": required_authority,
    }


def probe_result(
    request: Mapping[str, Any],
    *,
    classification: str,
    observed_identity: str | None,
    observed_sha256: str | None,
    observed_state: str,
    evidence_path: Path,
    evidence_sha256: str,
    observed_at: str,
) -> dict[str, Any]:
    if classification not in CLASSIFICATION_STATUS:
        raise DirectOperationError("PROBE_CLASSIFICATION_INVALID")
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "result",
        "probe_type": request["probe_type"],
        "target_identity": request["target_identity"],
        "observed_identity": observed_identity,
        "observed_sha256": observed_sha256,
        "observed_state": observed_state,
        "evidence_path": str(Path(evidence_path).resolve()),
        "evidence_sha256": evidence_sha256,
        "observed_at": observed_at,
        "classification": classification,
    }


def classify_observation(observed: Any, *, intended: Any, prestate: Any) -> str:
    """Fold one readback into the canonical four-way classification.

    ``observed`` is ``None`` when the readback could not be trusted at all,
    :data:`ABSENT` when it positively reported the record missing, or a mapping
    of the record's fields.  ``intended`` and ``prestate`` take the same shape:
    either :data:`ABSENT` or a mapping of the fields that must match.
    """
    if observed is None:
        return INSUFFICIENT_OBSERVATION
    if _matches(observed, intended):
        return INTENDED_EFFECT_PRESENT
    if _matches(observed, prestate):
        return PRESTATE_UNCHANGED
    return CONFLICTING_EFFECT


def _matches(observed: Any, expected: Any) -> bool:
    if expected is ABSENT or expected == ABSENT:
        return observed is ABSENT or observed == ABSENT
    if observed is ABSENT or observed == ABSENT or not isinstance(observed, Mapping):
        return False
    if not isinstance(expected, Mapping) or not expected:
        return False
    return all(observed.get(key) == value for key, value in expected.items())


def default_select(data: Any, *, issue_id: str | None) -> Any:
    """Dig the issue record out of a ``safe_bd`` payload.

    Returns the record mapping, :data:`ABSENT` when the tracker answered but
    held no such record, or ``None`` when the payload shape is not one this
    function can read — an unusable observation, not an absent one.
    """
    if data is None:
        return None
    records: Any = data
    if isinstance(data, Mapping):
        if "id" in data:
            return dict(data)
        for key in ("issue", "issues", "data", "results"):
            if key in data:
                records = data[key]
                break
        else:
            return None
    if records is None:
        return ABSENT
    if isinstance(records, Mapping):
        if issue_id is None or records.get("id") == issue_id:
            return dict(records)
        return ABSENT
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        for item in records:
            if isinstance(item, Mapping) and (
                issue_id is None or item.get("id") == issue_id
            ):
                return dict(item)
        return ABSENT
    return None


def marker_text(*, run_id: str, intent_sha256: str, effect_type: str) -> str:
    """Build the run-scoped attribution note for one caller key.

    Keyed on the intent digest rather than the operation id, so every attempt
    at the same caller key writes and recognises the same marker.
    """
    return f"{MARKER_PREFIX} run={run_id} intent={intent_sha256} effect={effect_type}"


# ---------------------------------------------------------------------------
# Operation description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readback:
    """How to observe the effect independently of the mutation's exit code."""

    profile: str
    arguments: Mapping[str, Any]
    intended: Any
    prestate: Any
    # Runtime-only; never serialised into the intent, so a changed selector can
    # never silently redefine a frozen caller key.
    select: Callable[[Any], Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class DirectOperation:
    """One tracker mutation under a stable caller key."""

    caller_key: str
    effect_type: str
    target_identity: str
    issue_id: str | None
    ownership_epoch: int | None
    arguments: Mapping[str, Any]
    readback: Readback
    marker_issue_id: str | None = None
    marker_enabled: bool = True

    def __post_init__(self) -> None:
        if self.effect_type not in EFFECT_PROFILES:
            raise DirectOperationError("DIRECT_OPERATION_EFFECT_UNKNOWN")
        if not self.caller_key or "\x00" in self.caller_key:
            raise DirectOperationError("DIRECT_OPERATION_CALLER_KEY_INVALID")
        if "actor" in self.arguments:
            raise DirectOperationError("DIRECT_OPERATION_ACTOR_NOT_CALLER_SUPPLIED")

    @property
    def profile(self) -> str:
        return EFFECT_PROFILES[self.effect_type]

    @property
    def marker_target(self) -> str | None:
        return self.marker_issue_id or self.issue_id


@dataclass(frozen=True)
class DirectOperationResult:
    status: str
    caller_key: str
    operation_id: str | None
    effect_type: str
    attempt_id: str | None
    classification: str
    observed: Any
    marker_present: bool | None
    dispatched: bool
    error_code: str | None
    prepared: dict[str, Any] | None
    resolution: dict[str, Any] | None
    evidence_path: Path | None


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _intent_payload(
    context: RunContext, operation: DirectOperation, *, actor: str
) -> dict[str, Any]:
    return {
        "schema_version": "beads.direct-operation-intent.v1",
        "run_id": context.run_id,
        "caller_key": operation.caller_key,
        "effect_type": operation.effect_type,
        "profile": operation.profile,
        "target_identity": operation.target_identity,
        "issue_id": operation.issue_id,
        "marker_issue_id": operation.marker_target,
        "ownership_epoch": operation.ownership_epoch,
        "actor": actor,
        "arguments": _plain(operation.arguments),
        "readback": {
            "profile": operation.readback.profile,
            "arguments": _plain(operation.readback.arguments),
            "intended": _plain(operation.readback.intended),
            "prestate": _plain(operation.readback.prestate),
        },
    }


def _read_owned_bytes(path: Path, *, root: Path, max_bytes: int) -> bytes:
    checked = state.validate_owner_file(path, root=root)
    fd = os.open(checked, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise DirectOperationError("DIRECT_OPERATION_INTENT_TOO_LARGE", status=UNKNOWN)
    return raw


def _freeze_intent(
    context: RunContext, operation: DirectOperation, *, actor: str
) -> tuple[Path, str, bool]:
    """Publish the intent file for this caller key.

    Returns ``(path, sha256, conflict)``.  ``conflict`` is true when the caller
    key already holds a *different* frozen request — the changed-intent case,
    which must never reach the tracker.
    """
    directory = operations_directory(context.run_directory)
    raw = state.canonical_bytes(_intent_payload(context, operation, actor=actor))
    key_digest = state.sha256_bytes(
        state.canonical_payload_bytes(
            {"run_id": context.run_id, "caller_key": operation.caller_key}
        )
    )
    path = directory / f"{key_digest}.intent.json"
    if path.exists():
        existing = _read_owned_bytes(path, root=directory, max_bytes=MAX_INTENT_BYTES)
        if existing != raw:
            return path, state.sha256_bytes(existing), True
        return path, state.sha256_bytes(raw), False
    state.atomic_write(
        path,
        raw,
        root=context.run_directory,
        max_bytes=MAX_INTENT_BYTES,
        hook=context.crash_hook,
    )
    return path, state.sha256_bytes(raw), False


def _write_evidence(
    context: RunContext, operation_id: str, payload: Mapping[str, Any]
) -> tuple[Path, str]:
    directory = operations_directory(context.run_directory)
    raw = state.canonical_bytes(_plain(payload))
    path = directory / f"{operation_id}.evidence.json"
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


# ---------------------------------------------------------------------------
# Tracker access
# ---------------------------------------------------------------------------


def _request(
    context: RunContext, profile: str, arguments: Mapping[str, Any]
) -> safe_bd.SafeBdRequest:
    return safe_bd.SafeBdRequest(
        profile, dict(arguments), context.repository_root, None
    )


def _observe(
    context: RunContext,
    operation: DirectOperation,
    *,
    runner: Callable[..., Any],
    sensitive: Any,
) -> tuple[Any, str]:
    readback = operation.readback
    try:
        result = runner(
            _request(context, readback.profile, readback.arguments),
            sensitive=sensitive,
        )
    except _OBSERVATION_ERRORS:
        return None, INSUFFICIENT_OBSERVATION
    if getattr(result, "status", None) != "ok":
        return None, INSUFFICIENT_OBSERVATION
    selector = readback.select
    observed = (
        selector(result.data)
        if selector is not None
        else default_select(result.data, issue_id=operation.issue_id)
    )
    return observed, classify_observation(
        observed, intended=readback.intended, prestate=readback.prestate
    )


def _marker_seen(
    context: RunContext,
    operation: DirectOperation,
    *,
    marker: str,
    runner: Callable[..., Any],
    sensitive: Any,
) -> bool | None:
    """Report whether this run's marker is already on the issue.

    ``None`` means the comment history could not be read, which is itself an
    ambiguity the caller must respect rather than treat as "no marker".
    """
    target = operation.marker_target
    if target is None or not operation.marker_enabled:
        return False
    try:
        result = runner(
            _request(context, "issue_comments", {"issue_id": target}),
            sensitive=sensitive,
        )
    except _OBSERVATION_ERRORS:
        return None
    if getattr(result, "status", None) != "ok":
        return None
    return _contains_marker(result.data, marker)


def _contains_marker(data: Any, marker: str) -> bool:
    if data is None:
        return False
    if isinstance(data, str):
        return marker in data
    if isinstance(data, Mapping):
        return any(_contains_marker(value, marker) for value in data.values())
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return any(_contains_marker(item, marker) for item in data)
    return False


def _append_marker(
    context: RunContext,
    operation: DirectOperation,
    *,
    marker: str,
    actor: str,
    runner: Callable[..., Any],
    sensitive: Any,
) -> bool:
    target = operation.marker_target
    if target is None or not operation.marker_enabled:
        return False
    try:
        result = runner(
            _request(
                context,
                "append_marker_note",
                {"issue_id": target, "actor": actor, "content": marker},
            ),
            sensitive=sensitive,
        )
    except _OBSERVATION_ERRORS:
        return False
    return getattr(result, "status", None) == "ok"


# ---------------------------------------------------------------------------
# The saga
# ---------------------------------------------------------------------------


def _state_digest(observed: Any) -> str:
    return state.sha256_bytes(state.canonical_bytes({"observed": _plain(observed)}))


def _observed_state_label(observed: Any) -> str:
    if observed is None:
        return "unreadable"
    if observed is ABSENT or observed == ABSENT:
        return "absent"
    return "present"


def _refused(
    operation: DirectOperation,
    *,
    status: str,
    classification: str,
    error_code: str,
    observed: Any = None,
    marker_present: bool | None = None,
    operation_id: str | None = None,
) -> DirectOperationResult:
    return DirectOperationResult(
        status=status,
        caller_key=operation.caller_key,
        operation_id=operation_id,
        effect_type=operation.effect_type,
        attempt_id=None,
        classification=classification,
        observed=observed,
        marker_present=marker_present,
        dispatched=False,
        error_code=error_code,
        prepared=None,
        resolution=None,
        evidence_path=None,
    )


def execute(
    context: RunContext,
    operation: DirectOperation,
    *,
    actor: str,
    runner: Callable[..., Any] | None = None,
    sensitive: Any = NO_SENSITIVE,
    now: Callable[[], str] = utc_now,
) -> DirectOperationResult:
    """Run one direct tracker operation to a four-way outcome.

    ``runner`` defaults to :func:`safe_bd.run_profile`; tests inject a scripted
    stand-in so a denied profile raises instead of silently succeeding.
    """
    dispatch = runner if runner is not None else safe_bd.run_profile

    intent_path, intent_sha, changed = _freeze_intent(context, operation, actor=actor)
    if changed:
        # The caller key is frozen to a different request.  Nothing is sent.
        return _refused(
            operation,
            status=CONFLICT,
            classification=CONFLICTING_EFFECT,
            error_code="DIRECT_OPERATION_INTENT_CONFLICT",
        )

    records = context.journal.read().records
    prior = [r for r in records if r.get("immutable_input_sha256") == intent_sha]
    resolutions = [r for r in prior if r.get("phase") == "RESOLUTION"]
    for record in resolutions:
        if record.get("status") in {APPLIED, CONFLICT}:
            # Terminal already.  Re-running must not touch the tracker again.
            return DirectOperationResult(
                status=str(record["status"]),
                caller_key=operation.caller_key,
                operation_id=str(record["operation_id"]),
                effect_type=operation.effect_type,
                attempt_id=record.get("attempt_id"),
                classification=(
                    INTENDED_EFFECT_PRESENT
                    if record["status"] == APPLIED
                    else CONFLICTING_EFFECT
                ),
                observed=None,
                marker_present=None,
                dispatched=False,
                error_code=(record.get("error") or {}).get("code"),
                prepared=None,
                resolution=dict(record),
                evidence_path=None,
            )

    attempt = len(resolutions) + 1
    if attempt > MAX_ATTEMPTS:
        return _refused(
            operation,
            status=UNKNOWN,
            classification=INSUFFICIENT_OBSERVATION,
            error_code="DIRECT_OPERATION_ATTEMPT_LIMIT",
        )
    attempt_id = f"attempt-{attempt:03d}"

    operation_id = state.semantic_operation_id(
        {
            "schema": "beads.direct-operation.v1",
            "effect_type": operation.effect_type,
            "target_identity": f"{operation.caller_key}#{attempt_id}",
            "immutable_input_sha256": intent_sha,
            "ownership_epoch": operation.ownership_epoch,
        }
    )
    dangling = [
        r
        for r in prior
        if r.get("phase") == "PREPARED" and r.get("operation_id") == operation_id
    ]
    # A dangling PREPARED means a previous process died somewhere between the
    # journal write and the resolution: the effect may or may not have landed.
    ambiguous_prior = bool(dangling) or any(
        r.get("status") == UNKNOWN for r in resolutions
    )

    marker = marker_text(
        run_id=context.run_id,
        intent_sha256=intent_sha,
        effect_type=operation.effect_type,
    )
    marker_present = _marker_seen(
        context, operation, marker=marker, runner=dispatch, sensitive=sensitive
    )
    observed, classification = _observe(
        context, operation, runner=dispatch, sensitive=sensitive
    )
    pre_state_sha = _state_digest(observed)

    if dangling:
        prepared = dict(dangling[0])
        probe = dict(prepared["recovery_probe"])
    else:
        probe = tracker_probe(
            target_identity=operation.target_identity,
            expected_before_sha256=pre_state_sha,
            intended_after_sha256=_state_digest(operation.readback.intended),
            descriptor=["beads", "tracker", operation.effect_type],
        )
        prepared = prepared_event(
            run_id=context.run_id,
            operation_id=operation_id,
            effect_type=operation.effect_type,
            input_path=intent_path,
            input_sha256=intent_sha,
            expected_pre_state_sha256=pre_state_sha,
            probe=probe,
            timestamp=now(),
            issue_id=operation.issue_id,
            ownership_epoch=operation.ownership_epoch,
            attempt_id=attempt_id,
        )
        prepared = context.journal.append(prepared, hook=context.crash_hook)
        context.crash_hook("after_operation_prepared")

    guard = _replay_guard(
        operation,
        classification=classification,
        marker_present=marker_present,
        ambiguous_prior=ambiguous_prior,
    )
    dispatched = False
    if guard is None and classification == PRESTATE_UNCHANGED:
        marker_ok = True
        if operation.effect_type not in MARKER_AFTER_EFFECT and not marker_present:
            marker_ok = _append_marker(
                context,
                operation,
                marker=marker,
                actor=actor,
                runner=dispatch,
                sensitive=sensitive,
            )
            context.crash_hook("after_operation_marker")
        if not marker_ok and operation.effect_type in REPLAY_GUARDED_EFFECTS:
            # The marker is what a later attempt reads to decide whether this
            # run caused the state it sees.  Dispatching a destructive effect
            # without one would leave that question permanently unanswerable,
            # so the operation stops here having changed nothing.
            guard = "DIRECT_OPERATION_MARKER_UNAVAILABLE"
        else:
            dispatched = True
            effect_ok = _dispatch_effect(
                context, operation, actor=actor, runner=dispatch, sensitive=sensitive
            )
            context.crash_hook("after_operation_effect")
            if effect_ok and operation.effect_type in MARKER_AFTER_EFFECT:
                _append_marker(
                    context,
                    operation,
                    marker=marker,
                    actor=actor,
                    runner=dispatch,
                    sensitive=sensitive,
                )
            observed, classification = _observe(
                context, operation, runner=dispatch, sensitive=sensitive
            )

    status = CLASSIFICATION_STATUS[classification]
    error_code = guard
    if guard in _AMBIGUITY_GUARDS:
        # The probe stayed ambiguous and the effect is one we refuse to replay.
        status = UNKNOWN
        classification = INSUFFICIENT_OBSERVATION

    observed_at = now()
    post_state_sha = _state_digest(observed)
    result_probe = probe_result(
        probe,
        classification=classification,
        observed_identity=operation.target_identity,
        observed_sha256=post_state_sha,
        observed_state=_observed_state_label(observed),
        evidence_path=context.run_directory / OPERATIONS_DIRNAME / "pending",
        evidence_sha256=GENESIS,
        observed_at=observed_at,
    )
    evidence = {
        "schema_version": "beads.direct-operation-evidence.v1",
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "caller_key": operation.caller_key,
        "effect_type": operation.effect_type,
        "dispatched": dispatched,
        "marker_present": marker_present,
        "classification": classification,
        "observed": _plain(observed),
        "probe": {**result_probe, "evidence_path": None, "evidence_sha256": None},
    }
    evidence_path, evidence_sha = _write_evidence(context, operation_id, evidence)

    resolution = resolution_event(
        prepared,
        status=status,
        observed_post_state_sha256=post_state_sha,
        readback_evidence_path=evidence_path,
        readback_evidence_sha256=evidence_sha,
        timestamp=observed_at,
        error_code=error_code,
    )
    context.journal.append(resolution, hook=context.crash_hook)
    context.crash_hook("after_operation_resolved")

    return DirectOperationResult(
        status=status,
        caller_key=operation.caller_key,
        operation_id=operation_id,
        effect_type=operation.effect_type,
        attempt_id=attempt_id,
        classification=classification,
        observed=observed,
        marker_present=marker_present,
        dispatched=dispatched,
        error_code=error_code,
        prepared=prepared,
        resolution=resolution,
        evidence_path=evidence_path,
    )


def _replay_guard(
    operation: DirectOperation,
    *,
    classification: str,
    marker_present: bool | None,
    ambiguous_prior: bool,
) -> str | None:
    """Return the refusal code when this attempt must not dispatch.

    Only replay-guarded effects with an ambiguous earlier attempt are held
    back, and only while the probe cannot settle what happened.  A decisive
    probe — the effect is plainly there, plainly absent, or plainly something
    else — always wins over the guard.
    """
    if classification != PRESTATE_UNCHANGED:
        return None
    if operation.effect_type not in REPLAY_GUARDED_EFFECTS:
        return None
    if not ambiguous_prior:
        return None
    if marker_present is None:
        return "DIRECT_OPERATION_REPLAY_BLOCKED"
    if marker_present:
        # Our own marker landed but the intended state is gone: either the
        # effect never ran, or it ran and was undone.  Nothing observable can
        # tell those apart, so the coordinator refuses to guess.
        return "DIRECT_OPERATION_AMBIGUOUS_CAUSALITY"
    return None


def _dispatch_effect(
    context: RunContext,
    operation: DirectOperation,
    *,
    actor: str,
    runner: Callable[..., Any],
    sensitive: Any,
) -> bool:
    arguments = {**_plain(operation.arguments), "actor": actor}
    try:
        result = runner(
            _request(context, operation.profile, arguments), sensitive=sensitive
        )
    except _OBSERVATION_ERRORS:
        return False
    return getattr(result, "status", None) == "ok"
